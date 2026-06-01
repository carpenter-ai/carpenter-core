"""Configuration loader for Carpenter.

Loads configuration from four sources (in order of precedence):
1. Defaults
2. YAML config file
3. {base_dir}/.env  (KEY=VALUE credential file; beats YAML)
4. Actual environment variables using standard credential names (highest precedence)

Config is stored as a module-level dict CONFIG (alias for internal _cache).
All existing ``CONFIG["key"]`` call sites work unchanged.

Single bootstrap exception: CARPENTER_CONFIG env var overrides config file path
(needed before anything else can be read).
"""

import logging
import os
from pathlib import Path

_log = logging.getLogger(__name__)

# Try to import yaml; fall back gracefully if not installed
try:
    import yaml
except ImportError:
    yaml = None


# Default base directory
_DEFAULT_BASE = os.path.expanduser("~/carpenter")

DEFAULTS = {
    "base_dir": _DEFAULT_BASE,
    "database_path": os.path.join(_DEFAULT_BASE, "data", "platform.db"),
    "log_dir": os.path.join(_DEFAULT_BASE, "data", "logs"),
    "code_dir": os.path.join(_DEFAULT_BASE, "data", "code"),
    "workspaces_dir": os.path.join(_DEFAULT_BASE, "data", "workspaces"),
    "templates_dir": os.path.join(_DEFAULT_BASE, "config", "templates"),
    "tools_dir": os.path.join(_DEFAULT_BASE, "config", "tools"),
    "data_models_dir": os.path.join(_DEFAULT_BASE, "config", "data_models"),
    "kb": {
        "enabled": True,
        "dir": "",                          # default: {base_dir}/kb
        "max_entry_bytes": 6000,            # soft cap (~1500 tokens)
        "staleness_days": 30,               # entries not accessed in N days flagged stale
        "search_backend": "embedding",        # embedding | vector | onnx | fts5 | hybrid
        # NOTE: ``kb.embedding_*`` keys are kept as one-release fallbacks
        # for the new top-level ``embedding`` block below.  New deployments
        # should configure embedding via ``embedding:`` in config.yaml.
        "embedding_url": "http://192.168.2.243:11434",
        "embedding_model": "nomic-embed-text",
        "embedding_dim": 384,
        "onnx_model_path": "",              # auto-resolved: {base_dir}/models/all-MiniLM-L6-v2.onnx
        "work_history_enabled": True,
        "theme_map": {},                    # overrides for tool-module -> KB-path mapping
    },
    # Phase 2 PR-1: process-wide embedding service config.  Defaults
    # reproduce the historical KB behaviour (local all-MiniLM-L6-v2 via
    # ONNX with numpy fallback).  See carpenter/embeddings/service.py.
    "embedding": {
        "provider": "local",          # local | ollama
        "batch_size": 10,
        "local": {
            "model_path": "",         # default: {base_dir}/models/all-MiniLM-L6-v2.onnx
        },
        "ollama": {
            "url": "http://192.168.2.243:11434",
            "model": "nomic-embed-text",
            "dim": 768,
        },
    },
    "prompts_dir": "",  # default: {base_dir}/prompts
    "prompt_templates_dir": "",  # default: {base_dir}/config/prompt-templates
    "coding_prompts_dir": "",  # default: {base_dir}/config/coding-prompts
    "coding_tools_dir": "",  # default: {base_dir}/config/coding-tools
    "executor_type": "restricted",
    "compaction_threshold": 0.8,
    "compaction_threshold_tokens": 0,
    "compaction_preserve_recent": 8,
    "context_compaction_hours": 6,
    "workspace_retention_days": 14,
    "workspace_retention_count": 100,
    "arc_archive_days": 7,
    "mechanical_retry_max": 4,
    "agentic_iteration_budget": 10,
    "agentic_iteration_cap": 256,
    "heartbeat_seconds": 5,
    "max_concurrent_handlers": 5,
    "execution_session_expiry_hours": 1,
    "shutdown_timeout": 25,
    "executor_grace_seconds": 5,
    "host": "127.0.0.1",
    "port": 7842,
    # IMPORTANT: do NOT set ui_token in config.yaml — it is a secret token.
    # Set UI_TOKEN in {base_dir}/.env or as an environment variable instead.
    "ui_token": "",
    "allow_insecure_bind": False,
    "default_thread_pool_size": 16,
    "work_handler_thread_pool_size": 3,
    "rate_limit_rpm": 45,
    "rate_limit_itpm": 35000,
    "rate_limit_headroom": 0.95,
    "rate_limit_429_fill_fraction": 0.75,
    "merge_resolution_template": "merge-resolution",
    "model_roles": {
        "default": "",
        "chat": "",
        "default_step": "",
        "title": "",
        "summary": "",
        "compaction": "",
        "code_review": "",
        "review_judge": "",
    },
    "memory_recent_hints": 2,
    "chat_language": "",  # ISO 639-1 code; empty = respond in user's language
    "review": {
        "adversarial_mode": False,
        "adversarial_min_findings": 1,
        "reviewer_max_tokens": 200,
        "reviewer_temperature": 0.0,
        "adversarial_max_tokens": 1000,
        "histogram_max_tokens": 100,
        "histogram_top_words": 50,
        "progressive_text_review": {
            "enabled": True,
            "max_concurrent_sessions": 5,
            "window_size": 10,
            "words_per_session_min": 80,
            "max_words": 1000,
            "window_max_tokens": 20,
            "excerpt_max_chars": 60,
        },
        # Quarantined Quality Reviewer (QQR) — defence-in-depth second
        # reviewer that does NOT see chat history. See
        # ``carpenter/review/qqr.py``. Composition rules in
        # ``carpenter/review/pipeline.py::determine_outcome``: under
        # disabled/errored verification, BOTH reviewers must concur for
        # APPROVE — never expands today's auto-approve surface.
        "qqr": {
            "enabled": True,
            # Optional model-policy ID; if set and resolves to a model in
            # ``allowed_models`` it is used. Otherwise the first allowed
            # model is used.
            "model_policy_id": "",
            # Vetted allowlist. The selected model MUST be in this list —
            # config drift to a non-allowlisted model falls back to the
            # first entry.
            "allowed_models": [
                "anthropic:claude-haiku-4-5",
                "anthropic:claude-sonnet-4-6",
            ],
            # When True (default), QQR errors / timeouts / malformed JSON
            # produce an ABSTAIN signal. The aggregator then falls back to
            # today's main-reviewer-only path with an audit-log entry.
            # The narrowed-approval table can only upgrade outcomes to
            # MAJOR; it never silently approves more than today.
            "fail_closed": True,
        },
    },
    "verification": {
        "enabled": True,
        "threshold": 150,
        "model_policy_fallback": "careful-coding",
        "feedback_reason_max_length": 300,
        "feedback_summary_max_length": 500,
        "arc_names": {
            "correctness_check": "verify-correctness",
            "quality_check": "verify-quality",
            "judge": "judge-verification",
            "documentation": "post-verification-docs",
        },
        # Safe stdlib modules allowed in verified code (whitelist and dry-run).
        # Empty list means use the built-in default.
        "allowed_stdlib_modules": [],
    },
    # Agent capability matrix — maps agent types to their allowed capabilities.
    # Keys are agent type strings (PLANNER, EXECUTOR, REVIEWER, JUDGE, CHAT).
    # Each entry has: can_create_untrusted_arcs (bool),
    # allowed_tools (list of tool names, or null for unrestricted).
    # See carpenter/core/trust_types.py for hardcoded defaults.
    "agent_capabilities": {},
    "agent_roles": {
        "security-reviewer": {
            "system_prompt": "You are a security reviewer for agent-generated code. Analyze for injection vulnerabilities, unsafe operations, and security risks.",
            "auto_review_output_types": ["python", "shell"],
            "temperature": 0.2,
        },
        "ux-reviewer": {
            "system_prompt": "You are a UX/safety reviewer. Ensure outputs are appropriate, helpful, and safe for end users.",
            "auto_review_output_types": ["text", "json"],
        },
        "judge": {
            "system_prompt": "You are the final judge in a multi-reviewer process. Synthesize reviewer verdicts and render final approval or rejection.",
            "auto_review_output_types": [],
            "temperature": 0.1,
        },
    },
    "models": {
        "opus": {
            "provider": "anthropic",
            "model_id": "claude-opus-4-7",
            "description": "Most capable model. Deep reasoning, architecture decisions, security review, complex multi-step planning.",
            "cost_tier": "high",
            "context_window": 200000,
            "roles": ["planning", "review", "implementation"],
        },
        "sonnet": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4-6",
            "description": "Balanced capability and cost. Standard implementation, code review, general-purpose tasks.",
            "cost_tier": "medium",
            "context_window": 200000,
            "roles": ["planning", "review", "implementation", "documentation"],
        },
        "haiku": {
            "provider": "anthropic",
            "model_id": "claude-haiku-4-5",
            "description": "Fast and cheap. Summarization, simple code generation, data extraction, formatting.",
            "cost_tier": "low",
            "context_window": 200000,
            "roles": ["summarization", "documentation"],
        },
    },
    "ai_provider": "anthropic",
    "inference_chain": [],  # list of backend dicts; active when ai_provider == "chain"
    "api_standards": {
        "anthropic": "anthropic",
        "ollama": "openai",
        "tinfoil": "openai",
        "chain": "anthropic",
    },
    "ollama_url": "http://localhost:11434",
    "ollama_model": "llama3.1",
    "tinfoil_model": "llama3-3-70b",
    "tinfoil_max_tokens": 4096,
    # Context windows — map provider prefixes or specific model strings to token limits.
    # Used by compaction logic and prompt building.
    "context_windows": {
        "ollama": 16384,
        "tinfoil": 8192,
        "anthropic": 200000,
        "chain": 16384,
    },
    "sandbox": {
        "method": "auto",
        "allowed_write_dirs": [],  # empty = compute from config paths
        "on_failure": "open",      # open = fallback unsandboxed, closed = refuse
    },
    "encryption": {
        "enforce": True,  # Require cryptography library for tainted arcs (fail-closed)
    },
    "security": {
        "email_allowlist": [],
        "domain_allowlist": [],
        "url_allowlist": [],
        "filepath_allowlist": [],
        "command_allowlist": [],
        # Whitelist of carpenter_tools modules whose output is trusted.
        # Any carpenter_tools import NOT in this list taints the conversation.
        # Empty list means use the built-in default from security/trust.py.
        "trusted_imports": [],
        # Network modules that cause taint when imported in executor code.
        # Covers stdlib and common third-party networking packages.
        # Empty list means use the built-in default from security/trust.py.
        "network_modules": [],
    },
    "retry_max_attempts": 3,
    "retry_base_delay": 1.0,  # seconds, exponential backoff base
    "circuit_breaker_threshold": 5,  # consecutive failures before opening
    "circuit_breaker_recovery_seconds": 60,
    "model_health_window_size": 20,  # sliding window size for model health tracking
    "arc_retry": {
        "enabled": True,                     # Master switch
        "default_policy": "transient_only",  # transient_only | aggressive | conservative
        "max_retries": {
            "RateLimitError": 5,
            "APIOutageError": 4,
            "NetworkError": 3,
            "UnknownError": 2,
            "VerificationError": 2,
            "default": 3,
        },
        "backoff_caps": {
            "RateLimitError": 600,   # 10 min
            "APIOutageError": 300,   # 5 min
            "NetworkError": 60,      # 1 min
            "VerificationError": 0,  # immediate retry
            "default": 120,          # 2 min
        },
        "backoff_base": 2,           # Exponential base (2^attempt)
        "jitter_percent": 10,        # ±10% randomization
        "escalate_on_exhaust": {
            "RateLimitError": False,
            "APIOutageError": True,
            "ModelError": True,
            "VerificationError": False,
            "default": False,
        },
    },
    "executor_memory_limit_mb": 300,  # Legacy (unused with restricted executor)
    "egress_policy": "auto",   # Legacy (unused with restricted executor)
    "egress_enforce": True,    # Legacy (unused with restricted executor)
    # TLS/SSL configuration
    "tls_enabled": False,
    "tls_cert_path": "",
    "tls_key_path": "",
    "tls_domain": "",       # Domain matching cert SAN/CN (used for callback URLs)
    "tls_ca_path": "",      # Custom CA bundle for executor verification (empty = system CA)
    # Scheduling: allowed event types for cron/one-shot triggers.
    "scheduling_allowed_event_types": ["cron.message", "arc.dispatch"],
    # Trigger and subscription pipeline configuration.
    # Triggers emit events into the event bus; subscriptions route events to actions.
    # Feature-specific triggers are declared inside each template's ``triggers:``
    # section and loaded at startup via ``template_manager.load_template_triggers``
    # — the platform config holds only cross-feature triggers here.
    "triggers": [],
    "subscriptions": [],
    # Resource sweep: delete blobs of deprecated Resources older than N days.
    "resource_sweep_age_days": 7,
    "connectors": {},                   # Connector definitions (replaces plugins.json)
    "connector_retention_days": 7,      # Days to keep completed connector task folders
    "plugin_shared_base": "",           # Base path for plugin shared folders (empty = disabled)
    "plugins_config": "",               # Path to plugins.json (empty = {base_dir}/plugins.json)
    "plugin_retention_days": 7,         # Days to keep completed plugin task folders
    "model_registry_path": "",  # Path to model_registry.yaml (empty = {base_dir}/model_registry.yaml)
    "db_encryption_key": "",  # Optional SQLCipher key (requires pysqlcipher3)
    "tool_output_max_bytes": 32768,
    "tool_output_head_lines": 50,
    "tool_output_tail_lines": 20,
    # Tool backend timeouts and limits
    "git_api_timeout": 30.0,               # default git server API HTTP timeout (seconds)
    "git_api_long_timeout": 60.0,         # timeout for large git server responses like diffs (seconds)
    # Forge provider selection (D5 — see carpenter/forges/).  ``forge`` picks
    # which provider implementation to dispatch to (currently "forgejo";
    # "github" is a follow-up).  ``forge_default_base_branch`` is the PR base
    # branch used when callers don't override it.
    "forge": "forgejo",
    "forge_default_base_branch": "main",
    "web_request_default_timeout": 30.0,  # default HTTP timeout for web tool requests (seconds)
    "web_response_max_chars": 10000,      # max chars returned from web GET/POST responses
    "web_fetch_max_bytes": 1000000,       # max bytes for webpage fetch content (1 MB)
    # Tool classification lists — used by the callback API for access control.
    # Each list can be extended (add items) or reduced (remove items) via config.yaml.
    # Format in config.yaml:
    #   tool_lists:
    #     session_exempt_tools_add: ["my_custom.read"]
    #     session_exempt_tools_remove: ["plugin.list_plugins"]
    #     external_access_tools_add: ["web.soap_call"]
    "tool_lists": {
        "session_exempt_tools_add": [],
        "session_exempt_tools_remove": [],
        "external_access_tools_add": [],
        "external_access_tools_remove": [],
        "messaging_tools_add": [],
        "messaging_tools_remove": [],
        "core_tools_add": [],
        "core_tools_remove": [],
        "ultra_core_tools_add": [],
        "ultra_core_tools_remove": [],
    },
    # Display and truncation limits (previously in constants.py)
    "arc_state_value_max_length": 300,
    "arc_log_output_max_length": 8000,
    "conversation_summary_max_length": 6000,
    "conversation_summary_min_remaining": 50,
    "pr_review_summary_max_length": 200,
    "arc_parent_chain_max_depth": 100,
    "default_coding_agent": "builtin",
    "notifications": {
        "email": {
            "enabled": False,
            "mode": "smtp",        # "smtp" or "command"
            "smtp_host": "",
            "smtp_port": 587,
            "smtp_from": "",
            "smtp_to": "",
            "smtp_username": "",
            "smtp_password": "",
            "smtp_tls": True,
            "command": "",          # shell command for command mode
        },
        "batch_window": 60,        # seconds to batch notifications
        "priorities": ["urgent", "normal", "low", "fyi"],
        "default_routing": {
            "urgent": ["chat", "email"],
            "normal": ["chat", "email"],
            "low": ["email"],
            "fyi": [],
        },
        "routing": {
            "review_needed": "normal",
            "security_events": "urgent",
        },
    },
    "model_presets": {},  # User overrides for model selector presets (see model_selector.py)
    # Platform-integrity classifier config.  Read by
    # ``carpenter/security/platform_paths.py`` to drive path-tier and
    # change-category decisions.  This PR is purely additive — later PRs
    # consume these keys to drive workflow routing and human-review forcing.
    #
    # ``change_workflows``: maps change-category ("python", "yaml", "kb",
    # "unknown") to the template name used by the routing layer.
    # ``path_overrides``: optional user-supplied list of
    # ``{prefix: "/abs/path", tier: "T0"|"T1"|"T2"}`` entries that ADD to
    # the hardcoded T0/T1 floor.  Entries cannot demote a hardcoded T1
    # path to T2 — the floor wins.
    "platform_integrity": {
        "change_workflows": {
            "python": "coding-change",
            "yaml": "yaml-change",
            "kb": "kb-change",
            "unknown": "coding-change",
        },
        "path_overrides": [],
    },
    "coding_agents": {
        "builtin": {
            "type": "builtin",
            "model": "claude-sonnet-4-6",
            "max_tokens": 4096,
            "max_iterations": 20,
            "timeout": 300,
            # system_prompt: loaded from coding-prompts-dir templates.
            # Set explicitly here to override the template files.
        },
        "claude-code": {
            "type": "external",
            "command": "claude -p {prompt_file} --output-format stream-json",
            "timeout": 600,
            "env": {
                "ANTHROPIC_API_KEY": "{claude_api_key}",
            },
        },
    },
}

