"""Read-only config tools. Tier 1: callback to platform."""
from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=True, side_effects=False,
      param_types={"key": "Label"})
def get_value(key: str) -> dict:
    """Read a platform config value from the live in-memory CONFIG.

    Returns {\"key\": key, \"value\": current_value}.
    """
    return callback("config.get_value", {"key": key})


@tool(local=True, readonly=True, side_effects=False)
def list_keys() -> dict:
    """List all mutable platform config keys with current values and descriptions.

    Returns {\"keys\": [{\"key\": str, \"value\": any, \"description\": str}, ...]}.
    Use this to discover which config setting controls a feature before changing it.
    """
    return callback("config.list_keys", {})


@tool(local=True, readonly=True, side_effects=False)
def models() -> dict:
    """Return the model manifest: all available AI models with capabilities.

    Returns {\"models\": {identifier: {provider, model_id, description,
    cost_tier, context_window, roles}, ...}}.
    Use this to choose the right model for arc creation.
    """
    return callback("config.models", {})
