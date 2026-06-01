# Carpenter Coding Invariants

A tight set of principles the platform aims for. These are the target — we do not yet meet all of them everywhere, and calling out a gap against this list is welcome. New code should move toward these invariants, not away from them.

See [`design.md`](design.md) for the architecture that these invariants govern, and [`trust-invariants.md`](trust-invariants.md) for the formal I1–I9 trust-boundary properties referenced below.

---

## Platform shape

### 1. Narrow platform, wide configuration

Platform code implements mechanism; policy, workflows, tools, and skills live in YAML templates, Python tool modules under `act/` / `read/`, data-model files, and KB entries. Adding a capability should almost never require a platform change — it should be a new tool module, template, data model, or KB entry.

### 2. One abstraction for *agent* work

Arcs are the only agent-visible work unit — tasks, projects, cron jobs, and sub-steps are all arcs. Agent-facing features compose from arcs, templates, and events; they do not introduce parallel concepts (tasks, jobs, loops).

The internal **work queue** is a separate platform mechanism for reliable processing of platform jobs (event dispatch, arc state transitions, reflections, notifications). It is not an agent abstraction and must not leak into agent-facing APIs or templates. Arcs and work-queue items are distinct, and code that conflates them is wrong.

### 3. Config over code

Instance-specific behavior (models, providers, review modes, egress, escalation policies, hostnames) is read from the configured config file (location resolved via the documented config-path env var / loader, with a documented default) and environment variables. The package ships no credentials, no site identity, and no "dev mode" that lowers guarantees. Code must never hardcode the config path — always go through the config loader.

---

## Security posture

### 4. Primary defense is at submission time; execution-time defense is real but secondary

The review pipeline (sanitize + reviewer AI + policy checks) is the main trust gate — it is where we try hardest to catch bad code. Execution-time defenses (RestrictedPython import block, `dispatch()` as sole tool bridge, session validation, network default-deny, capability matrix) exist as independent layers and must hold on their own: security properties must survive a tricked reviewer, and sandbox properties must survive a tricked submission. Neither layer is allowed to assume the other caught it.

### 5. Platform is authoritative; the executor attests to nothing

Review status, integrity level, session validity, taint source, arc lineage — every security-relevant fact is looked up from the platform database, never accepted from executor-supplied input.

### 6. Deterministic checks on hard boundaries

Control-flow promotion (U → C → T) and all capability-matrix decisions cross through platform Python code only. No LLM judgment sits on a trust boundary — JUDGE is deterministic policy, not a model.

### 7. Fail-closed

When a security check errors, raises, or returns ambiguous results, the answer is "deny". Taint-check exceptions, missing session IDs, unreadable code files, malformed integrity levels — every failure mode blocks by default.

### 8. Untrusted bytes never touch a trusting AI's context

Raw output from taint sources is returned only as opaque `output_key` references; only designated REVIEWER arcs may read it. This invariant is absolute (I1) and must be directly testable.

### 9. No credentials in the executor process

All credentialed operations go through `dispatch()`. The `carpenter_tools` package is an RPC client; secrets live in the platform process and never cross the sandbox boundary.

### 10. Separation of powers at the leaf, not the subtree

The *specific* arc that produces an artifact cannot be the *specific* arc that judges it. Producer and reviewer are distinct siblings with different agent identities; verification arcs carry an explicit `verification_target_id`; self-verification and cross-subtree verification are blocked at creation.

This is compatible with — and in fact requires — a parent (typically a PLANNER) that orchestrates both a producing child and a reviewing sibling. The parent's role is coordination, not judgment: it never adjudicates its own children's output, it routes that decision to a designated reviewer/judge pair. "One arc producing and its sibling reviewing" is the intended shape, not a violation.

### 11. Capability matrix is the platform's opinion, enforced in one place, modifiable only under human review

Agent type (PLANNER / EXECUTOR / REVIEWER / JUDGE / CHAT) restricts tool access through a single kernel-level check, run before the tool executes — not via per-tool ad-hoc logic. The shipped matrix encodes the platform's default trust model.

Users *can* override it via config, but because changes to this matrix broaden the blast radius of every agent on the system, any such change must flow through a human-review-gated config path (never AI-approved, never reflection-auto-actioned). Platform code must treat the matrix as the sole source of truth — no tool may re-grant itself capability its agent type lacks.

### 12. Security-relevant changes are human-gated by default

Any modification to security code, trust policies, the capability matrix, review-pipeline logic, integrity-level rules, egress rules, credential handling, or the sanitization stage requires human review as the default path — these changes cannot be approved by AI review or auto-submitted by reflection auto-action.

