"""Review infrastructure for trust boundary system.

Manages review arcs, verdicts, trust promotion, and no-dangling validation.
Review arcs are the only bridge from untrusted to trusted zones.
"""

import json
import logging

from ...db import get_db, db_connection, db_transaction
from ..arcs import manager as arc_manager
from ..engine import event_bus
from ..trust.audit import log_trust_event

logger = logging.getLogger(__name__)


def create_review_arc(
    target_arc_id: int,
    reviewer_name: str,
    reviewer_goal: str | None = None,
) -> int:
    """Create a review arc as sibling of the target.

    The review arc is created with integrity_level='trusted' and
    agent_type='REVIEWER'. A _review_target state key is set to
    link it to the target arc. Reviewers are trusted agents that
    can read untrusted data.

    Args:
        target_arc_id: The untrusted arc to review.
        reviewer_name: Name for the review arc.
        reviewer_goal: Optional goal description.

    Returns:
        The review arc ID.

    Raises:
        ValueError: If target arc not found.
    """
    target = arc_manager.get_arc(target_arc_id)
    if target is None:
        raise ValueError(f"Target arc {target_arc_id} not found")

    parent_id = target["parent_id"]

    review_arc_id = arc_manager.create_arc(
        name=reviewer_name,
        goal=reviewer_goal or f"Review output of arc #{target_arc_id}",
        parent_id=parent_id,
        integrity_level="trusted",
        agent_type="REVIEWER",
    )

    # Link review arc to target via arc state
    with db_transaction() as db:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
            (review_arc_id, "_review_target", json.dumps(target_arc_id)),
        )

    log_trust_event(target_arc_id, "review_arc_created", {
        "review_arc_id": review_arc_id,
        "reviewer_name": reviewer_name,
    })

    return review_arc_id




def submit_verdict(
    reviewer_arc_id: int,
    target_arc_id: int,
    decision: str,
    reason: str = "",
) -> dict:
    """Submit a review verdict from a reviewer arc.

    Args:
        reviewer_arc_id: The review arc submitting the verdict.
        target_arc_id: The tainted arc being reviewed.
        decision: 'approve' or 'reject'.
        reason: Explanation for the verdict.

    Returns:
        Dict with 'accepted' bool and optional 'promoted' bool.

    Raises:
        ValueError: If reviewer not designated for target, or invalid decision.
    """
    if decision not in ("approve", "reject"):
        raise ValueError(f"Invalid decision '{decision}'. Must be 'approve' or 'reject'.")

    # Validate reviewer is designated for target
    with db_connection() as db:
        row = db.execute(
            "SELECT value_json FROM arc_state "
            "WHERE arc_id = ? AND key = '_review_target'",
            (reviewer_arc_id,),
        ).fetchone()

    if row is None:
        raise ValueError(f"Arc {reviewer_arc_id} is not a designated reviewer")

    linked_target = json.loads(row["value_json"])
    if linked_target != target_arc_id:
        raise ValueError(
            f"Reviewer arc {reviewer_arc_id} is designated for arc {linked_target}, "
            f"not arc {target_arc_id}"
        )

    # Record verdict as system-actor history on the TARGET arc
    arc_manager.add_history(
        target_arc_id,
        "review_verdict",
        {
            "reviewer_arc_id": reviewer_arc_id,
            "decision": decision,
            "reason": reason,
        },
        actor="system",
    )

    log_trust_event(target_arc_id, "review_verdict", {
        "reviewer_arc_id": reviewer_arc_id,
        "decision": decision,
        "reason": reason,
    })

    if decision == "approve":
        promoted = _check_and_promote(target_arc_id)
        return {"accepted": True, "promoted": promoted}
    else:
        _handle_rejection(target_arc_id, reviewer_arc_id, reason)
        return {"accepted": True, "promoted": False}


def _get_review_arcs_for_target(target_arc_id: int) -> list[int]:
    """Find all review arcs designated for a target arc."""
    with db_connection() as db:
        rows = db.execute(
            "SELECT arc_id FROM arc_state "
            "WHERE key = '_review_target' AND value_json = ?",
            (json.dumps(target_arc_id),),
        ).fetchall()
        return [row["arc_id"] for row in rows]


