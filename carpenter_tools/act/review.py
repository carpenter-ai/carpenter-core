"""Review verdict tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""
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
    # reviewer_arc_id is injected by the platform dispatch bridge from the
    # calling arc's _caller_arc_id; the client does not set it here.
    ...
