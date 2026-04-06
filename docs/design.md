# Carpenter Design

*Working document. Last updated 2026-04-05.*

---

## What Carpenter Is

A pure-Python AI agent platform implementing the CaMeL pattern (Capability-Mediated Language).

**The core conviction:** the hard problem of autonomous agents isn't capability — it's safety and auditability at the boundary between intent and action. Most agent security models sandbox *execution*, constraining what code can do once it runs. Carpenter inverts this: the primary defense is at *submission time*. The agent observes freely through read-only tools but can only act through reviewed, persisted Python code.

**The threat model** is prompt injection, not adversarial users. The danger is untrusted data — web content, webhooks, API responses — manipulating the AI into generating harmful code. This reframing is what produces the distinctive architecture: code sanitized before the reviewer sees it, credentials that never leave the platform process, and every state change persisted as a file on disk.

**The design philosophy:** give the agent maximum observational freedom, require every action to be reviewable code, persist everything, and build memory structures that let the agent learn from its own history.

**The ambition:** Carpenter is not just a chatbot. It is a persistent, autonomous work platform — an entity that maintains long-running arcs of work spanning days, weeks, or months. Arcs are timeless: a cron-triggered monitor can run for years, spawning child arcs that escalate into template-governed projects. The reflection compression chain (raw events -> daily notes -> weekly patterns -> monthly insights) builds toward persistent self-knowledge. The knowledge base crystallizes learned patterns across conversations. The platform is opaque to its own agents — platform changes are code changes to the platform repo, handled externally by developers and CI/CD. The trust boundary is explicit and auditable at every level — from individual string literals in submitted code to the multi-month arc trees that organize the agent's work.

---

## Core Abstractions

### Arcs

An **arc** is a unit of work with a lifecycle. It is the *only* work abstraction — tasks, projects, cron jobs, and sub-steps are all arcs at different depths in a recursive tree.

State machine: `pending -> active -> waiting / completed / failed / cancelled`. Completed, failed, and cancelled are **frozen** — the record becomes immutable. History entries can still be appended.

Key properties of an arc:
- **parent_id** — tree structure; root arcs have no parent
- **step_order** — sibling execution sequence
- **from_template** — if set, the arc was created by a workflow template and is **immutable**: cannot be deleted, reordered, or have children added
- **integrity_level** — `trusted`, `constrained`, or `untrusted` (see Trust Boundaries)
- **output_type** — `python`, `text`, `json`, or `unknown`
- **agent_type** — `PLANNER`, `EXECUTOR`, `REVIEWER`, `JUDGE`, or `CHAT`

Cancellation cascades to all descendants. Children execute in step_order.

### Planner-Root Convention

For non-trivial multi-step work, a **PLANNER** root arc serves as the coordination and escalation target. When a child arc fails, the PLANNER parent is re-invoked to decide whether to retry, restructure, or ask the user. A built-in `skills/planner-root` KB entry teaches agents this pattern. The platform hints when an agent adds multiple children to a non-PLANNER arc — a signal that the arc should have been created as a PLANNER from the start.

### Child Failure Escalation

When a child arc transitions to `failed`, the platform fires `_notify_parent_of_failure()` on the parent arc. The response is governed by `_escalation_policy` in `arc_state`:

| Policy | Behavior |
|--------|----------|
| `replan` (default) | Re-invoke the parent planner to decide next steps |
| `fail` | Do nothing — let the failure stand |
| `human` | Notify the user via the notification system |
| `escalate` | Retry with a stronger model (consults `escalation.stacks` config) |

Root arcs with no parent check the `escalation.stacks` config for model escalation before falling through to user notification. The handler lives in `child_failure_handler.py`.

### Iterative Planning (Ralph Loop)

