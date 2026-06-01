# Capability packages: how to build one

> **Audience.** Package authors (people writing `carpenter-packages/<pkg>/`)
> and operators who install / uninstall packages on a Carpenter
> deployment.  Platform maintainers should read this in tandem with
> [`design.md`](design.md) §11 (D24) and [`trust-invariants.md`](trust-invariants.md).

This doc walks through the lifecycle of a capability package end to
end: what files you write, what the manifest declares, how the
platform loads each artifact at startup, and how the trust model
handles the artifacts at runtime.  It's deliberately concrete — every
section has a snippet you can copy from.

We use two running examples:

1. **`hello`** — the reference no-op package shipped in
   `~/repos/carpenter-packages/packages/hello/`.  Demonstrates the
   minimum (a chat tool, nothing else).
2. **A synthetic `email-triage` package** — illustrates the full
   stack: chat tools, an arc template, a JUDGE handler, a typed
   data model, KB articles, and trigger subscriptions.

Both examples target the **D24 stage 3b** loader (the platform code
in `carpenter/packages/loaders.py`).  Earlier stages (Phase A, 3a)
loaded a strict subset of these artifacts; section "Compatibility" at
the bottom summarizes what changed.

---

## 1. The package layout

A capability package is a directory containing a `manifest.yaml` and
the source files it references.  At a minimum:

```
packages/<pkg-name>/
  manifest.yaml
  README.md         # human-facing — describes what the package does
  tools.py          # chat-tool defs (if any)
  data_models.py    # @dataclass kinds for JUDGE inputs (if any)
  judges.py         # JUDGE handler functions (if any)
  templates/
    <template>.yaml         # arc template definition
    <template>/
      __init__.py           # optional step handlers (register_handlers)
  kb/
    <slug>.md               # KB articles (seeded into the KB store)
```

You can split modules however you want — the manifest references each
file by relative path.  Nothing in the package layout is *required*
beyond `manifest.yaml`; declare only the artifact types your package
ships.

For a real-world skeleton, copy `~/repos/carpenter-packages/packages/hello/`
and adapt.

---

## 2. The manifest

`manifest.yaml` is the contract between your package and the
platform.  The schema is defined in
`carpenter/packages/manifest.py` and validated on load.

### 2.1 Minimum manifest (`hello`)

```yaml
name: hello
version: "0.1.0"
description: Reference no-op capability package.
chat_tools:
  - tools.py
```

That's it — three required fields plus a list of chat-tool modules.

### 2.2 Full manifest (`email-triage`)

```yaml
name: email-triage
version: "0.3.0"
description: Email summarization with typed JUDGE oversight.

chat_tools:
  - tools.py             # registers fetch_email, send_email, etc.

data_models:
  - module: data_models.py
    kinds:
      - EmailExtraction   # @dataclass — JUDGE input shape

arc_templates:
  - name: email-triage
    yaml: templates/email-triage.yaml
    step_handlers: templates/email-triage/__init__.py  # optional

judge_handlers:
  - template: email-triage
    module: judges.py
    function: judge_email_triage

kb_articles:
  - slug: email/usage
    path: kb/usage.md
  - slug: email/forwarding-policy
    path: kb/forwarding-policy.md

trigger_subscriptions:
  - trigger: email.message_received
    template: email-triage
```

Every field above is declaration-only — the platform reads the
manifest at install time, validates security invariants, and
records the declarations in `installed_packages` /
`installed_packages_templates`.  The actual *loading* is done by the
registry at server-start, not by the manifest itself.

---

## 3. Lifecycle

### 3.1 Install (operator action)

The operator calls the `install_package` chat tool.  The chat agent
asks for human confirmation; on approval, the platform:

1. Reads `~/repos/carpenter-packages/packages/<source-name>/`.
2. Validates the manifest (schema + security invariants — see §6).
3. Computes a deterministic SHA-256 over the package tree.
4. Atomically swaps the validated tree into
   `~/carpenter/packages/<name>/`.
5. Records the install in `installed_packages` (with the hash) and
   declared template names in `installed_packages_templates`.

The package source-tree in `carpenter-packages` is **not** auto-loaded.
Only `~/carpenter/packages/` is on the registry's search path.

### 3.2 Server start (every restart)

`PackageRegistry.discover_and_register()` walks
`~/carpenter/packages/`, and for each manifest:

1. Verifies the on-disk hash matches `installed_packages.hash`
   (skips the package on mismatch — SD3 / SD6).
2. Calls `validate_manifest_security(manifest)` — see §6.
3. Imports the chat-tool modules into the
   `_carpenter_pkg_.<package>.chat_tools` namespace, registering
   each `@chat_tool` against the platform's chat-tool registry.