# Credential registry: env_var_name -> {config_key, description, ...}
# Populated in load_config() from credential_registry.yaml.
CREDENTIAL_REGISTRY: dict = {}

# Derived map: env_var_name -> config_key (subset of CREDENTIAL_REGISTRY for fast lookup)
# Populated in load_config() from CREDENTIAL_REGISTRY.
_CREDENTIAL_MAP: dict[str, str] = {
    # Tokens / secrets — never put these in config.yaml
    "UI_TOKEN": "ui_token",
    # AI provider credentials
    "ANTHROPIC_API_KEY": "claude_api_key",
    "TINFOIL_API_KEY": "tinfoil_api_key",
    # TLS
    "TLS_KEY_PASSWORD": "tls_key_password",
    # Git / forge
    "GIT_TOKEN": "git_token",
    "GIT_AUTHOR_NAME": "git_author_name",
    "GIT_AUTHOR_EMAIL": "git_author_email",
    "GIT_COMMITTER_NAME": "git_committer_name",
    "GIT_COMMITTER_EMAIL": "git_committer_email",
}


def _load_yaml(path: str) -> dict:
    """Load a YAML config file. Returns empty dict on missing file or missing yaml."""
    if yaml is None:
        return {}
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


# Map of deprecated config keys → canonical names.  The carpenter codebase
# moved off Forgejo-flavored names; this one-shot migration rewrites any
# lingering occurrences in the on-disk config.yaml so the runtime never has
# to dual-read.  ``git_server_url`` was a short-lived intermediate name and
# is also migrated to the final ``git_url``.
_LEGACY_CONFIG_KEY_MAP = {
    "forgejo_url": "git_url",
    "git_server_url": "git_url",
    "forgejo_token": "git_token",
    "forgejo_api_timeout": "git_api_timeout",
    "forgejo_api_long_timeout": "git_api_long_timeout",
}