Iterative workflows are not a platform feature — they are an **agent strategy** using the existing arc system. A planner creates pairs of sibling arcs under a single parent: an *implementation arc* that does work, and a *monitor arc* that evaluates the result. All arcs are flat siblings executing in step_order. Each monitor, if it decides to continue, creates two more siblings (next implementation + next monitor). When a monitor decides "done" and creates no more arcs, all children complete and the parent regains agency.

The monitor can be a simple Python script checking data conditions, an agent that reasons about approach quality, or a human-gated arc. This pattern requires no special loop machinery — it uses arc creation, step ordering, and completion cascading as they already work.

**Platform-managed performance counters** provide trustworthy resource data to monitors. Each arc maintains `descendant_tokens`, `descendant_executions`, and `descendant_arc_count`, updated by the platform after each execution and arc creation. Monitors read these via `get_arc_detail` to make resource-aware decisions. Because the platform maintains these counters (not executor code), they cannot be tampered with.

The pattern is documented as a KB entry (`skills/iterative-planning`) so the agent can learn and apply it.

### Structured Arc I/O

Arcs pass data via `arc_state`. For typed inter-arc communication, shared **Pydantic data models** define the contract between producer and consumer arcs. Models live in `data_models_dir` (default `~/carpenter/config/data_models/`, seeded from `config_seed/data_models/`) and are importable by executor code via `PYTHONPATH`.

The state tools provide typed methods: `state.set_typed(key, model)` serializes via `model_dump()`, `state.get_typed(key, ModelClass)` deserializes and validates via `model_validate()`. Raw `set`/`get` remain available for simple cases.

Templates can optionally declare `input_contract` and `output_contract` per step (format: `data_models.module:ClassName`). When declared, the platform validates at boundaries — catching cases where raw `state.set` is used on a contracted step. Data model files are Python code and go through the review pipeline like everything else.

### Events and Work Queue

Everything that happens enters as an **event** (append-only table). Events include chat messages, webhooks, cron fires, and arc state changes.

**Event matchers** are dynamic subscriptions registered by arcs at runtime — "wake me when X happens." Matchers have expiry and are cleaned up periodically.

The **work queue** provides exactly-once delivery via idempotency keys. Items are atomically claimed, processed, and marked complete or failed. Failed items retry with exponential backoff up to 4 attempts before going to a dead letter state.

### Main Loop

An asyncio background task combining two rhythms:
- **Wake signal** — instant processing, used for chat messages
- **5-second heartbeat** — periodic work: cron, timeouts, matcher evaluation, trust validation

Each cycle: claim work queue items, match events, check expirations, fire cron, run heartbeat hooks.

### Workflow Templates

YAML files that constrain arc subtrees. Several ship with the platform:

| Template | Steps | Purpose |
|----------|-------|---------|
| `coding-change` | 3 | Agent writes code in isolated workspace, reviewed before merging |
| `writing-repo-change` | 6 | Git branch, changes, PR, review, approval, merge |
| `dark-factory` | 4 | Autonomous spec-driven development with iterative validation |

The dark factory template implements a complete development loop: spec refinement (CHAT) → scenario generation (EXECUTOR) → iterative implementation loop (PLANNER using the iterative planning pattern with coding-change sub-workflows) → completion gate (JUDGE with holdout validation). The implementation loop creates flat impl+validate+monitor arc triplets, iterating until the validation pass rate exceeds a threshold or resource limits are hit. Data flows between steps via Pydantic contracts (`config_seed/data_models/dark_factory.py`).

Template-mandated arcs (those with `from_template=True`) are **completely immutable**:
- Cannot be deleted or modified
- Cannot have children added to them
- Cannot be reordered
- Form a rigid sibling sequence as defined by the template

This ensures workflow integrity — template steps remain exactly as designed, preventing drift or tampering.

---

## Security Architecture

### Read-Only Agency + Pythonic Action

The agent has two modes of interaction:

**Free access** (read-only, via tool_use): read files, get state, list arcs, introspect, recall memory, browse knowledge base.

