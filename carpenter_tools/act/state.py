"""State mutation tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_types={"key": "Label"})
def set(key: str, value):
    """Store a state value."""
    ...


@tool(local=True, readonly=False, side_effects=True,
      param_types={"key": "Label"})
def set_typed(key: str, value) -> str:
    """Set arc state from an attrs model instance. Serializes via cattrs.unstructure()."""
    ...


@tool(local=True, readonly=False, side_effects=True,
      param_types={"key": "Label"})
def delete(key: str):
    """Delete a state key."""
    ...
