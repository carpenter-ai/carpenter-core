"""Judge-verification arc handling.

Runs deterministic Python-only boolean aggregation over sibling
verification arcs (verify-quality, verify-correctness) and produces
a pass/fail verdict without invoking an AI agent. On FAIL, either
re-invokes the coding agent with feedback or escalates to human
review when the rework limit is reached.
"""

import json
import logging
import time

from ...db import db_connection, db_transaction
from . import CODING_CHANGE_PREFIX, manager as arc_manager, retry as arc_retry
from ..engine import work_queue
from ...agent import error_classifier

logger = logging.getLogger(__name__)


def _load_verification_config() -> tuple[int, int]:
    """Return (reason_max, summary_max) truncation limits from config."""
    from .verification import _get_verification_config
    _vcfg = _get_verification_config()
    reason_max = _vcfg.get("feedback_reason_max_length", 300)
    summary_max = _vcfg.get("feedback_summary_max_length", 500)
    return reason_max, summary_max


def _gather_check_results(
    siblings, arc_id: int, reason_max: int,
) -> tuple[list[dict], bool, list[str]]:
    """Inspect sibling verification arcs and collect their pass/fail state.

    Returns (check_results, all_passed, feedback_parts).
    """
    from .verification import get_arc_name as _get_vname

    check_results = []
    all_passed = True
    feedback_parts = []

    for sib in siblings:
        sib_name = sib["name"]
        sib_status = sib["status"]
        sib_role = sib["step_role"] if "step_role" in sib.keys() else None

        # Skip docs arc — it depends on the judge, not the other way around.
        # Prefer step_role; fall back to name-equality for legacy arcs.
        is_docs = (
            sib_role == "docs"
            if sib_role is not None
            else sib_name == _get_vname("documentation")
        )
        if is_docs:
            continue

        # Identify verifier arcs by step_role with name-equality fallback.
        is_verifier = (
            sib_role in ("verifier-correctness", "verifier-quality")
            if sib_role is not None
            else sib_name in (_get_vname("correctness_check"), _get_vname("quality_check"))
        )
        if is_verifier:
            passed = sib_status in ("completed", "frozen")
            check_results.append({
                "name": sib_name,
                "arc_id": sib["id"],
                "status": sib_status,
                "passed": passed,
            })
            if not passed:
                all_passed = False
                # Try to get failure reason from arc history
                history = arc_manager.get_history(sib["id"])
                error_entries = [
                    h for h in history
                    if h["entry_type"] in ("error", "failed", "verdict")
                ]
                reason = ""
                if error_entries:
                    last = error_entries[-1]
                    data = last.get("data_json")
                    if isinstance(data, str):
                        try:
                            data = json.loads(data)
                        except (json.JSONDecodeError, TypeError):
                            data = {}
                    elif data is None:
                        data = {}
                    reason = data.get("message", "") or data.get("reason", "") or str(data)
                feedback_parts.append(
                    f"- {sib_name} (arc #{sib['id']}): FAILED ({sib_status})"
                    + (f" — {reason[:reason_max]}" if reason else "")
                )

    return check_results, all_passed, feedback_parts


def _record_verdict(
    arc_id: int,
    verification_target_id: int,
    verdict: str,
    check_results: list[dict],
    feedback_parts: list[str],
) -> dict:
    """Persist the judge verdict to arc_state and history for both the judge
    arc and the target coding-change arc. Returns the summary dict.
    """
    from ..workflows.coding_change_handler import _set_arc_state

    summary = {
        "verdict": verdict,
        "checks": check_results,
        "feedback": "\n".join(feedback_parts) if feedback_parts else "",
    }

    # Store verdict in judge arc state and history
    with db_transaction() as db:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?) "
            "ON CONFLICT(arc_id, key) DO UPDATE SET value_json = excluded.value_json, "
            "updated_at = CURRENT_TIMESTAMP",
            (arc_id, "verdict", json.dumps(summary)),
        )

    arc_manager.add_history(
        arc_id, "judge_verdict",
        {"verdict": verdict, "checks": check_results},
    )

    # Also store verification summary on the target coding-change arc
    _set_arc_state(verification_target_id, "_verification_summary", summary)

    return summary