# Map of deprecated env-var / dot-env keys → canonical names.
_LEGACY_DOT_ENV_KEY_MAP = {
    "FORGEJO_TOKEN": "GIT_TOKEN",
}


def _migrate_legacy_config_keys(yaml_path: str) -> None:
    """Rewrite deprecated config keys in-place in ``yaml_path`` and {base_dir}/.env.

    Idempotent: if no legacy keys are present the files are left untouched.
    If a canonical key is already set, the legacy key is dropped (canonical wins,
    matching the precedence the old dual-read layer used).
    """
    if yaml is None:
        return

    # ── config.yaml ──────────────────────────────────────────────────
    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        data = None

    if isinstance(data, dict):
        changed = False
        for old_key, new_key in _LEGACY_CONFIG_KEY_MAP.items():
            if old_key not in data:
                continue
            old_val = data.pop(old_key)
            if new_key not in data:
                data[new_key] = old_val
            changed = True
        if changed:
            try:
                with open(yaml_path, "w") as f:
                    yaml.dump(
                        data,
                        f,
                        default_flow_style=False,
                        allow_unicode=True,
                        sort_keys=False,
                    )
            except OSError:
                # Best-effort: a read-only filesystem shouldn't break server
                # startup; the in-process config still has the canonical
                # values because the dict was mutated above.
                pass

    # ── {base_dir}/.env ──────────────────────────────────────────────
    # We don't know base_dir until after YAML load, but the .env file lives
    # alongside config.yaml in deployment.  Walk up one directory and look
    # for a sibling .env at the conventional path.
    try:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(yaml_path)))
        dot_env_path = os.path.join(base_dir, ".env")
        if not os.path.isfile(dot_env_path):
            return
        with open(dot_env_path) as f:
            lines = f.readlines()
        new_lines: list[str] = []
        seen: dict[str, bool] = {}
        env_changed = False
        # Track which canonical keys already exist so legacy lines are dropped
        # if the canonical key is also present.
        existing_keys = set()
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            existing_keys.add(stripped.split("=", 1)[0].strip())
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                new_lines.append(line)
                continue
            k, _, rest = line.partition("=")
            k_clean = k.strip()
            if k_clean in _LEGACY_DOT_ENV_KEY_MAP:
                new_key = _LEGACY_DOT_ENV_KEY_MAP[k_clean]
                env_changed = True
                if new_key in existing_keys:
                    # Canonical already present elsewhere — drop the legacy line.
                    continue
                if seen.get(new_key):
                    continue
                seen[new_key] = True
                # Preserve original line ending
                new_lines.append(f"{new_key}={rest}" if rest else f"{new_key}=\n")
            else:
                new_lines.append(line)
        if env_changed:
            try:
                with open(dot_env_path, "w") as f:
                    f.writelines(new_lines)
            except OSError:
                pass
    except OSError:
        pass


