# Phase 2 — Quarantined Quality Reviewer (Implementation Plan)

*Investigation + design only. Do not implement from this doc without an explicit go-ahead.*

Date: 2026-04-29
Author: subagent draft for Ben

---

## 0. Architect question (ask first)

Before coding, please confirm one of two readings of "Phase 2: quarantined quality reviewer":

**(A) `submit_code` pipeline phase** — A new stage *inside*
`carpenter/review/pipeline.py`, between sanitisation and the existing reviewer
LLM call (or replacing it for the security-focused branch), in which the
reviewer LLM runs in a *quarantined invocation context* (no tools, fixed
system prompt, no chat-history bleed-through, no raw payload bytes), producing
an advisory signal that combines with the deterministic phases.

**(B) Verification-sibling phase** — A new (or hardened) instance of the
existing `verify-quality` REVIEWER sibling created by
`carpenter/core/arcs/verification.py::create_verification_arcs`, in which the
quality-check arc runs in a quarantined integrity profile (no untrusted reads,
no chat tools, no `submit_code` execution path) so the LLM that judges the
diff cannot itself be steered by tainted data it accidentally reads.

The two interpretations are *not* mutually exclusive — the verification-sibling
quality check (B) already exists but is loosely defined; the submit-time
pipeline (A) is the in-process gate. The "high benefit, real gap" framing from
prior triage most closely matches **(A)**, because the existing code path
already has a deterministic taint/whitelist verifier (`verify_code`) and an
*unquarantined* LLM reviewer (`review_code` / `review_code_adversarial`), but
no formally quarantined quality LLM that runs without conversation context
leakage.

This plan assumes **(A) submit-pipeline phase** as primary, with notes on how
it composes with (B). If you want (B) instead, the plan reduces to "tighten
`create_verification_arcs` quality branch + add invariants" — much smaller.

---

## 1. Map of the current review pipeline

### 1.1 Submit-time pipeline (`carpenter/review/pipeline.py`)

`run_review_pipeline(code, conversation_id, profile, arc_id)` runs these
ordered steps. Numbering from the docstring:

| # | Stage | Profile | Type | Outcome |
|---|---|---|---|---|
| 1 | Hash check (per-conversation in-memory cache) | both | deterministic | CACHED |
| 1b | Formal verification (`verify_code` → whitelist + string declarations + taint + dry-run) | STEP only | deterministic | REJECTED if `hard_reject`; APPROVE on `verified` |
| 2 | `import *` check | both | deterministic | REJECTED |
| 3 | Syntax validation | both | deterministic | REWORK |
| 4 | Injection pattern scan (`analyze_injection_risk`) | STEP | heuristic, advisory | flags only |
| 4b | Histogram LLM analysis (`analyze_histogram_with_llm`) | STEP | LLM, advisory | flags only |
| 4c | Progressive text review for UnstructuredText literals | STEP | LLM | MAJOR escalate |
| 5 | Sanitisation (`sanitize_for_review`) | STEP | deterministic AST | wraps for step 6 |
| 6 | Reviewer LLM (`review_code` / `review_code_adversarial`) — sees sanitised code + **full conversation history** | STEP | LLM | APPROVE/MINOR/MAJOR |
| 6' | Intent reviewer (`review_code_for_intent`) — sees raw code + conversation | PLANNER | LLM | APPROVE/MINOR/MAJOR |
| 7 | `determine_outcome` aggregation | both | deterministic | final mapping |

Final precedence: when `verification_result.verified` is True, the LLM verdict
becomes *advisory* and the outcome is APPROVE regardless. When verification
ran but did not verify, MAJOR is forced (human decides). When verification is
disabled / errored, the LLM verdict is authoritative — this is the *gap*.

### 1.2 Verification-sibling pipeline (`carpenter/core/arcs/verification.py`)

After a coding-change arc completes, the platform creates four sibling arcs:
`verify-quality` (REVIEWER, only for platform/tool code), `verify-correctness`
(REVIEWER), `judge-verification` (Python-only), `post-verification-docs`
(EXECUTOR). The judge is deterministic; quality and correctness are LLM
agents. They share `parent_id` with the implementation arc and carry
`verification_target_id`.

### 1.3 What is *already* quarantined

- **JUDGE arcs** are deterministic Python; intercepted at dispatch
  (test I8 in `test_taint_invariants.py:438`); no LLM is invoked.
- **Sanitiser** (`sanitize_for_review`) strips literals, comments,
  user-defined names — the structural defence the synthesis doc emphasises.
