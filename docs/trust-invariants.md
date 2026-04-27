# Trust Invariants

Security invariants that Carpenter's trust boundary system must maintain.
Each invariant has a unique ID, a prose statement, the file(s) responsible
for enforcement, and a pointer to the targeted test(s).

For the formal lattice formulation and relationship to FIDES/CaMeL, see
the [Carpenter website](https://carpenter-ai.org/docs/trust/).

---

## I1 — No CHAT/PLANNER context contains raw untrusted tool output

**Statement:** The return value of `submit_code` and `get_execution_output`
must never contain raw execution output when the executed code imports
untrusted tool modules (currently `carpenter_tools.act.web`).

**Enforcement:**
- `invocation.py` `_execute_chat_tool()` submit_code branch — returns
  metadata-only string when `taint_source` is set.
- `invocation.py` `_tool_get_execution_output()` — withholds log output when
  code uses untrusted imports.

**Tests:** `tests/test_taint_invariants.py::TestI1`

---

## I2 — Trusted arcs cannot access untrusted data tools

**Statement:** Arcs with `integrity_level='trusted'` are denied access to
`arc.read_output_UNTRUSTED` and `arc.read_state_UNTRUSTED` — unless the arc's
agent type has the `can_read_untrusted` capability (REVIEWER, JUDGE).

**Enforcement:**
- `api/callbacks.py` `_UNTRUSTED_DATA_TOOLS` check — returns HTTP 403 when a
  trusted arc without `can_read_untrusted` capability attempts to call an
  untrusted-data tool.
- `core/trust_types.py` `AGENT_CAPABILITIES` — REVIEWER and JUDGE arcs have
  `can_read_untrusted=True`.

**Tests:** `tests/test_taint_invariants.py::TestI2`

---

## I3 — Only path from untrusted to trusted is JUDGE approval

**Statement:** Trust promotion (changing `integrity_level` from `'untrusted'`
to `'trusted'`) can only be performed by a JUDGE arc's deterministic policy
checks via `_check_and_promote()`. JUDGE arcs run platform code (not LLM
agents) — they execute deterministic policy checks against configured allowlists.

**Enforcement:**
- `core/review_manager.py` `_check_and_promote()` — only promotes when the
  approving verdict comes from an arc with `agent_type='JUDGE'`.
- `security/judge.py` `run_policy_checks()` — deterministic policy validation.
- `core/arc_dispatch_handler.py` `_run_judge_checks()` — JUDGE arcs are
  intercepted at dispatch time and run platform code instead of LLM agents.

**Tests:** `tests/test_taint_invariants.py::TestI3`, `tests/security/test_judge.py`

---

## I4 — Non-trusted arcs only created in batches with reviewers

**Statement:** An individual `arc.create()` call with
`integrity_level='untrusted'` or `'constrained'` is rejected. Non-trusted
arcs must be created via `arc.create_batch()` which validates that at least
one REVIEWER or JUDGE arc is included.

**Enforcement:**
- `core/arc_manager.py` `create_arc()` — raises `ValueError` when
  `integrity_level` is non-trusted. Internal batch-builders use the
  unchecked `_insert_arc` directly after running batch-level validation
  (reviewer coverage, single judge, judge-highest-order).
- `core/integrity.py` `is_non_trusted()` — returns True for both
  `constrained` and `untrusted` levels.
- `tool_backends/arc.py` `handle_create_batch()` — validates batch includes
  reviewer arcs when non-trusted arcs are present.

**Tests:** `tests/test_taint_invariants.py::TestI4`, `tests/core/test_constrained_level.py`

---

## I5 — Parent arcs stay trusted when orchestrating non-trusted children

**Statement:** Creating non-trusted child arcs does NOT change the parent's
integrity level. Parents remain trusted because they never process
non-trusted data — I2 (HTTP 403 on UNTRUSTED data tools) is the real
enforcement.

**Enforcement:**
- I2 (HTTP 403) prevents trusted arcs from reading non-trusted data.
- `core/arc_manager.py` `add_child()` — no upward propagation.
- `tool_backends/arc.py` `handle_create_batch()` — no upward propagation.

**Tests:** `tests/test_taint_invariants.py::TestI5`

---

## I6 — Judge approval promotes only the target arc

**Statement:** When a JUDGE approves a non-trusted child arc, only that arc's
`integrity_level` changes to `'trusted'`. The parent arc was never
non-trusted and stays trusted.

**Enforcement:**
- `core/review_manager.py` `_check_and_promote()` — UPDATE WHERE clause
  targets only `target_arc_id`.

**Tests:** `tests/test_taint_invariants.py::TestI6`

---

## I7 — Non-trusted arc state encrypted at rest; only designated reviewers decrypt

**Statement:** State written to non-trusted arcs (integrity_level `'untrusted'`
or `'constrained'`) is encrypted with a Fernet key that is only shared with
designated reviewer arcs. When `encryption.enforce=true` (default), arc
creation fails if encryption is unavailable.

**Enforcement:**
- `core/trust_encryption.py` — Fernet encrypt/decrypt.
- `core/state.py` — encrypts values for non-trusted arcs at write time.
- `tool_backends/arc.py` `handle_create_batch()` — generates Fernet keys and
  stores them in `review_keys` table; fails closed when `encryption.enforce`
  is true and cryptography library is missing.

**Tests:** `tests/test_integration_trust.py::test_full_trust_lifecycle`,
`tests/core/test_constrained_level.py::TestConstrainedEnforcement::test_constrained_state_encrypted`

---

## I8 — CONSTRAINED data cannot influence control flow without deterministic check

**Statement:** Data with integrity_level `'constrained'` cannot drive planner
decisions (arc creation, tool invocation, workflow branching) unless it has
been validated through a deterministic policy check against a trusted
reference (security allowlist).

**Enforcement:**
- `security/judge.py` `run_policy_checks()` — validates constrained extraction
  data against platform security policies (default-deny allowlists).
- `security/policies.py` `SecurityPolicies.validate()` — per-type validation
  functions (email, domain, url, filepath, command, int_range, enum, bool,
  pattern).
- `core/arc_dispatch_handler.py` — JUDGE arcs run platform code, not LLM agents.
- `core/integrity.py` — CONSTRAINED level is enforced identically to
  UNTRUSTED for all access control checks (conservative default).

**Tests:** `tests/security/test_judge.py`, `tests/security/test_policies.py`,
`tests/test_taint_invariants.py::TestI8`

---

## I9 — Policy-typed literals must validate against security policies

**Statement:** When submitted code compares CONSTRAINED data against a literal
value, the literal must be wrapped in a policy-typed class (Email, Domain,
Url, etc.). In verification mode, the constructor validates the literal
against the platform's configured security policies. All security policies
default to deny (empty allowlists).

**Enforcement:**
- `carpenter_tools/policy/types.py` — Policy-typed literal classes that validate
  against platform policies when `CARPENTER_VERIFICATION_MODE=1`.
- `carpenter_tools/policy/_validate.py` — Executor-side RPC to platform's
  `policy.validate` endpoint.
- `carpenter/tool_backends/policy.py` — Platform-side handler that checks
  values against `SecurityPolicies` singleton.
- `security/policy_store.py` — DB-backed CRUD for security allowlists with
  version tracking.

**Tests:** `tests/security/test_policy_types.py`, `tests/tool_backends/test_policy.py`,
`tests/test_taint_invariants.py::TestI9`

---

## I10 — Chat tools have enforced trust boundaries and capabilities

**Statement:** All chat tools declare a `trust_boundary` (`chat` or `platform`)
and a `capabilities` list via `@chat_tool` decorators in Python modules under
`config/chat_tools/`. Chat-boundary tools may only have read capabilities.
Platform boundary is restricted to `PLATFORM_TOOLS` frozenset (`submit_code`,
`escalate_current_arc`, `escalate`) — user config cannot create platform tools.

**Enforcement:**
- `chat_tool_loader.py` — `@chat_tool` decorator validates at decoration time
- `chat_tool_registry.py` `PLATFORM_TOOLS` — frozenset allowlist
- `chat_tool_registry.py` `validate_tool_defs()` — load-time and hot-reload validation
- `coordinator.py` — calls `load_chat_tools()`, raises RuntimeError on failure

**Tests:** `tests/test_chat_tool_registry.py`, `tests/test_chat_tool_loader.py`

---

## Implementation Status

Invariants I1-I10 are fully implemented and tested. The verified flow analysis
(`verified-flow-analysis.md`) — which would add static AST taint tracking
to enforce I8/I9 at the code level rather than relying on the review pipeline —
is designed but not yet implemented.
