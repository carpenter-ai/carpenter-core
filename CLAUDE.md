# Carpenter

Pure-Python AI agent platform using the CaMeL pattern.

## Repository Structure

- `carpenter/` — Platform package (kernel, read-only to agents at runtime)
  - `config.py` — YAML + `.env` + credential env var loading (4-layer precedence)
  - `coordinator.py` — Lifecycle management independent of HTTP
  - `db.py` — SQLite WAL connection, schema init
  - `schema.sql` — 25+ tables
  - `core/` — Work queue, event bus, main loop, arc manager, template manager, code manager, workspace manager
  - `agent/` — AI clients (Anthropic, Ollama, Tinfoil, Local), prompts, invocation loop, coding agent, reflection
  - `inference/` — Local inference server management (llama.cpp lifecycle)
  - `executor/` — Executor protocol, subprocess/Docker executors, network egress enforcement
  - `platform/` — OS abstraction (Platform protocol). Concrete implementations live in platform packages (e.g., `carpenter-linux`).
  - `api/` — FastAPI HTTP server, chat, callbacks, webhooks, web UI
  - `tool_backends/` — Platform-side callback handlers
  - `security/` — Information-flow security: judge, policies, policy store, trust taint tracking
  - `channels/` — Channel abstraction: base ABC, registry, channel connectors (web, Telegram, Signal), tool connectors
  - `connectors/` — Backward-compatible import shims (redirects to `channels/`)
  - `review/` — Security review pipeline
  - `prompts.py` — Prompt template loader (reads from `config_seed/prompts/`)
- `carpenter_tools/` — Executor-side package (thin RPC wrappers for callback tools)
  - `read/` — Safe, read-only tools — direct agentic access
  - `act/` — Action tools — requires reviewed code submission
  - `policy/` — Policy-typed literals for comparing CONSTRAINED data against trusted references
  - `tool_meta.py` — `@tool()` decorator and package validation
- `config_seed/` — All default/seed data (copied to `{base_dir}/config/` on first install)
  - `prompts/` — Default system prompt sections (markdown)
  - `chat_tools/` — Chat tool handler modules (`@chat_tool` decorated, loaded by `chat_tool_loader.py`)
  - `templates/` — Workflow template YAML files
  - `kb/` — Seed knowledge base entries
  - `coding-prompts/` — Coding agent prompt defaults
  - `coding-tools/` — Coding agent tool definitions
  - `prompt-templates/` — Jinja2 prompt templates (markdown)
  - `model-registry.yaml` — Model metadata registry
  - `credential-registry.yaml` — Credential env var mapping
  - `data_models/` — Shared Pydantic data models for inter-arc communication
- `tests/` — Test suite mirroring source structure
- `docs/` — Architecture, security model, and design documentation
- `install.sh` — Interactive installer

## Quick Start

```bash
bash install.sh               # Interactive setup
python3 -m carpenter_linux    # Start server (via platform package)
# Open http://localhost:7842
```

## Development

**Run tests (recommended):** `pytest tests/ -q`

The test suite uses pytest-xdist for parallel execution. The default `-n 3` is set in `pyproject.toml` (leaves one core free so the server stays responsive). Override with `pytest -n auto -q` (all cores) or `pytest -n 0 -q` (single-threaded).

All tests use isolated temp databases via the `test_db` autouse fixture in `conftest.py`. Database template caching provides fast initialization while maintaining complete test isolation.

## Architecture

See `docs/design.md` for the authoritative system design document.

## Key Patterns

