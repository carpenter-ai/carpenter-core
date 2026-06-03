# D5 — Forge-provider injection — Implementation Plan

*Investigation + design only. No code changes in this document.*

Anchored to D5 in `/home/pi/2026-04-29_leadership.md`: forge providers live in carpenter-core under `carpenter/forges/`, registered via `register_forge_provider(name, impl)`, default selected by config key `forge`.

---

## 1. Map of current Forgejo coupling

Files reviewed; references categorized.

### A. True forge interactions (need provider abstraction)

| File | Lines | Coupling |
|------|------:|----------|
| `carpenter/tool_backends/forgejo_api.py` | 312 | All 9 handlers — PR create/list/merge/close/get/get_diff/post_review + create/delete repo webhook. URLs all `{base}/api/v1/repos/...`. Hardcodes `"base": "main"` (PR base branch) at line 40. Hardcodes `"type": "forgejo"` in webhook payload at line 267. |
| `carpenter/tool_backends/git.py` | 284 | Pure-git via dulwich. Forgejo-flavored only via `_git_server_url()` / `_git_server_headers()` (lines 38–54) — they read `git_server_url` + `git_token` from config. Hardcodes `_GIT_IDENTITY = b"Carpenter <carpenter@localhost>"` (line 25). Hardcodes `b"main"` as upstream branch in `handle_create_branch` (line 202) and `handle_commit_and_push` rebase target (line 256, 258). |
| `carpenter/tool_backends/webhook.py` | 174 | `handle_subscribe`/`handle_delete` call `forgejo_api_backend.handle_create_repo_webhook` / `handle_delete_repo_webhook` directly. Default `source_type = "forgejo"` (line 51). Conditional `if source_type == "forgejo"` (line 64) means non-forgejo subscriptions skip remote registration — silent feature gap for GitHub. |
| `carpenter/core/workflows/external_coding_change_handler.py` | 363 | Imports `forgejo_api as forgejo_api_backend` (line 25); calls `handle_create_pr` directly (line 309) with positional Forgejo-shaped payload. Also has `/tmp` workspace fallback (line 67) — orthogonal to D5 but flagged in prompt. |
| `carpenter/core/workflows/pr_review_handler.py` | 431 | Imports `forgejo_api_backend` (line 24); calls `handle_get_pr`, `handle_get_pr_diff`, `handle_post_pr_review` directly. The whole pr-review pipeline is shaped around Forgejo's review verb (`APPROVED`/`REQUEST_CHANGES`/`COMMENT`) — happens to match GitHub. |
| `carpenter/core/workflows/webhook_dispatch_handler.py` | 323 | `_parse_forgejo_payload` extracts PR/repo/branch fields. `_PARSERS = {"forgejo": _parse_forgejo_payload, "generic": _parse_generic_payload}`. No GitHub parser. Also — note conflict — this is a *legacy* dispatch path; the newer engine pipeline lives in `core/engine/triggers/webhook.py` and already has a github parser stub. |
| `carpenter/core/engine/triggers/webhook.py` | 178 | Already has `_parse_forgejo`, `_parse_github`, `_parse_generic`. Selected by per-trigger config `parser:` field. This is the *good* shape — D5 should pivot the rest of the system toward this. |
| `carpenter/api/callbacks.py` | — | Static `_DISPATCH` table maps 9 `git.*` callback ops to `forgejo_api_backend.*`. Lines 195–203 are the dispatch surface that executor-side `dispatch()` calls hit. |

### B. Token/credential plumbing

- `carpenter/config.py:400–415` — `_CREDENTIAL_MAP` maps `FORGEJO_TOKEN` → `git_token` (back-compat alias kept).
- `carpenter/config.py:639–650` — `_COMPAT_ALIASES` migrates old `forgejo_url`, `forgejo_token`, `forgejo_api_timeout`, `forgejo_api_long_timeout` → `git_*`. **Already neutralized** at config layer.
- `carpenter/api/credentials.py:101–133` — `verify_credential` for `GIT_TOKEN`/`FORGEJO_TOKEN` calls `{git_server_url}/api/v1/user`. Forge-specific endpoint shape; needs to delegate to provider.
- `config_seed/credential-registry.yaml:24–30` — `GIT_TOKEN` and `FORGEJO_TOKEN` (deprecated alias) registered.

### C. Config keys