**Gated access** (via `submit_code`): all file writes, state mutations, web requests, arc management, git operations, scheduling. Code goes through the review pipeline before execution.

### Messaging Restrictions in Executor Code

Arc executor code is blocked from calling `messaging.send` and `messaging.ask`. Only chat `submit_code` contexts (where `execution_context="reviewed"`) can send messages. Arc executors write to state; the platform handles all communication. This prevents executor code — which may process untrusted data — from impersonating the platform or manipulating the user through fabricated messages.

### Review Pipeline

When code is submitted:

1. **Hash check** — SHA-256 against per-conversation approval cache. Previously approved identical code skips review.
2. **Import star check** — `import *` is unconditionally rejected (policy violation, no retry).
3. **AST parse** — reject syntax errors before spending API tokens.
4. **Injection scan** — regex detection of suspicious patterns. Advisory flags for the reviewer.
5. **Histogram LLM analysis** — a cheap model examines per-source word frequency histograms (comments, strings, docstrings separately) for patterns characteristic of prompt injection. Produces advisory flags; non-blocking (graceful fallback on error or missing model).
6. **Sanitize** — the distinctive step:
   - String/bytes literals replaced with placeholders (`S1`, `S2`, ...)
   - F-strings collapsed to single placeholders
   - User-defined variables renamed to sequential identifiers (`a`, `b`, `c`, ...)
   - Comments and docstrings stripped
   - Imported names and builtins preserved
7. **Reviewer AI** — sees sanitized code + conversation context + advisory flags. Judges structure and intent without seeing payload content.

Outcomes: `APPROVE` (execute), `REWORK` (fixable issues, agent retries up to 3 times), `MAJOR` (security concern, requires human decision), `REJECTED` (policy violation, no retry).

The sanitization step is the core defense against prompt injection — an attacker can embed instructions in string literals, but the reviewer never sees them.

### Adversarial Review Mode

An optional review mode where the reviewer **must** find issues. Zero findings indicates insufficient review, not clean code. Configured via `review.adversarial_mode` (default: false) and `review.adversarial_min_findings` (default: 1).

Three-pass escalation:
1. Standard adversarial review with structured findings (location, severity, description, remediation)
2. If findings < threshold: re-review with escalated scrutiny prompt emphasizing edge cases, security boundaries, error handling, race conditions
3. If still insufficient: model escalation (if configured) or MAJOR verdict for human attention

Findings are structured data (severity: critical/warning/note) formatted for human consumption. This encodes the operational wisdom that meaningful review always surfaces something — rubber-stamp reviews are the most common failure mode in both human and AI code review.

### Execution Sessions

Every code execution gets a platform-generated UUID session. The session is created *before* the code executes, linking session -> code_file -> review status (looked up from the database, not attested by the executor).

The executor receives the session ID as `CARPENTER_EXECUTION_SESSION` and sends it as `X-Execution-Session-ID` in callback requests. The platform validates: session exists, reviewed=true, not expired (default 1 hour). Read-only callbacks work without a session.

This means a compromised executor cannot spoof review status — session IDs are opaque tokens whose meaning is determined by the platform's database.

### Output Isolation at Submit

When `submit_code` executes code that imports untrusted tool modules (e.g., `act/web`), the raw execution output — which may contain attacker-controlled content — is **never returned to the chat agent's context** (I1). Instead, the agent receives structured JSON metadata: `status`, `output_key`, `output_bytes`, and `exit_code`. The output is stored in arc state (keyed by `output_key`) for retrieval by review arcs, and persisted to the execution log file. A `taint_source` column on `code_executions` records which untrusted tool triggered the taint, enabling fast lookup without re-parsing code files.

This is enforced **fail-closed**: if the taint check itself fails (exception, unreadable code file), output is withheld and the failure is logged. The invariant is absolute — no AI sees untrusted data unless it is in a designated review arc.

### Network Egress

