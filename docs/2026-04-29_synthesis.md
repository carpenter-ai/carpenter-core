# Carpenter — Conceptual Briefing

*A synthesis for future copies of Claude working on this codebase. Written 2026-04-29 from `design.md`, `trust-invariants.md`, `coding-invariants.md`, `security-model.md`, `template-rigidity.md`, and `verified-flow-analysis.md`. Read those for depth; read this first to understand what we are aiming at.*

---

## What we are building

A pure-Python AI agent platform that can run autonomously for months — spawning child arcs from cron triggers, escalating into template-governed projects, compressing its own history into self-knowledge — without ever letting prompt-injected instructions leak from untrusted data into a trusting AI's context or into an executed action.

The hard problem of autonomous agents is **not capability**. It is **safety and auditability at the boundary between intent and action**. Most agent designs sandbox execution. Carpenter inverts this: **the primary defense lives at submission time**. The agent observes freely through read-only tools, but every action is reviewed Python code persisted as a file on disk.

The threat model is **prompt injection**, not adversarial users. The danger is untrusted bytes — web pages, webhook payloads, API responses — manipulating the AI into emitting harmful code. This reframing is what produces the distinctive shape of the system.

---

## The core architecture in five ideas

### 1. Arcs are the only agent-visible unit of work

Tasks, projects, cron jobs, sub-steps, iterative loops — all arcs at different depths in a recursive tree. State machine: `pending → active → waiting / completed / failed / cancelled / escalated`. Frozen statuses are immutable. Cancellation cascades to descendants.

**Do not introduce parallel work concepts** (jobs, tasks, loops). Iterative workflows are an *agent strategy* — a planner creates `(implementation, monitor)` sibling pairs; each monitor decides whether to spawn the next pair. The platform supplies counters (`descendant_tokens`, `descendant_executions`, `descendant_arc_count`) that monitors can trust because executors cannot tamper with them.

The internal **work queue** is a separate platform mechanism for reliable processing of platform jobs (event dispatch, state transitions, reflections, notifications). It is not an agent abstraction. Code that conflates arcs and work-queue items is wrong.

### 2. Read-only agency + pythonic action

Two modes of interaction, sharply separated:

- **Free** (tool_use, read-only): read files, query state, list arcs, search KB, recall reflections.
- **Gated** (`submit_code`): every mutation — file writes, state changes, web requests, arc creation, git, scheduling, messaging from the platform — goes through the review pipeline.

Tool partitioning enforces this physically: `carpenter_tools/read/` (no side effects, direct agentic access) vs `carpenter_tools/act/` (requires reviewed code). `validate_package()` enforces it at decoration time. **Never add write/mutation tools as direct chat tools.** This is invariant I10.

Arc executor code is forbidden from `messaging.send` / `messaging.ask`. Only chat `submit_code` contexts may speak to the user — the platform handles communication so executor code processing untrusted data cannot fabricate platform messages.

### 3. The review pipeline is the trust gate

When code is submitted: hash check → `import *` rejection → AST parse → injection scan → histogram analysis → **sanitize** → reviewer AI (sees sanitized code + conversation context, not payload content).

**Sanitization is the distinctive defense.** String/bytes literals become placeholders (`S1`, `S2`). F-strings collapse. User-defined names become `a, b, c…`. Comments and docstrings strip. The reviewer judges *structure and intent*, never seeing the attacker-controlled bytes embedded inside literals.

Outcomes: `CACHED` / `APPROVE` / `REWORK` (3 retries) / `MAJOR` (human) / `REJECTED` (no retry). Optional **adversarial mode** requires the reviewer to surface findings — zero findings means insufficient review, not clean code.

### 4. Information-flow control: a three-level integrity lattice

Every arc has an `integrity_level`: `trusted` (T), `constrained` (C), or `untrusted` (U). The lattice governs whether data may influence control flow.

- **T** — user input, platform config, deterministic policy outputs. May drive planning.
- **C** — extracted from untrusted sources via a constrained channel (Q-LLM with Pydantic schema). Bounded *structure*; not guaranteed *correctness*. Treated identically to U for access control (conservative default).
- **U** — raw external bytes. Must go through extraction before use.

Trust does not propagate upward: a trusted parent orchestrating untrusted children stays trusted. The real enforcement is **HTTP 403 on untrusted-data tools for trusted arcs** (I2).

