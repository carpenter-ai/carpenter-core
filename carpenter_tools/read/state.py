"""Read-only state tools. Tier 1: callback to platform."""
import json

import cattrs

from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=True, side_effects=False,
      param_types={"key": "Label"})
def get(key: str, default=None, arc_id: int | None = None):
    """Get a state value by key. Optionally read from a child arc's state."""
    params = {"key": key, "default": default}
    if arc_id is not None:
        params["_target_arc_id"] = arc_id
    result = callback("state.get", params)
    return result.get("value", default)


@tool(local=True, readonly=True, side_effects=False,
      param_types={"key": "Label"})
def get_typed(key: str, model_class):
    """Get arc state and validate against an attrs model class.

    Returns a model instance.

    Raises:
        KeyError: If the state key is not found.
        cattrs.errors.ClassValidationError: If the stored data does not match the model.
    """
    raw = get(key)
    if raw is None:
        raise KeyError(f"State key '{key}' not found")
    data = json.loads(raw)
    return cattrs.structure(data, model_class)


@tool(local=True, readonly=True, side_effects=False)
def list_keys():
    """List all state keys."""
    result = callback("state.list", {})
    return result.get("keys", [])