- Active: `git_server_url`, `git_token`, `git_api_timeout`, `git_api_long_timeout`, `git_author_*`, `git_committer_*`.
- Deprecated-but-aliased: `forgejo_url`, `forgejo_token`, `forgejo_api_timeout`, `forgejo_api_long_timeout`.
- **Missing:** `forge` (the provider-selector key D5 mandates). Has no callers today.
- Per MEMORY.md, renaming the deprecated keys is out of scope for this work.

### D. Test fixtures + assertions

- `tests/core/test_webhook_dispatch.py` — calls private `_parse_forgejo_payload`; asserts `sub["source_type"] == "forgejo"`. Needs to follow whatever literal we settle on.
- `tests/tool_backends/test_webhook_backend.py` — `test_subscribe_creates_subscription_and_forgejo_hook`, `test_subscribe_forgejo_error`, etc. Patches `carpenter.tool_backends.forgejo_api.httpx`. After Phase B these should patch the provider module instead.
- `tests/core/test_pr_review_handler.py` — `monkeypatch.setattr(handler.forgejo_api_backend, ...)` in many tests.
- `tests/core/test_external_coding_change_handler.py` — `patch.object(handler.forgejo_api_backend, "handle_create_pr", ...)`.
- `tests/core/test_trigger_pipeline.py` — `_parse_forgejo` test (already in the engine path; will not need to move).
- `tests/core/test_template_triggers.py` — uses literal event names `forgejo.pr.opened`/`forgejo.pr.closed`. These are user-facing event-bus identifiers; safest to keep stable and add `github.pr.opened` alongside.
- `tests/test_config.py`, `tests/test_setup_credential.py`, `tests/api/test_credentials.py` — exercise the back-compat alias path; should keep passing with no changes if compat aliases are preserved.

### E. User stories (carpenter-linux)

