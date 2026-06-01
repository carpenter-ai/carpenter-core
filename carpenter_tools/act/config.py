"""Config management tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""
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
    ...


@tool(local=True, readonly=False, side_effects=True)
def reload() -> dict:
    """Reload the platform configuration from ~/carpenter/config.yaml.

    Updates the in-memory CONFIG without a server restart.
    Returns {"status": "ok", "reloaded": True}.
    """
    ...