def _get_verdicts(target_arc_id: int) -> list[dict]:
    """Get all review verdicts for a target arc from its history."""
    history = arc_manager.get_history(target_arc_id)
    verdicts = []
    for entry in history:
        if entry["entry_type"] == "review_verdict":
            content = json.loads(entry["content_json"])
            verdicts.append(content)
    return verdicts


def _check_and_promote(target_arc_id: int) -> bool:
    """Check if judge has approved and promote trust if so.

    Judge pattern: Only the judge's verdict matters. Individual reviewer
    verdicts are advisory only.

    Returns True if promoted, False otherwise.
    """
    verdicts = _get_verdicts(target_arc_id)
    reviewer_arcs = _get_review_arcs_for_target(target_arc_id)

    # Find the judge verdict (agent_type='JUDGE')
    with db_connection() as db:
        judge_verdict = None
        for v in verdicts:
            reviewer_id = v["reviewer_arc_id"]
            row = db.execute(
                "SELECT agent_type FROM arcs WHERE id = ?",
                (reviewer_id,),
            ).fetchone()
            if row and row["agent_type"] == "JUDGE":
                judge_verdict = v
                break

        # If no judge verdict yet, cannot promote
        if judge_verdict is None:
            return False

        # If judge rejected, do not promote
        if judge_verdict["decision"] == "reject":
            return False

        # Judge approved — promote target
        if judge_verdict["decision"] == "approve":
            # Invariant I3: only a JUDGE arc can promote trust
            judge_arc_id = judge_verdict["reviewer_arc_id"]
            judge_row = db.execute(
                "SELECT agent_type FROM arcs WHERE id = ?",
                (judge_arc_id,),
            ).fetchone()
            if not judge_row or judge_row["agent_type"] != "JUDGE":
                raise RuntimeError(
                    f"Trust invariant violation (I3): arc {judge_arc_id} attempted "
                    f"trust promotion but has agent_type "
                    f"'{judge_row['agent_type'] if judge_row else 'missing'}', not 'JUDGE'"
                )

            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            db.execute(
                "UPDATE arcs SET integrity_level = 'trusted', updated_at = ? WHERE id = ?",
                (now, target_arc_id),
            )
            db.commit()

    # Log trust events after closing DB connection
    log_trust_event(target_arc_id, "trust_promoted", {
        "judge_verdict": judge_verdict["decision"],
        "judge_arc_id": judge_verdict["reviewer_arc_id"],
    })

    event_bus.record_event("trust.promoted", {
        "arc_id": target_arc_id,
        "judge_arc_id": judge_verdict["reviewer_arc_id"],
    }, source="review_manager")

    return True


def _handle_rejection(
    target_arc_id: int,
    reviewer_arc_id: int,
    reason: str,
) -> None:
    """Handle a rejection verdict.

    If rejection is from JUDGE, immediately fail the target arc.
    Individual reviewer rejections are advisory only.
    """
    # Check if rejecting arc is a JUDGE
    with db_connection() as db:
        row = db.execute(
            "SELECT agent_type FROM arcs WHERE id = ?",
            (reviewer_arc_id,),
        ).fetchone()

    if row and row["agent_type"] == "JUDGE":
        # Judge rejected — immediately fail the target
        target = arc_manager.get_arc(target_arc_id)
        if target and target["status"] not in arc_manager.FROZEN_STATUSES:
            try:
                arc_manager.update_status(target_arc_id, "active")
            except ValueError:
                pass  # May already be active
            try:
                arc_manager.update_status(target_arc_id, "failed")
            except ValueError:
                pass  # May already be failed

        arc_manager.add_history(
            target_arc_id,
            "escalation",
            {
                "reason": "Judge rejected",
                "judge_arc_id": reviewer_arc_id,
                "rejection_reason": reason,
            },
            actor="system",
        )
    # else: Individual reviewer rejection is advisory only, no action needed


