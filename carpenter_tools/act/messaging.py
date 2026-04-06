"""Messaging action tools. Tier 1: callback to platform."""
from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_types={"message": "UnstructuredText"})
def send(message: str) -> dict:
    """Send a message to the user."""
    return callback("messaging.send", {"message": message})