def _load_credential_registry(base_dir: str) -> dict:
    """Load credential_registry.yaml from base_dir. Returns {} if absent."""
    path = os.path.join(base_dir, "config", "credential_registry.yaml")
    return _load_yaml(path)


def _load_dot_env(base_dir: str, cred_map: dict | None = None) -> dict:
    """Load {base_dir}/.env KEY=VALUE file, mapping credential names to config keys.

    Only keys present in cred_map (defaults to _CREDENTIAL_MAP) are loaded.
    Returns {} if the file is absent.
    """
    if cred_map is None:
        cred_map = _CREDENTIAL_MAP
    path = os.path.join(base_dir, ".env")
    result = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                config_key = cred_map.get(key.strip())
                if config_key:
                    result[config_key] = value.strip()
    except OSError:
        pass
    return result


def _load_env(cred_map: dict | None = None) -> dict:
    """Read credential env vars using standard names from cred_map."""
    if cred_map is None:
        cred_map = _CREDENTIAL_MAP
    result = {}
    for env_var, config_key in cred_map.items():
        val = os.environ.get(env_var)
        if val is not None:
            result[config_key] = val
    return result


def _coerce_types(config: dict, schema: dict) -> dict:
    """Coerce string config values to match the type implied by the schema (DEFAULTS).

    YAML safe_load returns native types for unquoted values; this pass only converts
    strings — handling the case where a user quotes a value that should be numeric or
    boolean (e.g. ``port: '7842'`` → 7842, ``allow_insecure_bind: 'true'`` → True).
    Silently ignores values that cannot be coerced.
    """
    for key, schema_val in schema.items():
        if key not in config:
            continue
        val = config[key]
        if isinstance(schema_val, dict) and isinstance(val, dict):
            _coerce_types(val, schema_val)
            continue
        if not isinstance(val, str):
            continue
        # bool must be checked before int (bool is a subclass of int in Python)
        if isinstance(schema_val, bool):
            low = val.lower()
            if low in ("true", "yes", "1", "on"):
                config[key] = True
            elif low in ("false", "no", "0", "off"):
                config[key] = False
        elif isinstance(schema_val, int):
            try:
                config[key] = int(val)
            except ValueError:
                pass
        elif isinstance(schema_val, float):
            try:
                config[key] = float(val)
            except ValueError:
                pass
    return config


