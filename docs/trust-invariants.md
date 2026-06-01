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

## I2 — Trusted arcs cannot read untrusted Resources

**Statement:** Arcs with `integrity_level='trusted'` can only read *trusted*
Resources — those derived via a reviewed template pipeline whose
`template_verdict` has been set to `approved`. Raw Resources (produced with
`produced_by_template=NULL`) and Resources whose `template_verdict` is still
`pending` or `rejected` are refused by the chat `read_resource` tool with an
`untrusted` message, and their byte contents are never surfaced to the
caller. Trusted arc state is reached through the structural tools
(`arc.get`, `arc.get_history`, `arc.get_plan`, etc.), which only expose
fields that cannot carry raw tool output. Untrusted data flows across the
boundary only via the review-arc pipeline: an untrusted arc writes a
Resource, a REVIEWER (and optionally a JUDGE) runs against it, and only an
approved verdict unlocks the content for trusted downstream readers.

**Enforcement:**
- `config_seed/chat_tools/resources.py` `read_resource` — refuses raw
  Resources (`produced_by_template=NULL`) and any Resource whose
  `template_verdict` is not `approved`; never echoes the body in the
  refusal message.
- `core/resources/trust.py` `is_trusted()` — the predicate the chat tool
  uses to decide whether to surface content.
- `core/resources/manager.py` `derive_resource()` and `mark_template_verdict()`
  — only a reviewed template pipeline can produce an `approved` Resource.

**Tests:** `tests/test_taint_invariants.py::TestI2`,
`tests/core/resources/test_read_resource_tool.py`

---

## I3 — Only path from untrusted to trusted is JUDGE approval

**Statement:** Trust promotion (changing `integrity_level` from `'untrusted'`
to `'trusted'`) can only be performed by a JUDGE arc's deterministic policy
checks via `_check_and_promote()`. JUDGE arcs run platform code (not LLM
agents) — they execute deterministic policy checks against configured allowlists.

The JUDGE-dispatch wrapper reads the REVIEWER's pending extraction Resource
via `read_resource_content(caller_arc_id=None)` (the platform-introspection
path). Passing the JUDGE arc's id would be refused by the I2 defence-in-depth
gate because JUDGE arcs are `integrity_level='trusted'` and a pending Resource
has derived trust `'untrusted'`. The JUDGE handler is the *only* mechanism
that promotes the Resource via `mark_template_verdict('approved')` — reading
the bytes to make the verdict decision is part of that privileged operation,
not a tainted downstream read.

**Enforcement:**
- `core/review_manager.py` `_check_and_promote()` — only promotes when the
  approving verdict comes from an arc with `agent_type='JUDGE'`.
- `security/judge.py` `run_policy_checks()` — deterministic policy validation;
  reads the REVIEWER's pending Resource and flips `template_verdict` to match
  the result via `mark_template_verdict()`.
- `core/arcs/dispatch_handler.py` `_run_judge_checks()` — JUDGE arcs are
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

### I7 threat model and storage details

The "encrypted at rest" claim above is bounded by these concrete
assumptions. Operators should read this section before relying on I7 for
confidentiality against host-level attackers.

**1. `review_keys.fernet_key_encrypted` column name is aspirational.**
The column stores the *raw* Fernet key bytes, not a key-encrypted-by-
another-key blob. See `core/trust/encryption.py::generate_arc_key()`,
which inserts the output of `Fernet.generate_key()` directly, and
`tool_backends/state.py::_get_arc_fernet_key()`, which reads the bytes
back and passes them straight to `Fernet(...)`. The column name dates
from an earlier design that anticipated wrapping keys; the wrapping was
never implemented. The encryption boundary is therefore the SQLite file
itself.

**2. Trust boundary is the `platform.db` file.**
The threat model assumes that read access to `platform.db` (and its
`*-wal` / `*-shm` sidecars) is restricted to operating-system principals
that are already trusted by the platform. Any process that can read the
file can read the Fernet keys in `review_keys` and decrypt every
ciphertext stored in `arc_state.value_json` for non-trusted arcs.

I7 protects against:
- A misconfigured backup destination that captures `arc_state` but not
  `review_keys` (the encrypted column is useless without the key table).
- An in-process bug or sandbox escape that exposes a single arc's
  ciphertext but does not have direct DB read access.

I7 does NOT protect against an attacker with filesystem read access to
the data directory.