def _decide_rework(
    arc_id: int,
    verification_target_id: int,
    siblings,
    feedback_parts: list[str],
    summary_max: int,
) -> None:
    """Decide whether to re-invoke the coding agent or escalate to human
    review after a failed verdict. Always marks the judge arc completed.
    """
    from .verification import get_arc_name as _get_vname
    from ..workflows.coding_change_handler import (
        _get_arc_state, _set_arc_state, _notify_chat,
    )

    # Use arc_retry to decide whether to rework
    verification_feedback = "\n".join(feedback_parts)
    error_info = error_classifier.ErrorInfo(
        type="VerificationError",
        retry_count=0,
        source_location="arc_dispatch_handler._handle_judge_verification",
        message=f"Verification failed: {verification_feedback[:summary_max]}",
    )
    decision = arc_retry.should_retry_arc(verification_target_id, error_info)

    if decision.should_retry:
        # Record retry attempt (increments _retry_count)
        arc_retry.record_retry_attempt(
            verification_target_id, error_info, decision.backoff_seconds,
        )
        retry_state = arc_retry.get_retry_state(verification_target_id)
        retry_count = retry_state.get("_retry_count", 1)

        # Cancel the docs arc (not needed yet). Prefer step_role-based
        # match; fall back to name-equality for legacy arcs.
        for sib in siblings:
            sib_role = sib["step_role"] if "step_role" in sib.keys() else None
            is_docs = (
                sib_role == "docs"
                if sib_role is not None
                else sib["name"] == _get_vname("documentation")
            )
            if is_docs and sib["status"] == "pending":
                arc_manager.update_status(sib["id"], "cancelled")

        # Also bump rework_count so workspace is reused
        rework_count = _get_arc_state(verification_target_id, "rework_count", 0)
        _set_arc_state(verification_target_id, "rework_count", rework_count + 1)

        # Determine max retries for display
        max_retries = retry_state.get("_max_retries", 2)

        # Build verification feedback prompt
        from ...agent import templates
        original_prompt = _get_arc_state(
            verification_target_id, "original_prompt", "",
        )
        source_dir = _get_arc_state(verification_target_id, "source_dir", "")
        revised_prompt = templates.render(
            "verification_feedback",
            original_prompt=original_prompt,
            retry_count=retry_count,
            max_retries=max_retries,
            verification_feedback=verification_feedback,
        )

        # Clear verification pending flag (new cycle will set it again)
        _set_arc_state(verification_target_id, "_verification_pending", False)

        # Determine the correct invoke-agent event type based on target arc
        target_arc = arc_manager.get_arc(verification_target_id)
        target_name = target_arc.get("name", "") if target_arc else ""
        if target_name.startswith(f"external-{CODING_CHANGE_PREFIX}"):
            invoke_event = f"external-{CODING_CHANGE_PREFIX}.invoke-agent"
        else:
            invoke_event = f"{CODING_CHANGE_PREFIX}.invoke-agent"

        # Re-enqueue coding agent — preserve target arc's priority
        _target_arc = arc_manager.get_arc(verification_target_id)
        _target_priority = (_target_arc or {}).get("priority", 100)
        work_queue.enqueue(
            invoke_event,
            {
                "arc_id": verification_target_id,
                "source_dir": source_dir,
                "prompt": revised_prompt,
                "coding_agent": _get_arc_state(
                    verification_target_id, "coding_agent",
                ),
            },
            idempotency_key=f"{CODING_CHANGE_PREFIX}-vrework-{verification_target_id}-{int(time.time())}",
            max_retries=work_queue.SINGLE_ATTEMPT,
            priority=_target_priority,
        )

        _notify_chat(
            verification_target_id,
            f"Verification failed (attempt {retry_count}/{max_retries}). "
            f"Re-invoking coding agent with feedback...",
        )

        # Mark judge completed
        arc_manager.update_status(arc_id, "completed")
        arc_manager.freeze_arc(arc_id)

        logger.info(
            "Verification rework %d/%d for target arc %d",
            retry_count, max_retries, verification_target_id,
        )
    else:
        # Rework limit reached — proceed to human review with failure noted
        retry_state = arc_retry.get_retry_state(verification_target_id)
        retry_count = retry_state.get("_retry_count", 0)
        logger.warning(
            "Verification rework limit reached for target arc %d "
            "(%d attempts). Proceeding to human review.",
            verification_target_id, retry_count,
        )

        # Cancel the docs arc (verification failed, skip docs). Prefer
        # step_role-based match; fall back to name-equality for legacy arcs.
        for sib in siblings:
            sib_role = sib["step_role"] if "step_role" in sib.keys() else None
            is_docs = (
                sib_role == "docs"
                if sib_role is not None
                else sib["name"] == _get_vname("documentation")
            )
            if is_docs and sib["status"] == "pending":
                arc_manager.update_status(sib["id"], "cancelled")

        # Clear verification pending flag
        _set_arc_state(verification_target_id, "_verification_pending", False)

        # Transition coding-change arc to waiting for human review
        target_arc = arc_manager.get_arc(verification_target_id)
        if target_arc and target_arc["status"] == "active":
            arc_manager.update_status(verification_target_id, "waiting")

        _notify_chat(
            verification_target_id,
            f"AI verification failed after {retry_count} rework attempts. "
            f"Proceeding to human review.\n\n"
            f"Verification issues:\n{chr(10).join(feedback_parts)}",
        )

        # Mark judge completed
        arc_manager.update_status(arc_id, "completed")
        arc_manager.freeze_arc(arc_id)