def _expand_paths(config: dict) -> dict:
    """Expand ~ and resolve path-typed config values."""
    path_keys = {
        "base_dir", "database_path", "log_dir", "code_dir",
        "workspaces_dir", "templates_dir", "tools_dir",
        "data_models_dir", "prompt_templates_dir",
        "plugin_shared_base", "plugins_config",
        "tls_cert_path", "tls_key_path", "tls_ca_path",
    }
    for key in path_keys:
        if key in config and isinstance(config[key], str):
            config[key] = os.path.expanduser(config[key])
    return config


# Layout-dependent path keys derived from ``base_dir`` when not explicitly
# set in YAML.  Values are relative paths under ``base_dir``.
#
# This map is the single source of truth for server-side default layout.
# install.sh (and other deployment tooling) should only emit ``base_dir`` and
# genuinely deployment-specific values (port, AI provider, keys); the server
# derives every other path from this map at startup.
#
# Keys whose SEED target also appears in ``carpenter.seed.SEED_MANIFEST`` must
# match it.  The overlap today: ``prompts_dir``, ``coding_prompts_dir``,
# ``data_models_dir`` (kb dir lives under ``config["kb"]["dir"]`` and is
# handled separately below).
_DERIVED_PATH_DEFAULTS = {
    "database_path": ("data", "platform.db"),
    "log_dir": ("data", "logs"),
    "code_dir": ("data", "code"),
    "workspaces_dir": ("data", "workspaces"),
    "templates_dir": ("config", "templates"),
    "tools_dir": ("config", "tools"),
    "data_models_dir": ("data_models",),
    "prompts_dir": ("config", "prompts"),
    "coding_prompts_dir": ("config", "coding-prompts"),
    "coding_tools_dir": ("config", "coding-tools"),
    "chat_tools_dir": ("config", "chat_tools"),
    "prompt_templates_dir": ("config", "prompt-templates"),
}


