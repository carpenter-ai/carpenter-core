"""Review verdict submission tool. Tier 1: callback to platform."""
from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_types={"decision": "Label", "reason": "UnstructuredText"})
def submit_verdict(
    target_arc_id: int,
    decision: str,
    reason: str = "",
) -> dict:
    """Submit a review verdict for a tainted arc.

    Args:
        target_arc_id: The tainted arc being reviewed.
        decision: 'approve' or 'reject'.
        reason: Explanation for the verdict.

    Returns:
        Dict with 'accepted' and 'promoted' booleans.
    """
    import os
    reviewer_arc_id = int(os.environ.get("TC_ARC_ID", "0"))
    return callback("review.submit_verdict", {
        "reviewer_arc_id": reviewer_arc_id,
        "target_arc_id": target_arc_id,
        "decision": decision,
        "reason": reason,
    })