- `s015_external_repo_setup.py`, `s016_external_code_change_pr.py`, `s017_webhook_pr_review.py` — heavily Forgejo-flavored: `CARPENTER_TEST_FORGEJO_URL` / `CARPENTER_TEST_FORGEJO_TOKEN` env vars, manipulate `forgejo_url` in `config.yaml`. Stories test the credential intake flow with `FORGEJO_URL` / `FORGEJO_TOKEN`. Out-of-scope for Phase A/B (the chat agent+intake path is what's tested, not the provider abstraction itself), but worth noting these will need a parallel GitHub story when provider-C lands.

### F. Tool wrappers (chat-tool advertisements, mostly docstring drift)

- `carpenter_tools/act/git.py:33,40,47` — docstrings say "via Forgejo API". Should soften to "via configured forge".
- `carpenter_tools/act/webhook.py:25`, `carpenter_tools/read/webhook.py:14` — `source_type` parameter docs.
- `carpenter_tools/act/credentials.py:17,29,44` — example string `'FORGEJO_TOKEN'`.
- KB `config_seed/kb/git/tools.md`, `config_seed/kb/credentials/intake.md`, `config_seed/kb/skills/failure-patterns/network.md` — agent-facing docs mention Forgejo. Update in Phase B together with the call-sites.

### G. Dead-code-ish

- The entire `carpenter/core/workflows/webhook_dispatch_handler.py` `_PARSERS` map is the *legacy* dispatch path. The newer triggers/event-bus pipeline (`core/engine/triggers/webhook.py`) is preferred. Phase A/B should not double-implement — see §4.

---

## 2. `ForgeProvider` protocol

Narrow surface. The shape below is the union of what `pr_review_handler` + `external_coding_change_handler` + `webhook.py` actually call today, plus webhook parsing.

```python
# carpenter/forges/protocol.py (new file in Phase A)
from typing import Protocol, Optional

class ForgeEvent:
    """Normalized webhook event shape (already partially in
    core/engine/triggers/webhook.py — promote to this module)."""
    source_type: str          # "forgejo" | "github" | ...
    event_type: str           # "pull_request" | "push" | "issues" | ...
    action: str               # "opened" | "synchronize" | ...
    delivery_id: Optional[str]
    repo_owner: str
    repo_name: str
    pr_number: Optional[int]
    pr_title: str
    pr_body: str
    pr_state: str
    head_branch: str
    base_branch: str
    html_url: str
    raw: dict                 # the original body, for blob storage

class ForgeProvider(Protocol):
    """Abstraction over a git-forge SaaS/self-hosted instance."""

    name: str                            # "forgejo" | "github"
    default_base_branch: str             # config-driven; usually "main"

    # ---- PR lifecycle (return shape: dict, error key on failure) ----
    def create_pr(self, *, repo_owner: str, repo_name: str,
                  branch_name: str, fork_user: str,
                  pr_title: str, pr_body: str,
                  base_branch: Optional[str] = None) -> dict: ...
    def list_prs(self, *, repo_owner, repo_name, state="open") -> dict: ...
    def get_pr(self, *, repo_owner, repo_name, pr_number) -> dict: ...
    def get_pr_diff(self, *, repo_owner, repo_name, pr_number) -> dict: ...
    def merge_pr(self, *, repo_owner, repo_name, pr_number,
                 merge_method="merge") -> dict: ...
    def close_pr(self, *, repo_owner, repo_name, pr_number,
                 comment: Optional[str] = None) -> dict: ...
    def post_pr_review(self, *, repo_owner, repo_name, pr_number,
                       body: str, event: str,
                       comments: Optional[list] = None) -> dict: ...

    # ---- Repo webhook lifecycle ----
    def create_repo_webhook(self, *, repo_owner, repo_name,
                            target_url: str, events: list,
                            secret: str = "",
                            content_type: str = "json") -> dict: ...
    def delete_repo_webhook(self, *, repo_owner, repo_name,
                            hook_id) -> dict: ...

    # ---- Webhook ingress ----
    def parse_webhook(self, headers: dict, body: dict) -> ForgeEvent: ...
    def verify_webhook_signature(self, headers: dict, raw_body: bytes,
                                 secret: str) -> bool: ...
        # Forgejo: HMAC-SHA256 in X-Gitea-Signature / X-Forgejo-Signature
        # GitHub:  HMAC-SHA256 in X-Hub-Signature-256 (sha256= prefix)
        # Same primitive, different header → encapsulate.

    # ---- Identity helpers ----
    def verify_token(self, *, server_url: str, token: str) -> dict: ...
        # Today this is api/credentials.verify_credential's body.
```

**Deliberately excluded:**

- Pure-git operations (clone, branch, commit, push, fetch, rebase). Those stay in `tool_backends/git.py` and are dulwich-only — provider-agnostic. The provider only owns the *forge API* layer.
- Token/credential file paths. Per the constraint, the config layer holds those; the provider receives `server_url` and `token` via injection or via reading the same `config.CONFIG["git_server_url"]` / `config.CONFIG["git_token"]` keys it does today.
- Repo URL construction. Today `external_coding_change_handler` is given `repo_url`/`fork_url` from upstream callers. Keep that — the provider should not invent URLs.
- Author/committer identity. That's a git concern (commit metadata), not a forge concern. It belongs in `git.py` config (already partly via `git_author_*` config keys).

**Open architect question (not blocking the PR):** should `parse_webhook` and the dispatch parsers (`webhook_dispatch_handler._PARSERS`) be unified? The trigger pipeline already has its own forgejo/github parsers. Best answer is: have *one* parser per provider, lifted onto the protocol, and have both legacy-dispatch and the new trigger pipeline call into it. See §4.

---

## 3. Phasing plan

**Recommendation: A and B in a single PR, C as a follow-up.**

- A alone is invisible to all callers — pure refactor. Without B, no existing code uses the new shape, so reviewers have no executable signal that the abstraction is correct. Combining shrinks the testing surface (one diff, one set of mocks updated) and avoids landing a half-used abstraction in main.
- C is genuinely separable and has its own design surface (GitHub auth quirks, app-vs-PAT, signature header differences, merge_method values). Land it after the user actually needs it.

### Phase A+B (single PR)

**Goal:** introduce `carpenter/forges/` + `register_forge_provider`, register Forgejo as default, route every existing call-site through the registry. **Zero behavior change.**

Steps:

1. **New module tree:**
   - `carpenter/forges/__init__.py` — exposes `register_forge_provider(name, impl)`, `get_forge_provider(name=None)`, internal `_REGISTRY: dict[str, ForgeProvider]`. `get_forge_provider(None)` returns the provider named by `config.CONFIG["forge"]`, defaulting to `"forgejo"` when the key is absent (back-compat).
   - `carpenter/forges/protocol.py` — the `ForgeProvider` Protocol + `ForgeEvent` dataclass.
   - `carpenter/forges/forgejo.py` — implementation. Body lifted from current `tool_backends/forgejo_api.py`. Gains: `parse_webhook` (lifted from both legacy `_parse_forgejo_payload` and the trigger-pipeline `_parse_forgejo`, deduplicated), `verify_webhook_signature` (HMAC-SHA256 with `X-Forgejo-Signature` / `X-Gitea-Signature`), `verify_token` (lifted from `api/credentials.verify_credential`'s git path).

2. **Registration:** at module import (`carpenter/forges/__init__.py`), eagerly `register_forge_provider("forgejo", ForgejoProvider())`. This mirrors the platform-injection pattern but is simpler — Forgejo ships with core, and external providers can call `register_forge_provider` later without touching core. (If "implicit registration on import" feels wrong, lift it to `server.py` startup alongside `set_platform`.)

3. **Config:** add `forge` key to `DEFAULTS` with default value `"forgejo"`. Also expose `forge_default_base_branch` (default `"main"`) — needed because `forgejo_api.handle_create_pr` and `git.py` both hardcode `"main"`. Reading order: arc state override → provider default → config default. Document but don't yet rename `git_server_url` / `git_token` (per MEMORY.md).

4. **Rewire callers:**
   - `carpenter/api/callbacks.py:195–203` — replace `forgejo_api_backend.handle_*` with thin lambdas that resolve the provider per-call: `"git.create_pr": lambda p: get_forge_provider().create_pr(**p)`. (Or build the dispatch table at server startup. The lambda form lets tests monkey-patch `get_forge_provider` without touching `_DISPATCH`.)
   - `carpenter/tool_backends/webhook.py` — replace `forgejo_api_backend.handle_create_repo_webhook(...)` with `get_forge_provider(sub.source_type).create_repo_webhook(...)`. Drop the `if source_type == "forgejo"` gate; instead check whether the resolved provider exists.
   - `carpenter/core/workflows/external_coding_change_handler.py:25,309` — drop direct import; use `get_forge_provider().create_pr(...)`. The base-branch parameter wires through arc state (with config fallback).
   - `carpenter/core/workflows/pr_review_handler.py:24,83,102,323` — same swap. Provider name should come from arc state (set when the webhook subscription fired) rather than from global config, so a Forgejo PR review still works on a GitHub-default install.
   - `carpenter/core/workflows/webhook_dispatch_handler.py` — replace `_PARSERS` dict with `get_forge_provider(source_type).parse_webhook(...)` *or* keep `_PARSERS` but populate it from the registry at module import. Either way, `_parse_forgejo_payload` body is deleted (it lives on `ForgejoProvider.parse_webhook`).
   - `carpenter/core/engine/triggers/webhook.py` — replace `_PARSERS = {"forgejo": ..., "github": ...}` with `get_forge_provider(parser_name).parse_webhook(...)`. The `_parse_github` stub stays in core/engine until Phase C, *or* moves now into a placeholder `forges/github.py` that only implements `parse_webhook`. (Author preference: do it now — it's <30 lines and lets us drop the stub from triggers.)
   - `carpenter/api/credentials.py:101–133` — the git/forgejo branch becomes `get_forge_provider().verify_token(server_url=..., token=...)`. Keep the `("GIT_TOKEN", "FORGEJO_TOKEN")` env-var detection — that's a credentials concern, not a provider one.

5. **Delete `carpenter/tool_backends/forgejo_api.py`** once all imports are gone. Equivalent functionality now lives in `carpenter/forges/forgejo.py`. Keep `tool_backends/git.py` (pure git).

6. **Webhook subscription `source_type` field.** Today this defaults to `"forgejo"`. Change default to `config.CONFIG.get("forge", "forgejo")`. The DB column stays string-valued — same shape — but the value is now provider-name from the registry rather than a hardcoded literal.

7. **Tests.** Update mocks: tests that did `patch("carpenter.tool_backends.forgejo_api.httpx")` should now `patch("carpenter.forges.forgejo.httpx")`. Tests that did `monkeypatch.setattr(handler.forgejo_api_backend, "handle_get_pr", ...)` should patch via `monkeypatch.setattr("carpenter.forges._REGISTRY", {"forgejo": fake_provider})` or via a `register_forge_provider("forgejo", FakeProvider())` setup fixture. Add a test fixture `fake_forge_provider` in `conftest.py`.

8. **No data migration.** DB schema for `webhook_subscriptions` is unchanged.

**Phase A+B exit criteria:**
- Grep finds no `forgejo_api` import in the codebase.
- `carpenter/forges/forgejo.py` is the *only* file that knows the forgejo URL shape.
- All existing tests pass after mock updates.
- `config.yaml` with no `forge` key still works.

### Phase C (separate, deferrable PR)

Add `carpenter/forges/github.py`. Design surface:

- HTTPS base: `https://api.github.com` (configurable for GHE).
- Auth header: `Authorization: Bearer <token>` (vs Forgejo's `token <token>`).
- PR shape: same `head: "user:branch"` convention (good).
- Merge: `PUT /repos/{o}/{r}/pulls/{n}/merge` with `{"merge_method": "merge"|"squash"|"rebase"}`.
- Webhook signature: `X-Hub-Signature-256: sha256=<hmac>` — same primitive, different header. `verify_webhook_signature` shape covers it.
- Webhook parser: see existing stub at `core/engine/triggers/webhook.py:92`.

Phase C also adds a `GITHUB_TOKEN` entry to `credential-registry.yaml`. The selector is config: `forge: github`.

---

## 4. Webhook angle

**Current state — two paths:**

1. **Legacy:** `core/workflows/webhook_dispatch_handler._PARSERS` keyed by `source_type`. Used by the `webhook.received` work-queue handler that fires when an HTTP webhook lands at `/api/webhooks/{webhook_id}` (a route owned by `webhook_subscriptions` rows). Looks up subscription by `webhook_id`, reads `source_type` from the row, dispatches to `_parse_forgejo_payload` or `_parse_generic_payload`.

2. **New:** `core/engine/triggers/webhook.WebhookTrigger` — generic `EndpointTrigger` with per-trigger `parser: forgejo|github|generic` config. Lands on `/triggers/{name}`. Already provider-shaped; just needs to look up parser from registry instead of hardcoded `_PARSERS` dict.

**Architect note:** these paths exist in parallel because the trigger pipeline is newer (PR #111, 2026-04-03). The legacy `webhook.received` path is still active for `webhook_subscriptions` rows. D5 should not attempt to merge them — that's a separate refactor. D5 only swaps both paths' parsers to the provider registry.

**Sketch of change:**

```python
# webhook_dispatch_handler.handle_webhook_received
sub = get_subscription(webhook_id)
provider = get_forge_provider(sub["source_type"])  # was _PARSERS lookup
if provider is None:
    logger.warning("No provider for source_type '%s'", sub["source_type"])
    return
parsed = provider.parse_webhook(headers={}, body=data)
# (legacy path doesn't pass headers; provider.parse_webhook tolerates {} and
#  reads what it can from body alone.)
if event_filter and parsed.event_type not in event_filter:
    return
... # rest unchanged
```

```python
# core/engine/triggers/webhook.WebhookTrigger.handle_request
provider = get_forge_provider(self.config.get("parser", "generic"))
parsed = provider.parse_webhook(headers, body)
event_id = self.emit(emits, parsed.as_dict(), idempotency_key=...)
```

Signature verification (currently absent in the legacy path, partially absent in the trigger path) should be added in Phase A+B if it's a small lift, or filed as a follow-up. **Architect question:** is webhook signature verification load-bearing today, or aspirational? If aspirational, defer to a security-relevant follow-up PR (per the synthesis, security-relevant changes are human-gated by default and shouldn't ride along with a refactor).

---

## 5. Risk analysis

### Tests that depend on `source_type="forgejo"` literally

- `tests/core/test_webhook_dispatch.py:113,…` — `assert sub["source_type"] == "forgejo"` × 4. Passes after refactor as long as the row literal is preserved.
- `tests/tool_backends/test_webhook_backend.py` — multiple `source_type: "forgejo"` literals in fixtures and assertions. Passes unchanged.
- `tests/core/test_template_triggers.py:59,60,129` — event bus event types `forgejo.pr.opened` / `forgejo.pr.closed`. **Stable user-facing identifiers.** Do NOT rename these to `pr.opened`; templates in user `config.yaml` reference them.

**Verdict:** no acceptance test will break from the refactor proper; only mock-target paths change.

### Webhook signature verification

- Forgejo: HMAC-SHA256 in `X-Forgejo-Signature` (or `X-Gitea-Signature` depending on version). Hex-encoded.
- GitHub: HMAC-SHA256 in `X-Hub-Signature-256`, with `sha256=` prefix. Hex-encoded.
- Same primitive (HMAC-SHA256), trivially different header naming and value framing.
- Single `verify_webhook_signature(headers, raw_body, secret) -> bool` method per provider hides the differences.
- **Not a fork.** Provider abstraction handles it.

### Token storage

- Today: `git_token` config key, populated from `GIT_TOKEN` or `FORGEJO_TOKEN` env vars (back-compat alias).
- After Phase A+B: provider receives `server_url` + `token` via the same config keys. **Zero change to credential file paths.**
- For GitHub (Phase C): add `GITHUB_TOKEN` to `_CREDENTIAL_MAP` and registry, mapped to (proposal) `github_token`. Provider reads the *correct* config key for itself: Forgejo provider reads `git_token`, GitHub provider reads `github_token`. The provider, not the config layer, knows which key it wants — but it does *not* know about file paths.
- **Risk to flag:** today's single `git_token` key won't work if a user wants both forges configured simultaneously (e.g. mirror PRs across both). Out of D5 scope; flag for D5-followup.

### Other risks

- **Module import cycles.** `carpenter/forges/forgejo.py` will need `httpx` and `config`. It must NOT import `tool_backends/*` (they import config and would create a cycle through callbacks.py). Keep `forges/` a leaf module like `tool_backends/web.py`.
- **Eager registration vs explicit injection.** Eager registration on import diverges slightly from `set_platform()`/`register_executor()` (which are called by platform packages at startup). Acceptable because forgejo ships with core. If we want symmetry, move registration to `server.run_server()` startup. Either is fine; flag for the implementing agent.
- **Tests that import `forgejo_api` directly.** All hits are in the file map above. Updating them is mechanical but voluminous (~6 test files).
- **`tool_backends/git.py` hardcoded `b"main"` and identity bytes.** Surface in this PR or follow-up; recommendation: move `default_base_branch` and `git_author_*` reads into git.py, leave hardcoded fallbacks. Don't expand scope further.

---

## 6. Out of scope

- GitHub provider implementation (Phase C — separate PR).
- Renaming `forgejo_url` / `forgejo_token` config keys (per MEMORY.md, separate task).
- Renaming the `forgejo.pr.opened` event-bus event names (user-facing, breaks templates).
- Migrating active repos from Forgejo to GitHub.
- Unifying the legacy `webhook_dispatch_handler._PARSERS` and the trigger-pipeline `WebhookTrigger` (separate refactor).
- Webhook signature verification rollout (file as security-relevant follow-up if not already enforced).
- Multi-provider simultaneous config (single `git_token` key today; flag as D5-followup).
- Removing `/tmp` workspace fallback in `external_coding_change_handler.py:67` (mentioned in prompt but orthogonal to D5; better as a config-hygiene PR).
- Updating KB docs (`config_seed/kb/git/tools.md` etc.) — should ride with Phase A+B as docstring/doc cleanup but is not load-bearing.

---

## Implementation checklist (for the executing agent)

- [ ] Create `carpenter/forges/{__init__.py, protocol.py, forgejo.py}`.
- [ ] Lift body of `tool_backends/forgejo_api.py` → `forges/forgejo.py` as methods on `ForgejoProvider`.
- [ ] Lift `_parse_forgejo_payload` (legacy) and `_parse_forgejo` (trigger) into one `ForgejoProvider.parse_webhook`.
- [ ] Lift `verify_credential`'s git branch into `ForgejoProvider.verify_token`.
- [ ] Add `forge` and `forge_default_base_branch` to `DEFAULTS` in `config.py`.
- [ ] Rewire `api/callbacks.py:195–203`, `tool_backends/webhook.py`, `core/workflows/external_coding_change_handler.py`, `core/workflows/pr_review_handler.py`, `core/workflows/webhook_dispatch_handler.py`, `core/engine/triggers/webhook.py`, `api/credentials.py`.
- [ ] Delete `carpenter/tool_backends/forgejo_api.py`.
- [ ] Update test patches and fixtures (~6 test files).
- [ ] Soften docstrings in `carpenter_tools/{act,read}/{git,webhook,credentials}.py`.
- [ ] Verify with `~/bin/run-tests tests/ -q`.

End of plan.
