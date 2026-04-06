"""Config management tools. Tier 1: callback to platform.

These tools let reviewed code modify safe platform configuration values
without a server restart.  Only keys in the server-side allowlist can be
changed; security-critical settings (API keys, sandbox config, etc.) are
excluded.

For reading config values use carpenter_tools.read.config.get_value.
"""
from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_types={"key": "Label"})
def set_value(key: str, value) -> dict:
    """Set a platform config value and hot-reload.

    Writes the new value to ~/carpenter/config.yaml and immediately
    reloads the in-memory CONFIG on the running server.

    Only keys in the server-side mutable-key allowlist are accepted
    (e.g. memory_recent_hints, tool_output_max_bytes, heartbeat_seconds).

    Returns {"status": "ok", "key": key, "value": value, "previous": old}.
    Raises on disallowed keys.
    """
    return callback("config.set_value", {"key": key, "value": value})


@tool(local=True, readonly=False, side_effects=True)
def reload() -> dict:
    """Reload the platform configuration from ~/carpenter/config.yaml.

    Updates the in-memory CONFIG without a server restart.
    Returns {"status": "ok", "reloaded": True}.
    """
    return callback("config.reload", {})