The threshold is *impact on the trust model*, not file location: a config change that weakens review is security-relevant; a YAML edit that adds a new KB category is not. Humans can reconfigure what counts as security-relevant and what review mode applies, but that reconfiguration is itself security-relevant and gated the same way. In short: the trust model cannot be quietly edited by the system it governs.

---

## Data discipline

### 13. Critical domains have append-only audit trails

Arc lifecycle (`arc_history`), events (`events`), trust-boundary decisions (`trust_audit_log`), submitted code (`code_files`), executions (`code_executions`), messages (`messages`), and compaction (`compaction_events`) are append-only — the history of these domains is never overwritten or deleted.

Other tables (work queue, matchers, cron entries, review keys) are legitimately mutable because they represent current state, not history. The invariant is: anything that makes a security, trust, or correctness decision downstream must leave an append-only trace. If a new feature makes such decisions, it adds to an existing log or gets its own — it does not silently mutate and move on.

### 14. Lossy views never destroy their source while it is retained

Compaction, summarization, and tool-output truncation produce derived artifacts that stand alongside — not in place of — the originals they were built from. Each lossy step carries a reference back to its source (`compaction_event_id`, saved tool-output path, summarized-message range) so the full original can be reconstructed for as long as it exists.

Retention policies may eventually remove originals on a deliberate, configured schedule; that is a separate, explicit decision, not an implicit side effect of the lossy step. The invariant we enforce in code: no code path summarizes-then-deletes in a single transaction, and no lossy artifact outlives its retention window without either the source still being present or a clear record that it was retained-out.

### 15. Frozen arcs and template-mandated arcs cannot be changed

Once an arc reaches `completed`, `failed`, or `cancelled`, its record is immutable — status, outputs, parent, step order, and children are fixed. Arcs with `from_template=True` are immutable from creation: they cannot be deleted, reordered, retargeted, or have children added.

The one permitted post-freeze operation is appending to `arc_history` (and equivalent append-only logs) — lifecycle may continue to be *observed*, but the arc itself does not mutate. Templates therefore never drift: the sequence of steps a workflow defines is the sequence that ran.

### 16. Typed contracts at template boundaries

Data flowing between contracted template steps passes through declared `attrs`-defined data classes (serialized and validated via `cattrs.unstructure` / `cattrs.structure`). Models live in the configured data-models directory and are loaded by `validate_contract()` at step boundaries.

`state.set_typed` / `state.get_typed` are the typed path; raw `state.set` / `state.get` is only acceptable on steps without a declared `input_contract` / `output_contract`.

### 17. Platform-maintained counters are the only trustworthy resource signal

`descendant_tokens`, `descendant_executions`, `descendant_arc_count`, and related resource counters are updated by platform code, never by executors. Monitors, judges, and policy gates read from these — never from executor-reported numbers.

---

## Operational

### 18. Exactly-once for side-effectful platform work

Internal platform jobs whose side effects must not be repeated (event dispatch, reflection triggers, notifications, arc state transitions) go through the work queue with an idempotency key, bounded retries, and exponential backoff.

Items that exhaust their retry budget land in a **dead-letter state** — a terminal status on the work queue record meaning "we gave up retrying; the item is preserved for inspection and will not run again without explicit operator action." Dead-letter is a first-class outcome, not an error: it is visible, queryable, and requires a human decision to retry, discard, or patch the underlying cause.

### 19. Tools partition cleanly into `read/` and `act/`

The `@tool()` decorator declares safety properties (`local`, `readonly`, `side_effects`, `trusted_output`); `validate_package()` enforces that `read/` tools have no side effects and `act/` tools have at least one unsafe property. `trusted_output=False` is load-bearing for taint tracking, not documentation.

### 20. Every caught exception leaves a traceback

Every `except Exception` (and bare `except:`) in `carpenter/` must capture the traceback somewhere a human can find it. A site is compliant if any of:

- The body calls `logger.exception(...)` or any logger method with `exc_info=True`.
- The body re-raises (`raise` or `raise SomethingElse from e`).
- The body records the exception into durable, surfaced state (e.g. arc history) that includes the type **and** traceback — `str(e)` alone is not enough.

`pass`, `continue`, `return <default>`, DEBUG-only logs without `exc_info`, and message-only `logger.warning(f"failed: {e}")` all silently discard the traceback and are non-compliant.

Intentional swallowing is fine, but the catch must still log at INFO+ with `exc_info=True` and a one-line comment explaining why we don't bubble. Long-running loops and trigger / notification / webhook handlers that swallow on purpose must also include identifying context (arc id, trigger name, webhook URL) in the log line so the failure is diagnosable later.

This invariant exists because traceback loss is the single most expensive class of latent bug we hit in this codebase: the symptom shows up far from the cause, and reconstructing the stack from a `str(e)` is often impossible.
