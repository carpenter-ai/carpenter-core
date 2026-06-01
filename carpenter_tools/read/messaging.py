"""Read-only messaging tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""
from ..tool_meta import tool


@tool(local=True, readonly=True, side_effects=False,
      param_types={"question": "UnstructuredText"})
def ask(question: str) -> dict:
    """Ask the user a question. Returns their response."""
    ...
