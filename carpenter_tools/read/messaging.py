"""Read-only messaging tools. Tier 1: callback to platform."""
from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=True, side_effects=False,
      param_types={"question": "UnstructuredText"})
def ask(question: str) -> dict:
    """Ask the user a question. Returns their response."""
    return callback("messaging.ask", {"question": question})