The **only** path U → T is **JUDGE approval**. JUDGE arcs **run deterministic platform code, not LLMs** — they validate extracted data against default-deny allowlists in `security/policies.py`. The judge's verdict is authoritative and scoped to the target arc only (I3, I6). No probabilistic mechanism sits on a trust boundary.

Non-trusted arcs may only be created via `arc.create_batch()`, which validates the batch contains a reviewer and a judge (I4). Non-trusted state is Fernet-encrypted at rest with keys held by designated reviewers (I7). Output of `submit_code` in tainted contexts returns metadata only, never raw bytes — fail-closed (I1).

### 5. Verification arcs and separation of powers

After a coding-change arc completes, the platform auto-creates verification *siblings*: correctness check, quality check (for platform code), JUDGE synthesis, documentation. Each carries `verification_target_id`. Self-verification and cross-subtree verification are blocked at creation. **The agent that wrote the code cannot judge it.** The parent (typically a PLANNER) coordinates but never adjudicates its own children's output — that is routed to a designated reviewer/judge pair.

---

## The non-negotiable invariants

These are the load-bearing truths. Trust-boundary invariants `I1-I10` are formal and tested in `tests/test_taint_invariants.py`. Coding invariants are aspirational targets — new code moves toward them, gaps are welcome to call out.

### Trust (I1–I10, distilled)

1. **No raw untrusted output reaches a trusting AI's context.** `submit_code` and `get_execution_output` return metadata only when code imports tainted modules. Fail-closed if the taint check itself errors.
2. **Trusted arcs cannot read untrusted Resources.** Raw Resources (`produced_by_template=NULL`) and unapproved verdicts are refused; bodies are never echoed in refusals.
3. **U → T only via JUDGE approval** by deterministic platform code.
4. **Non-trusted arcs only created in batches with reviewers.** Single `arc.create()` of non-trusted is rejected.
5. **Parents stay trusted** when orchestrating non-trusted children.
6. **JUDGE approval promotes only the target arc**, not the parent.
7. **Non-trusted state is encrypted at rest**; keys are reviewer-scoped; encryption-required is fail-closed.
8. **CONSTRAINED data cannot influence control flow** without a deterministic policy check against a trusted reference.
9. **Policy-typed literals** (`Email`, `Domain`, `Url`, `FilePath`, `Command`, `IntRange`, `Enum`, `Pattern`) validate against allowlists that **default to deny** — empty allowlist means nothing comparable.
10. **Chat tools have enforced trust boundaries.** `chat`-boundary tools may only declare read capabilities. `platform`-boundary is a hardcoded frozenset (`submit_code`, `escalate_current_arc`, `escalate`) — user config cannot create platform tools.

### Engineering posture (the 19 coding invariants in spirit)

- **Narrow platform, wide configuration.** Adding a capability should almost never require a platform change — it should be a tool module, template, data model, or KB entry.
- **Config over code.** Instance behavior reads from the config loader; the package ships no credentials, no site identity, no "dev mode" that lowers guarantees. Never hardcode the config path.
- **Defense in depth, no layer assumes another caught it.** The review pipeline is primary; RestrictedPython, `dispatch()` as sole bridge, session validation, network default-deny, and the capability matrix are independent. Every layer must hold on its own.
- **Platform is authoritative; the executor attests to nothing.** Review status, integrity level, session validity, taint source, arc lineage — every security-relevant fact comes from the database.
- **Deterministic checks on hard boundaries.** Control-flow promotion and capability decisions are platform Python only. JUDGE is deterministic policy.
- **Fail-closed.** Ambiguous, missing, or erroring security checks deny. Always.
- **No credentials in the executor process.** All credentialed operations go through `dispatch()`. `carpenter_tools` is an RPC client.
- **Capability matrix is the platform's opinion, enforced in one place, modifiable only under human review.** No tool may re-grant itself capability its agent type lacks.
- **Security-relevant changes are human-gated by default.** Any modification to security code, trust policies, the matrix, review logic, integrity rules, egress rules, credential handling, or sanitization cannot be approved by AI review or auto-actioned by reflection.
- **Append-only audit trails for critical domains.** `arc_history`, `events`, `trust_audit_log`, `code_files`, `code_executions`, `messages`, `compaction_events`. If a new feature makes security/trust/correctness decisions downstream, it leaves an append-only trace.
- **Lossy views never destroy their source while it is retained.** Compaction, summarization, and tool-output truncation produce derived artifacts alongside the originals — never replacing them in a single transaction.
- **Frozen and template-mandated arcs cannot mutate.** Templates never drift: the sequence that ran is the sequence the template defined.
- **Typed contracts at template boundaries.** `state.set_typed` / `state.get_typed` and Pydantic models in `data_models_dir`.
- **Platform-maintained counters are the only trustworthy resource signal.** Executors do not report; the platform updates and monitors read.
- **Exactly-once for side-effectful platform work** through the work queue with idempotency, bounded retry, exponential backoff, and a first-class **dead-letter** terminal state.
- **Tools partition cleanly into `read/` and `act/`.** `@tool()` properties (`local`, `readonly`, `side_effects`, `trusted_output`) are load-bearing for taint tracking, not documentation.

