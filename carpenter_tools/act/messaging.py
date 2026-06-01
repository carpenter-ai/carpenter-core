"""Messaging action tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_types={"message": "UnstructuredText"})
def send(message: str) -> dict:
    """Send a message to the user."""
    ...