- **REVIEWER agents** for web extraction (`fetch_web_content` flow) run as
  trusted children that read tainted resources but only emit metadata.

### 1.4 The actual gap

The submit-time reviewer LLM (`review_code` in `code_reviewer.py`) is given
**`messages = conversation_mod.get_messages(conversation_id)`** —
i.e. the *full chat history*. If any prior message in that conversation
contains attacker-controlled bytes (a quoted webpage, a webhook payload
relayed into chat, an `[Advisory]` system message echoing a verdict reason
that itself contains injection prose), the reviewer LLM sees them and can be
*steered* into approving. The sanitiser only protects the **submitted code**;
it does not protect the **conversation context** that surrounds the reviewer.

That is the load-bearing gap. Prior triage's framing — "quarantined quality
reviewer, high benefit, real gap" — names exactly this asymmetry: the
*sanitiser* quarantines the code, but the *reviewer's broader context* is
not quarantined.

---

## 2. Design — Quarantined Quality Reviewer (QQR)

### 2.1 What QQR is

A **second LLM reviewer** that runs in a strict quarantine envelope:

| Dimension | Standard reviewer (today) | QQR (proposed) |
|---|---|---|
| Sees sanitised code | yes | yes |
| Sees raw conversation history | yes (`get_messages`) | **no** — sees only the user's most recent *trusted* request, distilled by deterministic platform code |
| Sees prior tool outputs | yes (interleaved in messages) | **no** |
| Sees advisory flag text from earlier stages | yes (concatenated) | **only fixed-form severity tags**; flag *descriptions* are dropped or hashed |
| Has any tool access | no (text in/text out today) | **explicitly no** — same |
| System prompt | reviewer prompt + arc context | **fixed prompt**, no string interpolation from runtime data |
| Model | configurable | constrained to a vetted set in config (`review.qqr.allowed_models`) |
| Output schema | free-form `status` + `reason` | **strict JSON schema** validated by Pydantic; reason field truncated to N chars and stripped of suspicious tokens before display |
| Cache | shared with main reviewer | **separate** cache; QQR APPROVE never short-circuits the main reviewer |

### 2.2 Integrity level

QQR runs at **trusted (T)** integrity. It must run trusted because its
verdict influences a control-flow decision (whether to APPROVE / REWORK /
MAJOR). What "quarantined" prevents is not promotion — it is *contamination
of the trusted reviewer's context by tainted history*.

This matches the synthesis principle: *"Never put an LLM on a trust
boundary."* QQR is **not** on the U→T boundary (only deterministic JUDGE
sits there). QQR is a *T-on-T* defence-in-depth check whose value comes
from receiving a tightly bounded input distribution.

### 2.3 What QQR produces

A `QqrSignal` with:

- `verdict` ∈ {APPROVE, MINOR, MAJOR, ABSTAIN}
- `category` ∈ {safety, correctness, scope, style, none}
- `confidence` ∈ {low, medium, high} (model-reported, advisory)
- No free-form `reason` exposed to other LLMs; a UI-only `reason_html` is
  HTML-escaped and tagged `[QQR]` before being rendered.

### 2.4 Composition with deterministic phases

The `determine_outcome` aggregator (lines 390–433) is extended:

```
inputs: verification_result, injection_flags, main_reviewer, qqr_signal
output: ReviewOutcome
```

Composition rules (deterministic table, not LLM-driven):

| `verify_code` | main reviewer | QQR | aggregate |
|---|---|---|---|
| `verified` | any | any | APPROVE (QQR/main are advisory only — current behaviour preserved) |
| `hard_reject` | n/a | n/a | REJECTED |
| ran but not verified | APPROVE | APPROVE | MAJOR (forced human, current behaviour preserved) |
| ran but not verified | APPROVE | MAJOR | MAJOR |
| ran but not verified | MAJOR | APPROVE | MAJOR |
| ran but not verified | MAJOR | MAJOR | MAJOR |
| disabled / errored | APPROVE | APPROVE | APPROVE *(only when both agree, narrower than today)* |
| disabled / errored | APPROVE | MAJOR | MAJOR |
| disabled / errored | MAJOR | APPROVE | MAJOR |
| disabled / errored | MINOR | any | REWORK |
| disabled / errored | any | ABSTAIN | fall back to today's main-reviewer-only path with audit log entry |

The new behaviour is on the **disabled/errored verification** rows: today,
disabled-verification falls through to the main reviewer alone. Under QQR
both reviewers must concur for APPROVE; either MAJOR is MAJOR. This
*narrows* the auto-approve surface without expanding it — it can only
introduce false MAJORs (which surface to the human), never silently
approve more than today.

