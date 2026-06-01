"""Read-only state tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""
from ..tool_meta import tool


@tool(local=True, readonly=True, side_effects=False,
      param_types={"key": "Label"})
def get(key: str, default=None, arc_id: int | None = None):
    """Get a state value by key. Optionally read from a child arc's state."""
    ...


@tool(local=True, readonly=True, side_effects=False,
      param_types={"key": "Label"})
def get_typed(key: str, model_class):
    """Get arc state and validate against an attrs model class.

    Returns a model instance.

    Raises:
        KeyError: If the state key is not found.
        cattrs.errors.ClassValidationError: If the stored data does not match the model.
    """
    ...


@tool(local=True, readonly=True, side_effects=False)
def list_keys():
    """List all state keys."""
    ...