- **Config**: 4-layer precedence in `config.py`: defaults < `config.yaml` < `{base_dir}/.env` (credential keys only) < standard env vars (`ANTHROPIC_API_KEY` etc.). `get_config(key)` is the preferred accessor. Module-level `CONFIG` dict; monkeypatched in tests via `monkeypatch.setattr("carpenter.config.CONFIG", {...})`. `_CREDENTIAL_MAP` is never mutated by `load_config()` to prevent test state leakage. Config values should be native YAML types (unquoted) — no auto-casting of quoted strings.
- **AI Providers**: `ai_provider` selects backend ("anthropic", "ollama", "tinfoil", "local"). `invocation.py` dispatches via `_get_client()`. All clients retry transient failures with exponential backoff + jitter. Per-provider circuit breaker fast-fails after consecutive failures. HTTP 429 handled by rate limiter, not circuit breaker.
- **API Standard Normalization**: `agent/api_standard.py` translates between Anthropic and OpenAI API formats. Two standards: `"anthropic"` (native) and `"openai"` (Ollama, llama.cpp, Tinfoil). `_call_with_retries()` normalizes all responses to canonical Anthropic-like format (`content` blocks, `stop_reason`, `usage.input_tokens/output_tokens`), converts tool definitions to provider format before calls, and formats tool results for message threading. The `api_standards` config dict maps providers to standards (defaults in `config.py`). To add a new provider: declare its standard in `api_standards`, implement `call()` with `tools` parameter, and register in `_get_client()`/`_get_provider_for_client()`.
- **Database**: SQLite WAL mode with Row factory. `get_db()` / `init_db()` in `db.py`. All tables use IF NOT EXISTS.
- **Tests**: Autouse fixture creates isolated temp DB per test. No network, no Docker. Tests mock subprocess/httpx where needed.
- **Security model**: Read-only agency + pythonic action. Agent uses read-only tools freely; all actions go through `submit_code` → review pipeline → execution. See `docs/security-model.md`.
- **Tool partitioning**: `carpenter_tools/read/` (safe, direct agentic access) vs `carpenter_tools/act/` (requires reviewed code). Each tool has `@tool()` metadata. `validate_package()` enforces consistency.
- **Trust boundaries**: Chat tools are `@chat_tool`-decorated Python functions in `config_seed/chat_tools/` (user-configurable, hot-reloadable). Each declares `trust_boundary` (`"chat"` default, read-only) and `capabilities` (e.g., `filesystem_read`, `database_read`, `pure`). Platform tools (`submit_code`, `escalate`, `escalate_current_arc`) are hardcoded in `invocation.py` with `trust_boundary="platform"`. `chat_tool_registry.py` provides validation. **Never add write/mutation tools as direct chat tools** — all actions must go through `submit_code` → review pipeline. This is invariant I10.
- **Model roles**: `model_roles` dict (slot → `provider:model` string). Resolution: `get_model_for_role(slot)` checks named slot → `default` slot → auto-detect from `ai_provider`. Per-arc model via `agent_config_id` FK to `agent_configs` table (deduplicated, immutable rows).
- **Review pipeline**: `review/pipeline.py` orchestrates hash check → AST parse → injection scan → sanitize → reviewer AI call. Optional adversarial mode requires minimum findings count.
- **Callback enforcement**: Action tool callbacks require valid reviewed execution session ID (UUID in `execution_sessions` table, injected as `CARPENTER_EXECUTION_SESSION` env var, validated via `X-Execution-Session-ID` header). Sessions expire after 1 hour. Read-only callbacks work without session.
- **Tool tiers**: Tier 1 = callback (HTTP POST to platform); Tier 2 = direct (pure Python in executor); Tier 3 = environment (credential injection).
- **Executor protocol**: `Executor` Protocol in `executor/base.py` with `SubprocessExecutor` and `DockerExecutor`. `get_executor()` factory. Sandbox config (UID, egress, rlimits) is internal to executor, not passed by code_manager.
- **Arc state machine**: pending → active → waiting/completed/failed/cancelled/escalated. Frozen statuses (completed/failed/cancelled/escalated) are immutable. Done statuses (completed/escalated) count for dependency purposes.
- **Arc auto-dispatch**: Arcs execute automatically when dependencies are satisfied. Three-part mechanism: work handler, `add_child()` immediate enqueue, heartbeat safety net. An arc is ready when status='pending' and all preceding siblings are completed.
- **Template rigidity**: Template-mandated arcs (`from_template=True`) are immutable. Exception: `template_mutable=True` allows agent-created children. Rigidity validation only counts `from_template=True` children.
- **Parent-child state reads**: `state.get(key, arc_id=child_id)` — validates target is a descendant and not non-trusted. Non-trusted child state must go through review arcs.
- **Connector system** (in `channels/`): `Connector` ABC with `start()`/`stop()`/`health_check()`. `ConnectorRegistry` manages lifecycle from `connectors` config key. Two kinds: **tool** (external IPC) and **channel** (chat). `ChannelConnector` handles identity resolution via `channel_bindings` → conversation → AI invocation. Auto-migration from legacy `plugins.json`. Old `carpenter.connectors.*` import paths still work via shims.
- **Conversation taint tracking**: Tools declare `trusted_output` via `@tool()`. Untrusted tool use marks the conversation as tainted. **Output isolation at submit is fail-closed**: submit_code in a tainted conversation returns only metadata, never raw output. If the taint check itself fails, output is withheld.
- **Information-flow security**: Three-level integrity lattice (T/C/U). CONSTRAINED acts like UNTRUSTED for enforcement. **JUDGE arcs run deterministic platform code**, not LLMs — they validate data against security policies (default-deny allowlists). Non-trusted arcs require at least one reviewer and a judge at creation. Fernet encryption enforced for non-trusted output at rest. See `docs/trust-invariants.md`.
- **Context management**: Tool output exceeding `tool_output_max_bytes` is truncated (head+tail with file pointer). Context compaction summarizes older messages at `compaction_threshold` of window. Original messages never deleted. Recent N messages always preserved.
- **Network egress**: Default-deny for executors — only platform callback allowed. Web access via `act/web.py` callback. Enforcement via iptables/nftables (subprocess) or `--network=none` (Docker).
- **Auth**: `TokenAuthMiddleware` gates endpoints when `ui_token` is set. Callbacks, webhooks, and review pages are unprotected. `__main__.py` refuses non-loopback bind without `ui_token`, `tls_enabled`, or `allow_insecure_bind`.
- **Coordinator**: `Coordinator` class owns startup/shutdown lifecycle. `http.py:lifespan()` delegates to it. Enables headless and embedded modes.
- **Child failure handling**: Parent notified based on `_escalation_policy` arc_state key (default: `"replan"`). Root failures check `escalation.stacks` for model escalation. `freeze_arc()` propagates failure when all children are frozen.