def _derive_paths_from_base_dir(
    config: dict, base_dir: str, yaml_overrides: dict,
) -> None:
    """Fill layout-dependent path defaults from ``base_dir`` when unset.

    A key is considered "unset" when the YAML file did not provide a value
    for it (so DEFAULTS' placeholder wins) — in that case we overwrite with
    a path derived from ``base_dir``.  If the YAML explicitly sets the key,
    its value is preserved for back-compat.
    """
    base = os.path.expanduser(base_dir)
    for key, parts in _DERIVED_PATH_DEFAULTS.items():
        if key in yaml_overrides:
            continue  # explicit YAML value wins
        config[key] = os.path.join(base, *parts)

    # kb.dir lives inside the nested ``kb`` dict.  Only derive when the user
    # did not explicitly set it in their YAML ``kb`` block.  Deep-copy before
    # mutating so we never write into the shared DEFAULTS["kb"] dict.
    import copy as _copy
    yaml_kb = yaml_overrides.get("kb") if isinstance(yaml_overrides, dict) else None
    kb_cfg = config.get("kb")
    if isinstance(kb_cfg, dict):
        kb_explicit_dir = isinstance(yaml_kb, dict) and bool(yaml_kb.get("dir"))
        if not kb_explicit_dir and not kb_cfg.get("dir"):
            kb_cfg = _copy.deepcopy(kb_cfg)
            kb_cfg["dir"] = os.path.join(base, "config", "kb")
            config["kb"] = kb_cfg


