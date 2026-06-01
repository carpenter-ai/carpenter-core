"""Review verdict tool backend — handles review.submit_verdict callbacks."""

from ..core.workflows import review_manager


def handle_submit_verdict(params: dict) -> dict:
    """Submit a review verdict.

    Params: target_arc_id, decision, reason (optional).

    The reviewer's arc id is sourced from ``_caller_arc_id`` (auto-injected
    by ``dispatch_bridge.validate_and_dispatch`` and by the HTTP callback
    handler). An explicit ``reviewer_arc_id`` param is accepted as a
    fallback for direct in-process callers.
    """
    reviewer_arc_id = params.get("_caller_arc_id") or params.get("reviewer_arc_id")
    if reviewer_arc_id is None:
        raise ValueError(
            "review.submit_verdict requires caller arc context "
            "(missing _caller_arc_id)"
        )
    return review_manager.submit_verdict(
        reviewer_arc_id=reviewer_arc_id,
        target_arc_id=params["target_arc_id"],
        decision=params["decision"],
        reason=params.get("reason", ""),
    )
