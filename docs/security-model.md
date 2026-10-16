# Carpenter Security Model: Read-Only Agency + Pythonic Action

## Overview

Carpenter uses a "read-only agency, pythonic action" security model:

- **Read-only tools** are available for direct agentic use (file reads, state queries, arc introspection)
- **All actions** (file writes, state mutations, web requests, arc management, git operations) must go through Python code submitted via `submit_code` for security review

## Defense Layers

| Layer | What It Does |
|-------|-------------|
| **Tool partitioning** | Only read-only, local-only tools exposed to agent via tool_use |
| **Code as action boundary** | All side effects require Python code submission |
| **String stripping** | Remove all string literals from submitted code before review |
| **Variable renaming** | Rename user-defined symbols to a, b, c... before review |
| **Reviewer AI** | Configurable model reviews sanitized code for intent alignment |
| **Context isolation** | Reviewer sees only sanitized code + user-visible conversation |
| **Injection scan** | Pattern-based detection (advisory flags for reviewer) |
| **AST validation** | Syntax check before proceeding |
| **Callback enforcement** | Action tool callbacks require valid reviewed execution session ID |
| **Execution isolation** | Subprocess with minimal environment |

## Tool Partitioning

Tools are organized into two packages under `carpenter_tools/`:

### `carpenter_tools/read/` — Safe, direct access

| Module | Functions |
|--------|-----------|
| `files` | `read(path)`, `list_dir(directory)` |
| `state` | `get(key, default)`, `list_keys()` |
| `arc` | `get(arc_id)`, `get_children(arc_id)`, `get_history(arc_id)` |
| `messaging` | `ask(question)` — solicits user input |

### `carpenter_tools/act/` — Requires reviewed code submission

| Module | Functions |
|--------|-----------|
| `files` | `write(path, content)` |
| `state` | `set(key, value)`, `delete(key)` |
| `arc` | `create(...)`, `add_child(...)`, `update_status(...)`, `cancel(...)` |
| `web` | `get(url)`, `post(url, data=...)` |
| `git` | `setup_repo(...)`, `create_branch(...)`, `commit_and_push(...)`, `create_pr(...)`, etc. |
| `scheduling` | `add_cron(...)`, `remove_cron(...)`, `list_cron()`, `enable_cron(...)` |
| `messaging` | `send(message)` — pushes content to user |

Each tool function has a `@tool()` decorator declaring `local`, `readonly`, and `side_effects` properties. The `validate_package()` function verifies that all tools in `read/` are safe and all tools in `act/` are marked as having side effects.

## Review Pipeline

When code is submitted via `submit_code`:

1. **Hash check** — identical previously-approved code skips review
2. **AST parse** — reject syntax errors early
3. **Injection scan** — advisory flags (not blocking) for suspicious patterns
4. **Sanitize** — strip strings, comments, docstrings; rename variables to a, b, c...
5. **Reviewer AI** — configurable model checks intent alignment against conversation

Results (see `review-outcomes-reference.md` for details):
- **CACHED** — identical code, review skipped
- **APPROVE** — code executes, result returned to agent
- **REWORK** — fixable issues, agent retries (up to 3 attempts)
- **MAJOR** — security concern, requires human decision
- **REJECTED** — policy violation, no retry

## Configuration

Add to `~/carpenter/config.yaml`:

```yaml
review:
  # Model for code review. Falls back to chat_model with a warning if not set.
  reviewer_model: "anthropic:claude-sonnet-4-6"
```

## Allowlists

Two allowlist mechanisms exist. They answer different questions and are deliberately separate.

| | `web_egress_allowlist` | `security.*_allowlist` (JUDGE policies) |
|---|---|---|
| Question | May a `web.*` tool connect to this destination? | May this CONSTRAINED value cross a trust boundary? |
| Default | Allow every public address; deny loopback, private, link-local and other non-global ones | Deny everything not listed |
| Entries | Exact hostnames, or CIDRs matched against the resolved addresses | Typed values: `email`, `domain` (exact or suffix), `url` (prefix), `filepath`, `command` |
| Enforced by | `security/egress.py`, on every request and redirect hop, in the platform process | The JUDGE (`security/policies.py`), deterministically, when a non-trusted arc's output is validated |
| Stored in | `config.yaml` only | `config.yaml` (`security.*`) as seed, plus the policy store in the database |

Listing a domain in `security.domain_allowlist` does not make it reachable by web tools, and listing a host in `web_egress_allowlist` does not make it pass a JUDGE. Merging the two would weaken both: the egress guard is default-allow for the public internet, so treating its entries as JUDGE policy values would be a default-allow judge, while treating JUDGE domains as egress entries would let a policy value (often editable at runtime) open a path to internal addresses.

Executor code itself cannot open network connections (the restricted executor refuses network imports), so there is no executor egress allowlist. The former `egress_policy` / `egress_enforce` keys had no effect and have been removed; setting them in `config.yaml` logs a warning.

## Read-Only Isolation: Capability, Not Path Confinement

Read-only agents operate on real files directly, with no workspace copy and no path boundary. The security invariant lives in the **tool capability tier**, not in filesystem confinement:

- `carpenter_tools/read/` tools can only read. Reading is inherently safe.
- No workspace copy means no boundary to enforce, and no boundary for future tools to accidentally miss.
- Filesystem-level access control (OS sandbox on Android, process user on Linux) handles "what can be seen" — the application layer doesn't duplicate this.
- The workspace manager (`workspace_manager.py`) is exclusively for mutable coding workflows where change tracking (git init, diff, apply/reject) justifies the copy overhead.

