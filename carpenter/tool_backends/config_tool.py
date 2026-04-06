"""Config tool backend — reads, writes, and hot-reloads platform config."""
import logging
import os

from .. import config as config_module

logger = logging.getLogger(__name__)

# Keys that agents are permitted to change at runtime.
# Deliberately excludes security-critical keys (API keys, credentials,
# host/port, encryption settings, sandbox config).
_MUTABLE_KEYS = frozenset({
    "memory_recent_hints",
    "tool_output_max_bytes",
    "tool_output_head_lines",
    "tool_output_tail_lines",
    "heartbeat_seconds",
    "compaction_threshold",
    "compaction_preserve_recent",
    "context_compaction_hours",
    "chat_tool_iterations",
    "agentic_iteration_budget",
    "workspace_retention_days",
    "plugin_retention_days",
    "notifications.batch_window",
    "chat_language",
    "model_roles.default",
    "model_roles.chat",
    "review_auto_approve_threshold",
})

_MUTABLE_KEY_DESCRIPTIONS = {
    "memory_recent_hints": "Number of recent conversation titles shown as memory hints in the agent's context window",
    "tool_output_max_bytes": "Maximum bytes of tool output shown inline; larger results are saved to disk with a pointer",
    "tool_output_head_lines": "Lines kept from the start of a truncated tool output",
    "tool_output_tail_lines": "Lines kept from the end of a truncated tool output",
    "heartbeat_seconds": "Seconds between arc dispatch heartbeat scans",
    "compaction_threshold": "Fraction of context window (0.0–1.0) that triggers conversation compaction",
    "compaction_preserve_recent": "Number of most-recent messages always kept during compaction",
    "context_compaction_hours": "Minimum hours between compaction attempts for a conversation",
    "chat_tool_iterations": "Maximum tool-use iterations per chat turn before forcing a final response",
    "agentic_iteration_budget": "Maximum total iterations an executor arc may use",
    "workspace_retention_days": "Days to keep completed coding-change workspaces on disk",
    "plugin_retention_days": "Days to keep completed plugin task folders",
    "notifications.batch_window": "Seconds to batch notifications before sending",
    "chat_language": "ISO 639-1 language code for the chat agent's response language (e.g. 'de', 'fr'); empty string = respond in the user's language",
    "model_roles.default": "Default model for all roles (format: 'provider:model', e.g. 'anthropic:claude-sonnet-4-20250514'); empty = auto-detect from ai_provider",
    "model_roles.chat": "Model for chat conversations (format: 'provider:model', e.g. 'anthropic:claude-haiku-4.5'); empty = use model_roles.default or auto-detect",
    "review_auto_approve_threshold": "Line count threshold for auto-approving small coding-change diffs (0 = always require manual review; 50 = auto-approve diffs under 50 lines)",
}


def handle_reload(params: dict) -> dict:
    """Reload CONFIG in-place from config.yaml.

    Reads ~/carpenter/config.yaml and updates the
    global CONFIG dict so all live code sees new values without a restart.

    Returns {"status": "ok", "reloaded": True}.
    """
    config_module.reload_config()
    logger.info("Config hot-reloaded via tool callback")
    return {"status": "ok", "reloaded": True}


def handle_set_value(params: dict) -> dict:
    """Set a single config key in config.yaml, then hot-reload.

    Params:
        key   (str): Config key name (must be in _MUTABLE_KEYS allowlist).
        value (any): New value for the key.

    Returns {"status": "ok", "key": ..., "value": ..., "previous": ...}.
    Raises ValueError if the key is not in the allowlist.
    """
    try:
        import yaml
    except ImportError:
        return {"status": "error", "message": "PyYAML not available"}

    key = params.get("key", "")
    value = params.get("value")

    if key not in _MUTABLE_KEYS:
        raise ValueError(
            f"Config key {key!r} is not in the mutable-key allowlist. "
            f"Allowed keys: {sorted(_MUTABLE_KEYS)}"
        )

    yaml_path = config_module._loaded_yaml_path or os.path.join(
        config_module.DEFAULTS["base_dir"], "config", "config.yaml"
    )

    # Read current config
    try:
        with open(yaml_path) as f:
            cfg = yaml.safe_load(f) or {}
    except OSError:
        cfg = {}

    previous = cfg.get(key)

    # Handle nested keys (e.g. "notifications.batch_window")
    if "." in key:
        parts = key.split(".", 1)
        outer, inner = parts[0], parts[1]
        if outer not in cfg or not isinstance(cfg[outer], dict):
            cfg[outer] = {}
        cfg[outer][inner] = value
    else:
        cfg[key] = value

    # Write back
    with open(yaml_path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True,
                  sort_keys=False)

    # Hot-reload in the main process
    config_module.reload_config(yaml_path)
    logger.info("Config key %r set to %r (was %r) and reloaded", key, value, previous)

    return {"status": "ok", "key": key, "value": value, "previous": previous}


def _resolve_nested(key: str) -> object:
    """Resolve a possibly-dotted key from the live CONFIG dict."""
    if "." in key:
        outer, inner = key.split(".", 1)
        parent = config_module.CONFIG.get(outer)
        if isinstance(parent, dict):
            return parent.get(inner)
        return None
    return config_module.CONFIG.get(key)


def handle_get_value(params: dict) -> dict:
    """Read a single config value from the live in-memory CONFIG.

    Params:
        key (str): Config key name.

    Returns {"key": ..., "value": ...}.
    """
    key = params.get("key", "")
    value = _resolve_nested(key)
    return {"key": key, "value": value}


def handle_list_keys(params: dict) -> dict:
    """Return all mutable config keys with current values and descriptions.

    Returns {"keys": [{"key": str, "value": any, "description": str}, ...]}.
    """
    result = []
    for key in sorted(_MUTABLE_KEYS):
        result.append({
            "key": key,
            "value": _resolve_nested(key),
            "description": _MUTABLE_KEY_DESCRIPTIONS.get(key, ""),
        })
    return {"keys": result}


def handle_models(params: dict) -> dict:
    """Return the model manifest from config.

    Returns {"models": {identifier: {provider, model_id, description,
    cost_tier, context_window, roles}, ...}}.
    """
    models = config_module.CONFIG.get("models", {})
    return {"models": models}