### 2.5 What QQR does *not* try to be

- Not a U→T promotion mechanism (that is JUDGE only — invariant I3).
- Not a replacement for `verify_code` (CaMeL flow analysis remains
  authoritative when applicable).
- Not a name-reconstruction reviewer (Phase 4 — *shelved*).
- Not the final-verdict reviewer (Phase 5 — needs typed-declarations
  enforcement first).
- Not an `act/`-tool-using agent. No tool calls at all.

---

## 3. Concrete implementation changes

### 3.1 New / modified files

**Add:**

- `carpenter/review/qqr.py` — `QqrSignal` dataclass; `run_qqr(sanitized_code,
  trusted_summary, advisory_severities) -> QqrSignal`. Hardcoded system
  prompt. Uses a separate Anthropic invocation path that does not pass the
  conversation messages.
- `carpenter/review/_summarize.py` — deterministic distillation of the
  user's *trusted* request from the conversation. For chat-driven submits
  this is the **most recent user message text**, validated as
  `UnstructuredText` (string-declarations contract) and truncated. For
  arc-step submits it is `arc.goal` (T by definition). No LLM, no recursion
  through history.
- `tests/review/test_qqr.py` — invariant tests (see §4).

**Modify:**

- `carpenter/review/pipeline.py` — add step 6b that calls `run_qqr` after
  the existing reviewer; thread `qqr_signal` into `determine_outcome`.
  Update `PipelineResult` with optional `qqr_signal` field.
- `carpenter/review/profiles.py` — add `run_qqr: bool = False` (default off)
  and set `True` on `PROFILE_STEP`. PROFILE_PLANNER stays as-is (planner
  string literals are T; QQR is unnecessary noise).
- `carpenter/config.py` — register `review.qqr.{enabled, model_policy_id,
  allowed_models, fail_closed}` keys with safe defaults.
- `carpenter/api/review.py` — render the QQR verdict + sanitised reason in
  the human-review panel alongside the main reviewer's verdict.
- `docs/review-outcomes-reference.md` — note the new dual-reviewer rule for
  the disabled/errored verification fallback row.

### 3.2 No new arc template required

QQR is *in-process* during `submit_code`; it does not create an arc. This
keeps the change small and avoids new lifecycle surface. (If we later want
the verification-sibling quality reviewer (B) hardened in the same way, that
is a follow-up; the `qqr.py` module would be reusable from
`agent_type='REVIEWER'` step prompts via a shared `QQR_PROMPT` constant.)

### 3.3 Integration point

Single touchpoint inside `run_review_pipeline`, after the existing reviewer
LLM call (line ~298):

```python
qqr_signal: QqrSignal | None = None
if profile.run_qqr and not profile.intent_review_only:
    trusted_summary = summarize_trusted_request(conversation_id, arc_id)
    qqr_signal = run_qqr(
        sanitized_code,
        trusted_summary,
        [f["severity"] for f in injection_flags],   # severities only, no descriptions
    )
```

`determine_outcome` gains an optional `qqr_signal` parameter and the
composition table from §2.4. All today's call sites continue to work
because the parameter defaults to `None`.

### 3.4 Test strategy

- **Unit:** `test_qqr.py` covers (i) prompt does not interpolate untrusted
  data, (ii) `summarize_trusted_request` returns only the most recent user
  message and rejects non-T sources, (iii) `QqrSignal.from_model_output`
  rejects malformed JSON (fail-closed → ABSTAIN).