def load_config(yaml_path: str | None = None) -> dict:
    """Build config from defaults, YAML file, .env file, and credential env vars.

    Precedence (highest last): defaults < YAML < {base_dir}/.env < env vars.
    """
    global CREDENTIAL_REGISTRY, _loaded_yaml_path

    config = dict(DEFAULTS)

    # Layer 2: YAML overrides
    if yaml_path is None:
        yaml_path = os.environ.get(
            "CARPENTER_CONFIG",
            os.path.join(DEFAULTS["base_dir"], "config", "config.yaml"),
        )
    # Record which file is authoritative so config_tool (and anyone else) can
    # write back to the same path without re-deriving it from env vars.
    _loaded_yaml_path = yaml_path
    # One-shot migration: rewrite deprecated forgejo_*/git_server_url keys to
    # their canonical git_* names in the on-disk YAML (and dot-env) before we
    # load it.  Done in-place so subsequent loads see the new names directly —
    # no dual-read backward-compat layer.
    _migrate_legacy_config_keys(yaml_path)
    yaml_overrides = _load_yaml(yaml_path)
    config.update(yaml_overrides)

    # Resolve base_dir early (needed for .env and registry paths)
    base_dir = os.path.expanduser(config.get("base_dir", DEFAULTS["base_dir"]))

    # Derive layout-dependent path defaults from base_dir for keys the user
    # did not explicitly set in YAML.  This lets install.sh (and other
    # deployment tooling) set ``base_dir`` alone and rely on the server to
    # compute every other path — eliminating the need to duplicate layout
    # knowledge in install scripts.
    #
    # Back-compat: if ``config.yaml`` sets an explicit value for any of these
    # keys, that value wins.  Existing deployments with fully-populated
    # ``config.yaml`` keep working unchanged.
    _derive_paths_from_base_dir(config, base_dir, yaml_overrides)

    # Load credential registry; build a call-local map (no global mutation of _CREDENTIAL_MAP)
    registry = _load_credential_registry(base_dir)
    if registry:
        CREDENTIAL_REGISTRY = registry
    # Merge hardcoded defaults with any user-defined extras from registry
    local_cred_map = dict(_CREDENTIAL_MAP)
    if registry:
        for env_var, entry in registry.items():
            if isinstance(entry, dict) and "config_key" in entry:
                local_cred_map[env_var] = entry["config_key"]

    # Layer 3: {base_dir}/.env (credential convenience file; beats YAML)
    dot_env = _load_dot_env(base_dir, local_cred_map)
    config.update(dot_env)

    # Layer 4: Actual env vars (credential names only — highest precedence)
    env_overrides = _load_env(local_cred_map)
    config.update(env_overrides)

    # Coerce any accidentally-quoted YAML values to match the type of the default
    # (e.g. port: '7842' → 7842).  Credentials are strings in DEFAULTS so they
    # are never coerced.
    _coerce_types(config, DEFAULTS)

    config = _expand_paths(config)
    return config