---

## How to think when working on this codebase

### Before changing anything, locate the trust boundary

Ask: does this touch the review pipeline, sanitization, the capability matrix, integrity-level rules, JUDGE code, encryption, egress, credential handling, or the chat tool registry? If yes, the change is **security-relevant** — it requires human review, cannot be AI-approved, and gets the most careful possible treatment. The threshold is impact on the trust model, not file location.

### Prefer adding a tool/template/KB entry over adding platform mechanism

Almost every new capability should land in `carpenter_tools/{read,act}/`, a workflow template under `config_seed/templates/`, a data model under `config_seed/data_models/`, or a KB entry under `skills/`. The platform implements mechanism; policy and capability live in configuration. If you find yourself adding a parallel work abstraction, reach for arcs, templates, and events instead.

### Treat the executor as untrusted

Anything coming from the executor — review status claims, attestation of trust level, session validity — is hostile input. The platform looks up these facts in its database. If you write code that takes a security-relevant fact from executor input, you have introduced a bug.

### Understand what the lattice actually enforces

The hard line is **TRUSTED vs CONSTRAINED**, deterministic and platform-enforced. CONSTRAINED is treated as UNTRUSTED for access control — that's intentional conservatism. The only mechanism that crosses the line is a JUDGE running deterministic policy code. **Never put an LLM on a trust boundary.** Verified flow analysis (designed in `verified-flow-analysis.md`, not yet implemented) is the principled future path for letting CONSTRAINED data drive control flow safely; until then, the structural decomposition (CHAT/PLANNER orchestrates → REVIEWER extracts → JUDGE validates) is the path.

### Match the rhythm of the system

Carpenter is built to run for months. Reflections compress raw activity into daily/weekly/monthly insights. The KB crystallizes learned patterns. Skill knowledge under `skills/` cannot be modified by tainted conversations without human review — this prevents `web → agent → KB → persistent poisoning`. When you touch features that interact with memory, reflection, or KB, **assume the platform is recording its own history and that future agents will read it back**. Anything that destroys history without trace is wrong (invariants 13, 14).

### Persist everything; make it observable

Every code submission is a file. Every event is a row. Every arc transition appends to `arc_history`. Every trust decision lands in `trust_audit_log`. When designing a new mechanism, ask: where does the audit trail live? If the answer is "nowhere", design the audit trail first.

### When the user asks for a feature, ask three questions

1. **Where does this go?** New tool module, new template step, new KB entry, new data model, or — last resort — platform code.
2. **What is the trust level of its inputs and outputs?** If it ingests untrusted data, the path is `untrusted arc → REVIEWER (extract) → JUDGE (policy check) → trusted consumers`, never directly into a trusted context.
3. **Does it cross a security boundary?** If yes, it is human-gated by default; route accordingly.

---

## The ambition

Carpenter is not a chatbot. It is a persistent, autonomous work platform — an entity that maintains long-running arcs spanning days, weeks, or months, that learns from its own history, that knows what it cannot trust and routes that data through explicit review, that never quietly edits the trust model that governs it. The trust boundary is auditable at every level — from individual string literals in submitted code to multi-month arc trees.

The platform is **opaque to its own agents**. Platform changes are code changes to the platform repo, handled externally by developers and CI/CD. Agents cannot rewrite the kernel that governs them. This is the deepest property of the design: **the system the agent runs inside is not a system the agent can edit**.

When you work on this codebase, you are extending a trust kernel. Treat it accordingly.