async def _handle_judge_verification(arc_id: int, arc_info: dict) -> None:
    """Handle judge-verification arc with Python-only boolean aggregation.

    Reads sibling verification arcs (verify-quality, verify-correctness),
    aggregates their statuses, and produces a pass/fail verdict without
    invoking an AI agent.

    On PASS: marks judge completed, lets docs arc run via standard propagation.
    On FAIL: marks judge completed, cancels docs arc, re-invokes the coding
        agent with verification feedback (up to a configurable limit).
    """
    from .dispatch_handler import _propagate_completion

    # Load truncation limits from config
    reason_max, summary_max = _load_verification_config()

    verification_target_id = arc_info.get("verification_target_id")
    parent_id = arc_info.get("parent_id")

    # Activate the arc (pending -> active)
    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    if verification_target_id is None:
        logger.error("judge-verification arc %d has no verification_target_id", arc_id)
        arc_manager.update_status(arc_id, "failed")
        return

    # Find sibling verification arcs via parent + verification_target_id.
    # step_role is included so dispatch keying can prefer role; name is
    # retained for the legacy fallback path and for human-readable feedback.
    with db_connection() as db:
        siblings = db.execute(
            "SELECT id, name, status, step_role FROM arcs "
            "WHERE parent_id = ? AND verification_target_id = ? "
            "AND id != ?",
            (parent_id, verification_target_id, arc_id),
        ).fetchall()

    # Collect check results
    check_results, all_passed, feedback_parts = _gather_check_results(
        siblings, arc_id, reason_max,
    )

    verdict = "pass" if all_passed else "fail"

    _record_verdict(
        arc_id, verification_target_id, verdict, check_results, feedback_parts,
    )

    if all_passed:
        logger.info(
            "judge-verification arc %d: PASS (%d checks passed) for target %d",
            arc_id, len(check_results), verification_target_id,
        )
        # Mark judge completed — standard propagation will dispatch docs arc
        arc_manager.update_status(arc_id, "completed")
        arc_manager.freeze_arc(arc_id)
        _propagate_completion(arc_id)
    else:
        logger.info(
            "judge-verification arc %d: FAIL for target %d: %s",
            arc_id, verification_target_id, feedback_parts,
        )
        _decide_rework(
            arc_id, verification_target_id, siblings, feedback_parts, summary_max,
        )