- **Integration:** extend `tests/review/test_security_pipeline.py` with
  scenarios where an injection in chat history *would* steer the main
  reviewer to APPROVE but QQR (which doesn't see the history) flags MAJOR
  → aggregate is MAJOR. Mock both LLM calls.
- **Invariant:** add **I11** to `tests/test_taint_invariants.py`:
  *"The QQR LLM call sees no message content other than (a) the sanitised
  code and (b) the trusted distilled summary. Construct a conversation with
  injected prose; assert the bytes do not appear in the QQR call payload."*
  Verified by capturing the Anthropic client request via a fixture.
- **Aggregation table:** parametrised test enumerating every row of the
  §2.4 table.

Run via `~/bin/run-tests tests/review/ tests/test_taint_invariants.py -q`.

---

## 4. Risk analysis

### 4.1 Invariants that must continue to hold

| Invariant | Risk | Mitigation |
|---|---|---|
| **I1** No raw untrusted output reaches a trusting AI | QQR is itself a trusting AI; if its summariser silently included a tainted message, I1 breaks | `summarize_trusted_request` whitelists *only* the most recent user message and `arc.goal`; both are T by construction. Unit-tested. |
| **I2** Trusted arcs cannot read untrusted Resources | QQR runs at T but does not call any tools; cannot read Resources at all | n/a — no tool surface |
| **I3** U→T only via JUDGE | QQR is advisory; it cannot promote integrity | `determine_outcome` does not write `integrity_level` |
| **I4** Untrusted arcs only via `arc.create_batch` | QQR doesn't create arcs | n/a |
| **I5** Parents stay trusted | QQR is in-process | n/a |
| **I6** JUDGE scope is target-only | QQR is not JUDGE | n/a |
| **I7** Non-trusted state encrypted at rest | QQR doesn't write state | n/a |
| **I8** CONSTRAINED can't drive control flow | QQR's verdict drives control flow but its inputs are T-only | enforced by §3.3 summariser whitelist |
| **I9** Policy-typed literals validate against allowlists | If we later let config drive QQR system prompt, this becomes a concern | system prompt is a Python constant, not config-derived |
| **I10** Chat tools have enforced trust boundaries | QQR is not a chat tool | n/a |
| **proposed I11** | the new property; tested as above |

### 4.2 Interaction with `file_provenance` (PR #293)

QQR does no file I/O — it is a pure text-in / text-out LLM call. The
file_provenance mechanism is orthogonal. The relevance is conceptual: D10
is a *reader-side* defence (a trusted reader cannot accidentally ingest
bytes a non-trusted writer planted); QQR is a *reviewer-side* defence (a
trusted reviewer cannot be steered by bytes a tainted history slipped
into its prompt). The two are complementary and share the design
principle "trusted LLMs only see T-validated bytes."

### 4.3 Failure modes

- **QQR LLM unreachable / timeout** — fail-closed: signal is `ABSTAIN`.
  Aggregate falls back to today's behaviour with an audit log entry on
  `arc_history` (`qqr_abstain`). The narrowed-approval table only
  *upgrades* outcomes to MAJOR; it never downgrades. So an outage cannot
  silently approve more code than today.
- **QQR returns malformed JSON** — same as above (ABSTAIN, audit log).
- **Both reviewers disagree pathologically** — by design, MAJOR wins.
  This may surface false-positive human-review prompts; metric to track in
  the chat UI advisory summary.
- **Cost** — adds one LLM call per non-cached, non-PLANNER submission.
  Use the `fast-chat` model preset by default; cost-bound via existing
  model-policy machinery.

### 4.4 Things this plan deliberately does not change

- The `verify_code` precedence (machine-verified APPROVE wins over both
  reviewers — preserves invariant *"deterministic checks on hard
  boundaries"*).
- The MAJOR-forced human review when `verification_result.verified` is
  False but verification ran.
- The `import *` and syntax checks.
- The progressive text reviewer (4c).
- `fetch_web_content` REVIEWER arcs.

---

## 5. Sizing

**Medium.** Estimated 700–1100 lines including tests, single PR.

Breakdown:

- `qqr.py` + `_summarize.py`: ~200 lines.
- `pipeline.py` + `profiles.py` + `determine_outcome` changes: ~80 lines.
- `config.py` keys + `api/review.py` rendering: ~60 lines.
- Tests (unit + integration + invariant + aggregation table): ~400–600
  lines — most of the volume, intentionally.
- Doc updates (`review-outcomes-reference.md`): ~30 lines.

This fits in a single PR if the architect-question answer is **(A)** as
assumed. If the answer is **(B)** (verification-sibling tightening only),
the size drops to ~250 lines (one PR, small).

If the answer is "both", split into two PRs: PR1 = QQR submit-time (this
plan), PR2 = verification-sibling quarantine envelope sharing
`qqr.QQR_PROMPT` and `_summarize`.

---

## 6. Open follow-ups (out of scope for Phase 2)

- **Phase 4 (name reconstruction)** remains shelved; QQR does not depend
  on it.
- **Phase 5 (final reviewer)** still needs typed-declarations enforcement
  first; QQR's strict JSON output schema is a small step in that direction
  but does not satisfy the precondition.
- Verification-sibling quality-check arc could re-use `QQR_PROMPT` to
  harden interpretation (B) — file as a follow-up issue.
- Consider promoting `summarize_trusted_request` to a shared helper for any
  future trusted-only-context LLM calls (e.g. reflection synthesis).
