"""Platform management tools. Tier 1: callback to platform."""
from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_types={"mode": "Label"})
def request_restart(mode: str = "opportunistic", reason: str = "") -> dict:
    """Request a platform restart.

    Args:
        mode: 'opportunistic' waits until idle (recommended);
              'urgent' drains in-flight work then restarts immediately.
        reason: Optional human-readable reason for the restart.
    """
    return callback("platform.request_restart", {
        "mode": mode,
        "reason": reason,
    })