4. Calls `load_package_artifacts(manifest)` — the stage-3b
   entrypoint that wires templates, JUDGEs, data models, and step
   handlers into their respective platform registries.

If any artifact fails to load (e.g. a JUDGE handler with a wrong
signature), the platform logs the error to `load_errors` on the
`RegisteredPackage` and **continues**.  Other artifacts in the same
package still load.  Hash verification is the only hard gate — that
one is fatal for the package as a whole.

### 3.3 Uninstall (operator action)

`uninstall_package <name>` checks for blocking arcs, deletes the
on-disk tree, removes DB rows, and calls
`get_handler_registry().unregister_package(name)` to drop the JUDGE
and kind registrations from the running process.  Templates already
written to `workflow_templates` are *not* deleted (ongoing arcs may
reference them); operators can `DELETE FROM workflow_templates WHERE
name = ?` if they're sure no arcs need the template.

---

## 4. Writing each artifact type

### 4.1 Chat tools (`chat_tools:`)

`@chat_tool`-decorated functions in a Python module.  See the full
guide in [`docs/coding-guidelines.md`](coding-guidelines.md).  Quick
example:

```python
# packages/hello/tools.py
from carpenter.chat_tool_loader import chat_tool

@chat_tool(
    description="Say hello.",
    input_schema={"type": "object", "properties": {}, "required": []},
    capabilities=["pure"],
)
def pkg_hello_say_hello(tool_input, **kwargs):
    return "Hello from the hello package!"
```

**Naming.** Prefix tool names with your package short name to
avoid cross-package collisions (`pkg_<pkg>_<verb>`).  The registry
errors on collisions at load time.

**Capabilities.** Declare the security capabilities your tool
needs.  Packages **cannot** declare platform-boundary capabilities
(e.g. `db_admin`, `judge_internal`); the security validator
rejects them at install time.

### 4.2 Data models (`data_models:`)

A data model is a `@dataclass` whose instances are passed to JUDGE
handlers.  The dispatch wrapper deserialises raw bytes into the
declared kind and validates policy-typed fields against
`SecurityPolicies` *before* the handler runs (I3).

```python
# packages/email-triage/data_models.py
from dataclasses import dataclass
from carpenter_tools.policy.types import EmailPolicy

@dataclass
class EmailExtraction:
    summary: str
    forwarded_to: list[EmailPolicy]   # validated against SecurityPolicies
    confidence: float
```

Manifest declares the kind name (must be unique across all
installed packages and not clash with a platform kind):

```yaml
data_models:
  - module: data_models.py
    kinds:
      - EmailExtraction
```

Platform-reserved kind names are listed in
`carpenter/security/judge.py::_PLATFORM_KINDS`.  Today the only
reserved name is `PolicyCheckList` (used for the platform's default
JUDGE flow).

### 4.3 Arc templates (`arc_templates:`)

A YAML template plus optional step handlers.  Templates load via
`carpenter.core.engine.template_manager.load_template`, which writes
the template to the `workflow_templates` table.  See
[`docs/template-rigidity.md`](template-rigidity.md) for the schema
and the rigidity rules (no LLM-emitted control flow,
human-confirmation steps mandatory for trust transitions, etc.).

Manifest:

```yaml
arc_templates:
  - name: email-triage
    yaml: templates/email-triage.yaml
    step_handlers: templates/email-triage/__init__.py
```

Template names are flat and unprefixed across the platform.  The
loader rejects names in the `_PLATFORM_TEMPLATES` set
(`reflection`, `coding_change`, `egress`, …) and detects
cross-package collisions.

If `step_handlers:` is set, the loader imports the file and calls
`register_handlers(registry)`.  Whatever you register through that
registry is automatically *unregistered* on uninstall.

### 4.4 JUDGE handlers (`judge_handlers:`)

A pure-Python function that takes one positional argument (the
deserialised dataclass) and returns a verdict.

```python
# packages/email-triage/judges.py
from carpenter.security.judge import JudgeVerdict
from .data_models import EmailExtraction

def judge_email_triage(extract: EmailExtraction) -> JudgeVerdict:
    """Approve only if confidence is high and forwards stay in-org."""
    if extract.confidence < 0.85:
        return JudgeVerdict.reject("low confidence")
    return JudgeVerdict.approve()
```

Manifest:

```yaml
judge_handlers:
  - template: email-triage
    module: judges.py
    function: judge_email_triage
```

**Trust invariants.** Package JUDGE code never sees raw bytes,
arc state, the DB, or the filesystem.  The dispatch wrapper:

