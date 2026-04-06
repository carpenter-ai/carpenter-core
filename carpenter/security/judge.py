"""Deterministic JUDGE for Carpenter.

JUDGE arcs run platform-level deterministic policy checks instead of
LLM agents. This module reads the reviewer's structured output from
arc state, runs policy validations against the security allowlists,
and returns a pass/fail result with a detailed check log.

Flow:
1. Dispatch handler calls run_policy_checks(judge_arc_id)
2. This module reads the reviewer's output from arc state
3. Runs deterministic policy checks on each extracted field
4. Returns JudgeResult(approved, checks, reason)
"""

import json
import logging
from dataclasses import dataclass, field

from ..db import get_db, db_connection
from ..core.arcs import manager as arc_manager
from ..core.trust.audit import log_trust_event
from .policies import get_policies
from .exceptions import PolicyValidationError

logger = logging.getLogger(__name__)


@dataclass
class PolicyCheck:
    """Result of a single policy check."""
    field_name: str
    policy_type: str
    value: str
    passed: bool
    reason: str = ""


@dataclass
class JudgeResult:
    """Result of deterministic judge evaluation."""
    approved: bool
    checks: list[PolicyCheck] = field(default_factory=list)
    reason: str = ""

    @property
    def failed_checks(self) -> list[PolicyCheck]:
        return [c for c in self.checks if not c.passed]


def run_policy_checks(judge_arc_id: int) -> JudgeResult:
    """Run deterministic policy checks for a JUDGE arc.

    Reads the review target arc, finds reviewer output in arc state,
    and validates each field against security policies.

    Args:
        judge_arc_id: The JUDGE arc ID.

    Returns:
        JudgeResult with approval status and check details.
    """
    judge_arc = arc_manager.get_arc(judge_arc_id)
    if not judge_arc:
        return JudgeResult(approved=False, reason=f"Judge arc {judge_arc_id} not found")

    # Find the review target
    target_arc_id = _get_review_target(judge_arc_id)
    if target_arc_id is None:
        return JudgeResult(approved=False, reason="No review target found for judge")

    # Get reviewer extraction data from arc state
    extraction_data = _get_extraction_data(target_arc_id)

    if not extraction_data:
        # No structured extraction data — approve by default (no policy constraints)
        log_trust_event(judge_arc_id, "judge_auto_approve", {
            "target_arc_id": target_arc_id,
            "reason": "no_extraction_data",
        })
        return JudgeResult(
            approved=True,
            reason="No structured extraction data to validate; approved by default",
        )

    # Run policy checks on each field
    policies = get_policies()
    checks = []

    for field_spec in extraction_data:
        field_name = field_spec.get("field", "unknown")
        policy_type = field_spec.get("policy_type", "")
        value = field_spec.get("value", "")

        if not policy_type:
            # No policy constraint on this field — skip
            checks.append(PolicyCheck(
                field_name=field_name,
                policy_type="none",
                value=str(value),
                passed=True,
                reason="No policy constraint",
            ))
            continue

        try:
            policies.validate(policy_type, value)
            checks.append(PolicyCheck(
                field_name=field_name,
                policy_type=policy_type,
                value=str(value),
                passed=True,
            ))
        except (PolicyValidationError, ValueError) as e:
            checks.append(PolicyCheck(
                field_name=field_name,
                policy_type=policy_type,
                value=str(value),
                passed=False,
                reason=str(e),
            ))

    result = JudgeResult(
        approved=all(c.passed for c in checks),
        checks=checks,
        reason="" if all(c.passed for c in checks) else "Policy check(s) failed",
    )

    # Audit log
    log_trust_event(judge_arc_id, "judge_policy_result", {
        "target_arc_id": target_arc_id,
        "approved": result.approved,
        "total_checks": len(checks),
        "failed_checks": len(result.failed_checks),
        "failures": [
            {"field": c.field_name, "policy_type": c.policy_type, "reason": c.reason}
            for c in result.failed_checks
        ],
    })

    return result


def _get_review_target(judge_arc_id: int) -> int | None:
    """Get the target arc ID for a judge arc."""
    with db_connection() as db:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = '_review_target'",
            (judge_arc_id,),
        ).fetchone()
        if row:
            return json.loads(row["value_json"])
        return None


def _get_extraction_data(target_arc_id: int) -> list[dict] | None:
    """Get the structured extraction data from reviewers for a target arc.

    Looks for '_extraction_output' key in the arc state of reviewer arcs
    that target this arc. Falls back to '_judge_policy_checks' on the
    target itself.

    Returns a list of field specs: [{"field": str, "policy_type": str, "value": any}]
    """
    with db_connection() as db:
        # First check target arc's own state for explicit policy checks
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = '_judge_policy_checks'",
            (target_arc_id,),
        ).fetchone()
        if row:
            data = json.loads(row["value_json"])
            if isinstance(data, list):
                return data

        # Then look for reviewer extraction output
        # Find all reviewer arcs that target this arc
        reviewer_rows = db.execute(
            "SELECT arc_id FROM arc_state WHERE key = '_review_target' AND value_json = ?",
            (json.dumps(target_arc_id),),
        ).fetchall()

        for reviewer_row in reviewer_rows:
            reviewer_id = reviewer_row["arc_id"]
            extraction = db.execute(
                "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = '_extraction_output'",
                (reviewer_id,),
            ).fetchone()
            if extraction:
                data = json.loads(extraction["value_json"])
                if isinstance(data, list):
                    return data

        return None