**3. Default file mode is 0644 (SQLite default).**
`carpenter/db.py::init_db()` calls `os.makedirs()` and lets SQLite open
the file with its default permissions, which on Linux respect the
process umask but typically result in mode 0644 (world-readable). On a
single-user host this matches the trust model. **On a multi-user host
the operator must restrict the data directory** (e.g. `chmod 700
~/carpenter/data && chmod 600 ~/carpenter/data/platform.db*`) or use
filesystem-level encryption (LUKS, dm-crypt). The platform does not
enforce this and does not check it at startup.

**4. `db_encryption_key` (SQLCipher) is optional defense-in-depth.**
When `db_encryption_key` is set in config and `pysqlcipher3` is
installed, the entire SQLite file is encrypted at rest using SQLCipher,
which means the Fernet keys in `review_keys` are also at-rest encrypted.
This raises the bar from "filesystem read access" to "filesystem read
access plus the SQLCipher passphrase". It is *not* the primary I7
boundary — I7 is enforced even when SQLCipher is disabled, but only
against the limited threats listed in (2). SQLCipher integration fails
closed if `db_encryption_key` is set without `pysqlcipher3` installed,
so that an operator who intended to enable disk encryption cannot
silently get plaintext on disk.

**5. In-memory plaintext is out of scope.**
I7 only covers state at rest in `arc_state.value_json`. Plaintext values
are present in memory in the platform process when state is written, in
tool backend params during dispatch, and (as of this writing) in
`RestrictedExecutor.ExecutionResult.dispatch_log` for the lifetime of an
`ExecutionResult` object. Anyone with read access to the platform
process's memory (a debugger attached to the running daemon, a core
dump, swap on an unencrypted disk) can read plaintext.

**Storage of dispatched tool params.** `executor/restricted.py` builds
a `dispatch_log` list on each `ExecutionResult`. This log is held in
process memory and discarded when the `ExecutionResult` is garbage
collected — no production caller persists it. The log entries currently
include `params` and `result` in plaintext, including untrusted arc
`state.set(value=...)` values. If future code persists `dispatch_log`
to disk, journal, or DB, it must redact `params` and `result` for
non-trusted arcs or the I7 guarantee is broken.

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

## Coding-time enforcement

The invariants above are enforced at **runtime** — `create_arc()` rejects bare
non-trusted creates (I4), `arc_dispatch_handler.py` intercepts JUDGE arcs to
run platform code (I3, I8), `tool_backends/arc.py` `handle_create_batch()`
checks reviewer/judge coverage. Runtime enforcement is the line of last
defence and is not optional.

A second line runs at **coding time**, before non-trusted templates and steps
are ever instantiated. The coding agent's finalization hook re-verifies every
file it edited or wrote, and refuses to end its turn while any verifier
returns `ok=False`:

- `carpenter/verify/registry.py` — content-type-keyed registry. Each content
  type registers a callable `(content, context) -> VerificationResult`.
  Unknown types pass through; registration is opt-in.
- `carpenter/verify/yaml_template.py` — verifier for
  `config_seed/templates/*.yaml`. Enforces the trust topology demanded by I3
  and I4 *in the template text*: every `integrity_level: untrusted` EXECUTOR
  must have downstream REVIEWER (`reviewer_profile: security-reviewer`) and
  JUDGE (`reviewer_profile: judge`) sibling steps in correct `order`,
  `output_type: json` is pinned, agent-type/integrity-level pairs are
  compatible, and goal-placeholder substitution inside fenced code blocks is
  flagged.
- `carpenter/agent/coding_agent.py` `_verify_touched_files()` — fired when
  the coding agent emits `end_turn`. Findings are fed back as a follow-up
  message identical in shape to a `submit_code` rejection. Bounded by
  `MAX_VERIFY_REJECTS` so a stuck agent escalates instead of looping.

The pattern generalises. A new content type — KB articles, prompt files, JSON
schemas — plugs in with `register_verifier(content_type, fn)` plus a branch
in `detect_content_type()`. The verifier returns
`VerificationResult(ok=..., findings=[VerificationFinding(severity, line,
message, fix_hint), ...])`. `severity="error"` blocks finalization;
`"warning"` is informational. `fix_hint` should point to the canonical KB
article, mirroring `string_declarations.py` error messages — the coding
agent reads it and edits the file accordingly.

See `config_seed/kb/security/coding-time-verification.md` for the
agent-facing rule sheet.

## Implementation Status

Invariants I1-I10 are fully implemented and tested. The verified flow analysis
(`verified-flow-analysis.md`) — which would add static AST taint tracking
to enforce I8/I9 at the code level rather than relying on the review pipeline —
is designed but not yet implemented.