1. Looks up the kind declared by the producing template.
2. Deserialises the Resource into that kind.
3. Walks dataclass fields and validates every policy-typed value
   (`EmailPolicy`, `Domain`, `Url`, `FilePath`, `Command`)
   against the active `SecurityPolicies`.
4. Calls the handler with the validated dataclass.
5. Catches any exception from the handler and converts it to a
   rejection (so a buggy package can never crash the platform's
   JUDGE arc).

Handlers with the wrong signature (anything other than a single
positional arg) are rejected at registration time.

### 4.5 KB articles (`kb_articles:`)

Markdown files seeded into the KB store under your
package's namespace.  Slugs **must** start with `<pkg>/` — the
security validator enforces this.  See [`docs/security-model.md`](security-model.md)
for the namespacing rationale.

```yaml
kb_articles:
  - slug: email/usage
    path: kb/usage.md
```

### 4.6 Trigger subscriptions (`trigger_subscriptions:`)

Declare which trigger events should fan out to one of your
arc templates.  Trigger handlers are platform-shipped; packages can
only subscribe.

```yaml
trigger_subscriptions:
  - trigger: email.message_received
    template: email-triage
```

### 4.7 Credential requirements (`credential_requirements:`) — OAuth callback

If your package needs OAuth-protected APIs (Gmail, Calendar, Drive,
Slack, ...), declare the requirement in the manifest.  The platform
ships a generic OAuth 2.0 authorization-code flow at
`/api/oauth/callback/{flow_id}` that any package can drive — see
`carpenter/api/oauth.py`.

```yaml
credential_requirements:
  - kind: oauth
    provider: google
    env_key_prefix: GMAIL_OAUTH
    authorize_url: https://accounts.google.com/o/oauth2/v2/auth
    token_url: https://oauth2.googleapis.com/token
    scopes:
      - https://www.googleapis.com/auth/gmail.readonly
```

> **Why the field is named `credential_requirements`, not `credentials`.**
> The security validator (`carpenter/packages/security.py`) treats the
> bare top-level key `credentials` as a security violation — that key
> traditionally meant "package ships credential bytes", which is
> forbidden.  The `credential_requirements` field declares
> *requirements*, not bytes; the bytes always come from operator input
> via the existing one-time-credential-link flow.

After a successful round-trip the platform writes the following
keys to `{base_dir}/.env`:

| Env key                           | Source                          |
|-----------------------------------|---------------------------------|
| `<PREFIX>_ACCESS_TOKEN`           | Token-endpoint response         |
| `<PREFIX>_REFRESH_TOKEN`          | Token-endpoint response (if issued) |
| `<PREFIX>_TOKEN_EXPIRES_AT`       | Unix timestamp = now + `expires_in` |
| `<PREFIX>_TOKEN_TYPE`             | Usually `Bearer`                |

Operator-supplied credentials (collected via the existing one-time
credential link) are read from the same `.env`:

| Env key                | Set by operator                                |
|------------------------|------------------------------------------------|
| `<PREFIX>_CLIENT_ID`   | Provider's OAuth console                       |
| `<PREFIX>_CLIENT_SECRET` | Provider's OAuth console                     |
| `<PREFIX>_TOKEN_URL`   | Same as `token_url` above (used by `refresh_token`) |

**Initiating the flow.**  A chat tool in your package (e.g.
`pkg_email_authorize`) calls
`carpenter.api.oauth.start_flow(...)` with the manifest values plus
the operator-supplied `client_id` / `client_secret`, then surfaces
the returned `authorize_url` to the user via chat.  Example:

```python
from carpenter import config
from carpenter.api.oauth import start_flow

result = start_flow(
    provider="google",
    client_id=config.CONFIG["GMAIL_OAUTH_CLIENT_ID"],
    client_secret=config.CONFIG["GMAIL_OAUTH_CLIENT_SECRET"],
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    env_key_prefix="GMAIL_OAUTH",
    package_name="carpenter-email",
    extra_authorize_params={
        "access_type": "offline",   # Google: needed for refresh_token
        "prompt": "consent",
    },
)
# Surface result["authorize_url"] to the user.
```

The user opens the URL, grants the requested scopes, and the
provider redirects back to the platform's callback endpoint.  The
callback handler verifies the per-flow `state` token, exchanges the
auth code for tokens, and persists them to `.env` under the prefix
above.  No further work in the package.