# Internal cache — mutation target; never rebound (so aliases stay valid)
_cache: dict = {}
# Module-level alias: all existing ``config.CONFIG["key"]`` call sites work unchanged.
CONFIG = _cache

# Effective YAML path resolved at first load; used by config_tool for hot-reload
# writes so all code agrees on which file is authoritative.
_loaded_yaml_path: str = ""


def get_config(key: str, default=None):
    """Preferred accessor for new code. Reads from the live CONFIG dict.

    Picks up monkeypatching in tests (looks up CONFIG in module globals at
    call time, not at import time).
    """
    return CONFIG.get(key, default)


# Populate cache on import
_cache.update(load_config())


def reload_config(yaml_path: str | None = None) -> None:
    """Reload CONFIG in-place from disk.

    Updates the existing dict so all modules that hold a reference to CONFIG
    (via ``from carpenter import config; config.CONFIG``) see the new
    values immediately without a server restart.

    Runtime-injected keys (if any) that should survive a reload
    are preserved so they are not wiped by a reload.
    """
    # Preserve runtime-only keys that are never in the YAML file
    _RUNTIME_KEYS: tuple = ()
    preserved = {k: _cache[k] for k in _RUNTIME_KEYS if k in _cache}

    new = load_config(yaml_path)
    _cache.clear()
    _cache.update(new)

    # Restore runtime keys (YAML/env cannot override them)
    for k, v in preserved.items():
        _cache.setdefault(k, v)