The callback handler enforces this via **default-deny session gating**: `_SESSION_EXEMPT_TOOLS` is an allow-list of read-only tools that don't require a reviewed execution session. Any tool not in this set requires a session by default. This means newly added tools are gated until explicitly exempted — fail-safe.

## Callback Enforcement

The platform callback API (`/api/callbacks/`) enforces that action tool callbacks include a valid execution session ID. Read-only tool callbacks work without a session ID.

### Execution Session Authentication

Action tool callbacks require a valid execution session ID. The platform is the sole source of truth for review status:

1. Harness reviews code and records approval in database
2. Harness generates unique session ID (UUID) before launching subprocess
3. Harness creates `execution_sessions` record linking: session → code_file_id → reviewed status
4. Executor receives session ID via `CARPENTER_EXECUTION_SESSION` environment variable
5. Executor includes session ID in `X-Execution-Session-ID` callback header
6. Platform validates: "Is this session ID registered and reviewed?"

**Security improvement over executor attestation:** The executor cannot spoof review status because:

- Session IDs are generated by the platform, not the executor
- Review status is determined by platform database lookup, not executor claims
- Sessions expire after 1 hour (configurable), preventing replay attacks
- Platform verifies session validity via database query, not environment variables

This is defense-in-depth. The primary security boundary remains the review pipeline. Session authentication prevents unreviewed code or expired sessions from invoking action tools.

Session records are retained indefinitely in the database for audit purposes.

**Defense layer table:**

| Layer | Prevents | Failure mode if absent |
|-------|----------|----------------------|
| **Review pipeline** | Bad code from running | Malicious code executes with full action access |
| **Execution sessions** | Unreviewed/expired code from acting | Code that bypasses review or has expired session could invoke action callbacks |
| **Callback token** | External callers from invoking callbacks | Any HTTP client on the network could call action tools |

## Inbound Channel Webhooks

Paths under `/hooks/` (channel connectors) and `/api/webhooks/` are exempt from the UI-token middleware, because the services that call them cannot carry the UI bearer token. Each handler must therefore authenticate its caller itself; the payload alone proves nothing.

**Signal (`rest_api` mode).** The sender number in a webhook payload is whatever the caller wrote, so the `allowed_numbers` allowlist only means something if the caller is known to be signal-cli-rest-api. Set the credential `SIGNAL_WEBHOOK_SECRET` (in `{base_dir}/.env` or the environment; config key `signal_webhook_secret`, never `config.yaml`) and give the same value to the webhook caller:

```
RECEIVE_WEBHOOK_URL=http://<carpenter-host>:<port>/hooks/signal?secret=<value>
```

The secret may also be sent as an `X-Webhook-Secret` header, which is preferable when the caller can set headers, since a query string can end up in HTTP access logs. Requests without the exact secret (constant-time comparison) get `401` and are never parsed or delivered. If the secret is unset, the webhook accepts every request, as before, and the connector logs a warning at start; set it on any deployment where the server's port is reachable by anything other than signal-cli-rest-api. Subprocess mode has no webhook and is unaffected.

**Other webhooks.** `/api/webhooks/{webhook_id}` is addressed by a 128-bit random id generated per subscription, which acts as the bearer secret. The Telegram connector's `webhook` mode (`/hooks/telegram`) does not yet verify Telegram's `X-Telegram-Bot-Api-Secret-Token` header.

## Adding New Tools

1. Determine if the tool is read-only or an action
2. Create the function in the appropriate package (`read/` or `act/`)
3. Add the `@tool()` decorator with correct safety properties
4. Add the callback handler in `tool_backends/`
5. Add the dispatch entry in `api/callbacks.py` `_DISPATCH`
6. If it's a **read tool**, add it to `_SESSION_EXEMPT_TOOLS` in `callbacks.py` (otherwise it will require a reviewed session by default — fail-safe)
7. If it's a read tool and should be available via tool_use, create a `@chat_tool`-decorated handler function in a Python module under `config_seed/chat_tools/`. Declare capabilities (e.g., `filesystem_read`, `database_read`, `pure`) and set `always_available=True` if the tool should always be offered to the agent.

Startup validation (`validate_tool_classification()`) catches stale entries in `_SESSION_EXEMPT_TOOLS` after tool removal. `validate_package()` catches `@tool()` metadata inconsistent with package placement.

### Trust Boundary Enforcement

Chat tool definitions are user-configurable Python modules in `config/chat_tools/`, loaded via `chat_tool_loader.py` using `@chat_tool` decorators. Each tool declares a `trust_boundary` and `capabilities` list (invariant I10):

- **`chat`** (default) — Read-only, internal state queries. May only declare read capabilities (`filesystem_read`, `database_read`, `kb_read`, `config_read`, `pure`).
- **`platform`** — Privileged operations. Restricted to `PLATFORM_TOOLS` frozenset allowlist (`submit_code`, `escalate_current_arc`, `escalate`). May declare write capabilities (`filesystem_write`, `database_write`, `arc_create`, `external_effect`).

`validate_tool_defs()` runs at startup and on hot-reload, rejecting tools with invalid boundaries, unknown capabilities, or write capabilities on chat-boundary tools. User config cannot create platform-boundary tools.

**Never add write/mutation tools as direct chat tools** — all actions must go through `submit_code` → review pipeline.

### Self-Modification Resistance

Platform tools (`submit_code`, `escalate`, `escalate_current_arc`) are hardcoded in `invocation.py` and cannot be overridden by user config modules. The `PLATFORM_TOOLS` frozenset in `chat_tool_registry.py` is the immutable allowlist. User-configurable chat tool modules are validated at load time — unknown capability strings are rejected, and write capabilities are structurally blocked for chat-boundary tools. Hot-reload validates before swapping and keeps the previous valid set on failure.