**Refresh.**  When you detect a 401 from the provider (access token
expired), call `oauth.refresh_token("GMAIL_OAUTH")`.  This is a
deterministic helper — no LLM involvement — that POSTs the stored
refresh token to the configured `<PREFIX>_TOKEN_URL` and writes back
the new access token (and refresh token, if rotated).  Returns
`{"ok": True, ...}` on success or
`{"ok": False, "error": "..."}` on revoked-token / network errors.

**Status check.**  The chat agent can query
`oauth.package_oauth_status("GMAIL_OAUTH")` (returns a dict of
booleans for each expected env key) before deciding whether to
prompt the user to (re-)authorize.

**Configuration prerequisites.**  `start_flow` derives the redirect
URI from `config.CONFIG["public_base_url"]` unless caller passes
`redirect_uri=`.  A deployment serving over `https://carp.example.com`
should set `public_base_url` to that origin so the redirect URI
matches what's registered in the provider's OAuth console.

---

## 5. Testing your package

Run the platform unit suite from the package's parent worktree:

```bash
cd ~/repos/carpenter-core
~/bin/run-tests tests/packages/ -v
```

(NEVER `pytest` directly — see `~/.claude/projects/-home-pi/memory/MEMORY.md`.)

For end-to-end testing:

1. Install your package in a dev deployment via `install_package`.
2. Trigger an arc that exercises the template.
3. Observe the JUDGE handler verdict in `journalctl --user -u carpenter`.
4. Uninstall and reinstall to verify the lifecycle is clean.

---

## 6. Security invariants enforced at install / load

The platform refuses to install a package — and refuses to load
it at startup — if any of the following holds:

| Check | Rationale |
|---|---|
| Manifest schema fails to parse | Garbage in. |
| Chat tool declares platform-boundary capability (`db_admin`, `judge_internal`, …) | I10 — packages can't escalate. |
| Chat tool ships JUDGE-internal hooks | I3 — JUDGE is platform code. |
| Manifest pre-populates a policy allowlist | I9 — packages can't grant themselves trust. |
| KB slug doesn't start with `<pkg-name>/` | I7 — packages can't write to peer namespaces. |
| Bundled `.env` file | Prevents credential leakage. |
| Hash on disk doesn't match recorded install hash | SD3 / SD6 — tamper detection. |
| Template name in `_PLATFORM_TEMPLATES` | I10 — packages can't shadow platform templates. |
| Kind name in `_PLATFORM_KINDS` | I10 — packages can't redefine platform JUDGE input shapes. |
| Cross-package collision on template / kind / chat-tool name | Determinism — first install wins, second errors. |
| JUDGE handler signature ≠ exactly one positional arg | I3 — JUDGE handlers operate on a single typed dataclass. |

The full list lives in
`carpenter/packages/security.py::validate_manifest_security` plus
the per-loader checks in `carpenter/packages/loaders.py` and
`carpenter/packages/handler_registry.py`.

---

## 7. Compatibility & upgrade path

D24 stage 3a (PR #305): chat tools loaded from packages; templates,
JUDGEs, data models, KB articles, and trigger subs were
**declaration-only** (recorded in DB but not wired into runtime).
A back-compat shim auto-loaded packages from
`~/repos/carpenter-packages/packages/` so unmigrated packages kept
working.

D24 stage 3b (this PR): templates, JUDGEs, data models, and step
handlers wire into runtime.  The back-compat shim is **gone** — the
registry only loads from `~/carpenter/packages/`.  Operators must
`install_package hello` (and any other package they were relying
on) once at deploy time.

D24 stage 3c (planned): KB-article seeding into a package-scoped
namespace, and trigger-subscription wiring through the trigger
dispatch.  Today those manifest fields are still declaration-only.

---

## 8. Where to look in the platform

- `carpenter/packages/manifest.py` — schema, validators, dataclasses.
- `carpenter/packages/security.py` — install-time security checks.
- `carpenter/packages/installer.py` — copy-on-install + hash + DB rows.
- `carpenter/packages/registry.py` — discovery + per-package wiring.
- `carpenter/packages/loaders.py` — stage-3b runtime wiring.
- `carpenter/packages/handler_registry.py` — JUDGE / kind registry.
- `carpenter/security/judge.py` — dispatch wrapper that consults the
  package handler registry before falling back to the platform JUDGE.
- `config_seed/chat_tools/packages.py` — `install_package` /
  `uninstall_package` chat-tool entrypoints.

For deeper background:
[`docs/2026-05-01_d24-package-untrusted-templates-plan.md`](2026-05-01_d24-package-untrusted-templates-plan.md)
is the design doc; [`docs/trust-invariants.md`](trust-invariants.md)
covers the I-numbered invariants referenced above.
