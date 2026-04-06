"""Review verdict tool backend — handles review.submit_verdict callbacks."""

from ..core.workflows import review_manager


def handle_submit_verdict(params: dict) -> dict:
    """Submit a review verdict. Params: reviewer_arc_id, target_arc_id, decision, reason."""
    return review_manager.submit_verdict(
        reviewer_arc_id=params["reviewer_arc_id"],
        target_arc_id=params["target_arc_id"],
        decision=params["decision"],
        reason=params.get("reason", ""),
    )