Executor code runs with **default-deny network egress**. All web access goes through `act/web.py` via the `dispatch()` bridge — the platform makes the actual outbound request, and executor code never touches the network directly. The RestrictedPython sandbox blocks all imports (including `socket`, `urllib`, `requests`), making direct network access impossible at the language level.

The review pipeline is the first defense (catches suspicious patterns); the RestrictedPython import block is the second (prevents direct network access even if the reviewer misses it). Platform packages may add additional OS-level enforcement (e.g., network namespaces) for external coding agents that run outside the restricted executor.

### Transport Security (TLS)

When `tls_enabled` is true, uvicorn terminates TLS directly. All connections — external clients and executor callbacks — use HTTPS with proper certificate verification. The `tls_domain` config key specifies the hostname matching the certificate's SAN/CN; the code manager constructs callback URLs as `https://{tls_domain}:{port}`. Executors verify the platform's certificate using the system CA bundle (sufficient for Let's Encrypt) or a configured CA file (`tls_ca_path`, for self-signed certs). There is no mixed HTTP/HTTPS mode and no verification bypass.

### Tool Tiers

| Tier | Mechanism | Credential exposure |
|------|-----------|-------------------|
| Callback | HTTP POST to platform | None — credentials stay in platform process |
| Direct | Pure Python in executor | None needed |
| Environment | Credential injection | Explicitly configured per-tool |

The executor-side `carpenter_tools` package is essentially an RPC client. The platform holds all credentials, rate limits, and audit logs.

### Tool Partitioning

```
carpenter_tools/
  read/   — safe, direct agentic access (files, state, arc, messaging)
  act/    — requires reviewed code (files, state, arc, web, git, scheduling, review)
```

The `@tool()` decorator declares safety properties: `local`, `readonly`, `side_effects`, `trusted_output`. `validate_package()` enforces that read/ tools are all safe and act/ tools have at least one unsafe property. The `trusted_output=False` declaration (currently only on the web tool) feeds into taint tracking.

---

## Trust Boundaries

Arc-level trust zones extend the basic review pipeline into a system for handling untrusted data flows. The system implements an information-flow control (IFC) lattice with three integrity levels. See `trust-invariants.md` for the formal invariants (I1-I9) with enforcement files and test pointers.

### Integrity Levels

Three-level lattice governing whether data may influence control flow:

- **trusted** (T) — default. Data from the user, platform configuration, or deterministic policy checks. May influence planner decisions. Cannot access untrusted data tools (I2).
- **constrained** (C) — data extracted from untrusted sources through a constrained channel (Q-LLM with Pydantic output schema). The schema bounds the *structure* but does not guarantee *correctness*. Safe to check against policy but not safe to act on directly. Cannot influence control flow without a deterministic policy check (I8).
- **untrusted** (U) — raw external data (web content, webhooks, API responses). Must go through constrained extraction before it can be used.

Integrity does NOT propagate upward: a trusted parent can orchestrate untrusted children without itself becoming untrusted. The real enforcement is I2 — trusted arcs get HTTP 403 on untrusted data tools.

The hard line between TRUSTED and CONSTRAINED is the control-flow boundary. Only TRUSTED data may influence planning decisions. This boundary is deterministic and platform-enforced — no probabilistic mechanism (LLM judgment) sits on this line.

### Agent Types and Capabilities

Each arc declares an agent type. A kernel-level capability matrix restricts which tools each type can use:

- **PLANNER** — structural/messaging tools only (create arcs, send messages). No data access. Context is TRUSTED only.
- **EXECUTOR** — full tool access within its integrity level.
- **REVIEWER** — read tools + `submit_verdict`. Can read untrusted data (`can_read_untrusted` capability) for extraction purposes.
- **JUDGE** — not an LLM. Runs deterministic platform code (`security/judge.py`) to validate extracted data against security policies (default-deny allowlists). The judge's verdict is authoritative.
- **CHAT** — standard chat agent tools. Context is TRUSTED only.

### Review Arcs and Trust Promotion

When an untrusted arc needs its output trusted, the review manager creates review arcs as siblings. The pattern follows FIDES-style decomposition:

1. **REVIEWER** arc extracts structured data through constrained schema (U → C)
2. **JUDGE** arc runs deterministic policy checks as platform code (C → T)

On judge approval: target arc's `integrity_level` is promoted to `trusted` (I3). The judge's authority is scoped to the target arc only — parent arcs are not automatically promoted (I6).

Non-trusted arcs are enforced at creation time via `arc.create_batch`, which validates that every non-trusted arc has at least one reviewer and a judge (I4).

### Verification Arcs (Separation of Powers)

After a coding-change arc completes, the platform auto-creates **verification sibling arcs**: a correctness check, a quality check (for platform/tool code), a judge to synthesize results, and a documentation arc. Each verification arc carries `arc_role="verifier"` and a `verification_target_id` pointing to the implementation arc it verifies. The target must be a sibling — cross-subtree verification is not allowed. Self-verification is blocked at creation time: the verification target cannot be the same arc or share an agent identity.

This enforces separation of powers: the agent that wrote the code cannot be the agent that judges it.

### Trust Encryption

Non-trusted arc state (integrity_level `untrusted` or `constrained`) is Fernet-encrypted at rest (I7). Keys are generated per reviewer-target pair and stored in `review_keys`. Only designated reviewers (or anyone after trust promotion) can decrypt.

### Trust Audit Log

A dedicated append-only table (`trust_audit_log`) records all boundary decisions: integrity level assignments, access denials, review verdicts, trust promotions, decryption grants. Separate from arc history to avoid redundancy.

---

## Agent Architecture

### Composable System Prompt

The system prompt is assembled from user-editable markdown files in `config_seed/prompts/`, loaded by `prompts.py`. Each file has a numeric prefix for ordering and a `compact: true/false` front-matter flag for small-context models.

Section order (from `config_seed/prompts/`): `00-identity`, `01-core-behavior`, `02-kb-and-tools`, `03-modules`.

Dynamic additions appended at build time: platform source directory, KB root index, auto-search results, recent conversation hints.

### Chat Tool_Use Loop

The chat agent uses tool_use for read-only tools and `submit_code` for all actions. No direct action tools are exposed to the agent. The loop is capped at `chat_tool_iterations` (default 10).

Available tools: `read_file`, `list_files`, `file_count`, `get_state`, `word_count`, `char_count`, `list_arcs`, `get_arc_detail`, `list_recent_activity`, `submit_code`, `kb_describe`, `kb_search`, `kb_links_in`, `get_kb_health`.

### Context Management

Two complementary mechanisms prevent context window degradation:

**Tool output truncation** — a code-manager-level concern. When a tool result exceeds `tool_output_max_bytes` (default 32KB), the full output is saved to `data/tool_output/YYYY/MM/DD/` and the agent receives only head + tail lines with a notice pointing to the full file. The agent can `read_file` the full output if needed. This prevents a single tool call from consuming excessive context.

**Context compaction** — when the conversation approaches a token budget (configurable as a fraction of the model's context window via `compaction_threshold`, default 0.8, or as an absolute token count via `compaction_threshold_tokens`), the platform automatically summarizes older messages. The check runs before each API call in the tool_use loop. The same model is used for summarization — summary quality matches conversation quality.

Compaction preserves the most recent N messages (`compaction_preserve_recent`, default 8) and replaces older messages with a structured summary covering key decisions, state mutations, results, and pending work. **Original messages are never deleted** — they remain in the `messages` table. Each compaction creates a `compaction_events` record tracking the summarized message range, and inserts a synthetic summary message with a `compaction_event_id` reference. A long conversation may compact multiple times; each event is independent and the full original context is always recoverable.

### AI Providers

- **Anthropic** — raw httpx to the Messages API (not the SDK)
- **Ollama** — OpenAI-compatible `/v1/chat/completions` endpoint

Selected via `ai_provider` config key. Model escalation is configurable per-task.

### Model Selection

A **model registry** (`model_registry.yaml`) declares available models with metadata: `provider`, `model_id`, `quality_tier` (1–5), `cost_per_mtok_in/out`, `context_window`, and `capabilities` (list of task types the model supports). Arc creation accepts `agent_model` as a short name (e.g., `"opus"`) which resolves to the full `provider:model_id` string. See `model-selection-guide.md` for the full selection algorithm, presets, and field reference.

Templates can specify `min_quality` per step to enforce minimum capability — security review steps require tier 5, preventing cost-optimization from undermining review quality.

Both clients use **exponential backoff with jitter** for transient failures (5xx, connection errors, timeouts) and a **per-provider circuit breaker** that fast-fails after consecutive failures. The circuit breaker transitions through CLOSED → OPEN → HALF_OPEN states: after `circuit_breaker_threshold` consecutive failures it opens (requests fail immediately), then after `circuit_breaker_recovery_seconds` it allows a single probe request. HTTP 429 responses are handled by the rate limiter (which fills its sliding window to create natural cooldown) rather than the circuit breaker. Client-error 4xx responses (except 429) are not retried.

### Coding Agents

Agent-agnostic coding workflow. Profiles configured in `coding_agents` config:

- **Built-in** — tool_use loop with read/write/edit/bash tools
- **External** — arbitrary subprocesses (e.g., coding agents, Aider)

Coding-change arcs manage workspace lifecycle: create git-backed workspace -> run agent -> generate diff -> review -> approve/reject -> apply or clean up.

---

## Memory and Continuity

### Conversation Boundaries

In single-stream mode (Signal, Telegram), a 6-hour silence gap triggers a new conversation. At the boundary, a background thread generates a structured summary (Topics Discussed, Key Decisions, User Preferences, Pending Items) using the cheapest model. New conversations receive the prior summary rather than raw message tail.

### Memory Tools

Cross-conversation recall is handled through the knowledge base:
- `kb_search(query, path_prefix="conversations/")` — full-text search across conversation summaries via FTS5 (BM25 ranking, Porter stemming, phrase queries).
- `kb_search(query, path_prefix="reflections/")` — search past self-reflections.
- `kb_describe(path)` — read any entry in detail (conversations, reflections, or other KB content).

Recent conversation hints (last 3 titles and dates) are appended to the system prompt.

### Reflective Meta-Cognition

Cadenced self-reflection via cron, forming a compression chain:

| Cadence | Input | Output |
|---------|-------|--------|
| Daily | Raw activity data (conversations, arcs, tools, errors) | Daily notes |
| Weekly | Daily reflections + 7-day stats | Weekly patterns |
| Monthly | Weekly reflections + 30-day stats + KB skill entries | Monthly insights |

Each reflection creates a dedicated conversation, invokes the full chat agent with tool access, saves to the `reflections` table, and archives the conversation. An activity threshold skips API calls on quiet days. Opt-in via `reflection.enabled` config.

### Reflection Auto-Action

Reflections produce `proposed_actions` but observations alone don't close the loop. After a reflection completes, an auto-action process examines proposed actions against current platform state and submits workable changes through the existing review pipelines (code review for tools/config, KB review for knowledge base entries).

**Review level is configurable, not hardcoded**: `reflection.review_mode` selects AI review, human review, or no review. **Taint-aware routing**: if the reflection touched untrusted data, `reflection.tainted_review_mode` applies (default: human). Trusted reflections use the standard configured mode.

Rate limits prevent runaway self-modification (configurable `max_actions_per_reflection` and `max_actions_per_day`). Results are batch-notified to the user via the notification system.

### Notifications

The platform communicates with the user through priority-routed channels:

- **Chat** — system message in the active conversation. Always available.
- **Email** — via SMTP credentials or a shell command receiving the message on stdin. Configurable, disabled by default.
- **Log** — `notifications` table audit trail. Always on.

Routing by priority: `urgent` → chat + email, `normal` → chat if active session else email, `low` → email only, `fyi` → log only. Users can override routing per notification category. Notifications are batched within a configurable window to avoid spamming.

---

## Skill Knowledge

Operational knowledge is stored as KB entries under the `skills/` path prefix. Agents discover skills via `kb_describe('skills')` and `kb_search()`. Each skill is a subtree of markdown entries with an `_index.md` root and optional resource pages linked via `[[wikilinks]]`.

The platform ships seed skill entries (e.g., `skills/failure-patterns` — operational knowledge about common failure modes with prescribed escapes) installed from `config_seed/kb/skills/` on fresh instances. Agents can create or modify skill KB entries via `kb.add`/`kb.edit`.

**Security review for skill modifications**: When an agent writes to a `skills/` KB path, the `skill-kb-review` template workflow is triggered. This runs a classify-source step (checks conversation taint), a progressive text review (for tainted sources), an AI intent review, and a human-escalation step. **Conversations that have processed untrusted data cannot modify skill knowledge without human review** — this prevents web content -> agent -> KB modification -> persistent knowledge poisoning.

---

## Execution

### All Code Is Files

Every piece of agent-generated Python is saved to a date-partitioned directory and tracked in `code_files`:

```
data/code/2026/03/16/000001_agent_step.py
data/logs/2026/03/16/000001_agent_step.log
```

### Executor

The only executor is the **RestrictedPython in-process executor**. Code runs in a restricted sandbox within the platform process using threading for isolation:

- **RestrictedPython** blocks all imports, `open`, `eval`/`exec`, and attribute access to private names
- **JSON-serialized queue boundary** between executor thread and platform — no object reference leakage
- **`dispatch(tool_name, params)`** is the sole interface from executor code to platform capabilities
- **`PyThreadState_SetAsyncExc`** for cooperative timeout enforcement

The subprocess and Docker executor types were removed. Platform packages (e.g., carpenter-linux) may provide additional OS-level sandboxing for external coding agents, which run as separate processes outside the restricted executor.

### Retry

- **Mechanical** — same operation, transient failure. Max 4 attempts, exponential backoff.
- **Agentic** — agent modifies approach. Default budget 10 iterations, doubles on user approval up to 256.

---

## Configuration

All instance-specific settings via `~/carpenter/config.yaml` or standard environment variables (e.g., `ANTHROPIC_API_KEY`, `UI_TOKEN`, `TINFOIL_API_KEY`). Four-layer precedence: env vars > {base_dir}/.env credential file > YAML > built-in defaults. No hardcoded credentials in the package.

---

## Data Model

SQLite WAL mode. 20+ tables including:

| Table | Purpose |
|-------|---------|
| `arcs` | Work tree (parent_id, status, integrity_level, agent_type) |
| `arc_history` | Append-only lifecycle log |
| `events` | Append-only event log |
| `event_matchers` | Dynamic arc-registered subscriptions |
| `work_queue` | Exactly-once job processing |
| `code_files` | Every piece of agent code (path, review_status) |
| `code_executions` | Execution records (exit_code, log_file) |
| `execution_sessions` | Platform-controlled session tokens |
| `conversations` | Title, summary, archived flag |
| `messages` | Conversation messages |
| `tool_calls` | Tool usage audit trail |
| `api_calls` | Token metrics |
| `conversation_taint` | Tracks which conversations have processed untrusted data |
| `trust_audit_log` | Trust boundary decisions |
| `review_keys` | Fernet encryption keys per reviewer |
| `reflections` | Self-reflection outputs |
| `workflow_templates` | Template definitions |
| `cron_entries` | Scheduled tasks |
| `conversations_fts` | FTS5 full-text index on conversation titles/summaries |
| `compaction_events` | Context compaction records (message ranges, model, tokens reclaimed) |