## Configuration

All instance config lives in `~/carpenter/config.yaml` (generated by `install.sh`). The package ships no hardcoded credentials.

**4-layer precedence** (highest last): defaults < `config.yaml` < `{base_dir}/.env` (credentials only) < standard env vars. Credential mapping defined in `credential_registry.yaml`. Use `python3 -m carpenter setup-credential --key ANTHROPIC_API_KEY` to write to `.env`. Single bootstrap exception: `CARPENTER_CONFIG` env var overrides config file path.

**Source of truth for all config keys**: `carpenter/config.py` (defaults dict + `_CREDENTIAL_MAP`).

Key config areas:
- `ai_provider` / `model_roles` / `agent_roles` / `api_standards` — AI backend, model selection, and API format mapping
- `connectors` — Channel and tool connector definitions (replaces legacy `plugins.json`)
- `ui_token` — **SECRET, set via `.env`** — web auth token
- `tls_*` — Direct TLS termination settings
- `encryption.enforce` — Fernet encryption for non-trusted arc output (default: true, fails-closed)
- `egress_policy` / `egress_enforce` — Executor network egress enforcement
- `api_standards` — Maps providers to API standard (`"anthropic"` or `"openai"`). Defaults: `{anthropic: anthropic, ollama: openai, local: openai, tinfoil: openai}`. To add a new provider: set its standard here, implement `call()` with `tools` param, register in `_get_client()`/`_get_provider_for_client()`
- `local_*` — Local inference server: `local_model_path`, `local_llama_cpp_path`, `local_server_port` (8081), `local_context_size` (8192), `local_parallel` (1), `local_repack` ("auto" — checks MemAvailable + commit headroom; or `true`/`false`), `local_gpu_layers` (0), `local_startup_timeout` (120), `local_server_args` (extra CLI flags)
- `reflection.*` — Reflective meta-cognition (opt-in, costs API credits)
- `review.adversarial_mode` — Adversarial zero-findings review mode

## Making Changes to This Repo

**MANDATORY: Always use a PR-based workflow.** Never commit directly to `main` or push to `main`. Always create a feature branch, push it to `origin` (the fork), and file a PR against `upstream` (the main repo). Direct pushes to `main` are only permitted if the user explicitly requests it for a specific situation.

## What's Not Yet Done

- Docker executor is implemented but not tested with real Docker (mocked in tests)
- Main loop integration tested via `test_integration_coding_change.py` (8 tests cover full arc flow)
- No archival of completed arcs yet (archived_arcs table exists but no archival logic)
- Scale architecture preparation (DB abstraction, work queue interface, connection pooling)
