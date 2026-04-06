"""State mutation tools. Tier 1: callback to platform."""
import json

import attrs
import cattrs

from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_types={"key": "Label"})
def set(key: str, value):
    """Store a state value."""
    return callback("state.set", {"key": key, "value": value})


@tool(local=True, readonly=False, side_effects=True,
      param_types={"key": "Label"})
def set_typed(key: str, value) -> str:
    """Set arc state from an attrs model instance. Serializes via cattrs.unstructure()."""
    if attrs.has(type(value)):
        data = cattrs.unstructure(value)
    else:
        raise TypeError(
            f"set_typed requires an attrs class instance, got {type(value).__name__}"
        )
    return set(key, json.dumps(data))


@tool(local=True, readonly=False, side_effects=True,
      param_types={"key": "Label"})
def delete(key: str):
    """Delete a state key."""
    return callback("state.delete", {"key": key})
