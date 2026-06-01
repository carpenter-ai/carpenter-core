"""Tests for the JUDGE -> Resource template_verdict side-effect path.

Verifies that when a JUDGE's review verdict is submitted for an arc
whose reviewer has ``_review_target_resource_id`` in arc_state, the
corresponding Resource's ``template_verdict`` flips approved/rejected.

Per PR2 scope, this is additive to the existing arc-promotion path in
``review_manager._check_and_promote`` — no regression to that path.
"""

import json

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager
from carpenter.core.workflows import review_manager
from carpenter.db import db_transaction, db_connection


def _make_judge_reviewer(target_arc_id: int) -> int:
    """Create a review arc and promote its agent_type to JUDGE.

    ``create_review_arc`` hardcodes REVIEWER.  Tests need JUDGE arcs to
    exercise the verdict side-effects, and there's no public helper for
    that yet — poke agent_type directly.
    """
    reviewer = review_manager.create_review_arc(target_arc_id, "judge")
    with db_transaction() as db:
        db.execute(
            "UPDATE arcs SET agent_type = 'JUDGE' WHERE id = ?",
            (reviewer,),
        )
    return reviewer


def _pin_resource_to_reviewer(reviewer_arc_id: int, resource_id: int) -> None:
    """Set _review_target_resource_id on the reviewer arc's state.

    Simulates what PR3's fetch_web_content will do when wiring the
    template arc pipeline.
    """
    with db_transaction() as db:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) "
            "VALUES (?, ?, ?)",
            (reviewer_arc_id, "_review_target_resource_id", json.dumps(resource_id)),
        )


def _make_target_and_derived_resource() -> tuple[int, int, int]:
    """Create a parent + untrusted target arc + a pending Resource.

    Returns (parent_id, target_arc_id, resource_id).  The resource is
    "derived" (has produced_by_template) so it can receive verdicts.
    """
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    resource_id = res_manager.derive_resource(
        content_type="text-summary",
        file_path=None,
        produced_by_arc_id=target,
        produced_by_template="html_to_summary",
    )
    return parent, target, resource_id


class TestJudgeVerdictFlipsResourceVerdict:
    def test_approve_flips_resource_to_approved(self):
        _, target, rid = _make_target_and_derived_resource()
        judge = _make_judge_reviewer(target)
        _pin_resource_to_reviewer(judge, rid)

        review_manager.submit_verdict(judge, target, "approve", "ok")

        row = res_manager.get_resource(rid)
        assert row["template_verdict"] == "approved"

    def test_reject_flips_resource_to_rejected(self):
        _, target, rid = _make_target_and_derived_resource()
        judge = _make_judge_reviewer(target)
        _pin_resource_to_reviewer(judge, rid)

        review_manager.submit_verdict(judge, target, "reject", "bad content")

        row = res_manager.get_resource(rid)
        assert row["template_verdict"] == "rejected"

    def test_no_resource_key_is_noop_for_resources(self):
        """Existing review flow without a Resource is unchanged."""
        parent = arc_manager.create_arc("parent")
        target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
        judge = _make_judge_reviewer(target)
        # Intentionally do NOT pin a resource.

        result = review_manager.submit_verdict(judge, target, "approve", "ok")
        assert result["accepted"] is True
        # Arc still gets promoted by the existing path.
        assert result["promoted"] is True

        arc = arc_manager.get_arc(target)
        assert arc["integrity_level"] == "trusted"

    def test_non_judge_reviewer_does_not_flip_resource(self):
        """REVIEWER (non-JUDGE) verdicts must not affect Resource verdict."""
        _, target, rid = _make_target_and_derived_resource()
        # Regular reviewer — NOT upgraded to JUDGE.
        reviewer = review_manager.create_review_arc(target, "reviewer")
        _pin_resource_to_reviewer(reviewer, rid)

        review_manager.submit_verdict(reviewer, target, "approve", "ok")

        row = res_manager.get_resource(rid)
        # Still pending — REVIEWER verdicts are advisory for Resources too.
        assert row["template_verdict"] == "pending"

    def test_arc_promotion_path_still_runs_alongside_resource_flip(self):
        """When a JUDGE approves, both the arc AND the Resource flip."""
        _, target, rid = _make_target_and_derived_resource()
        judge = _make_judge_reviewer(target)
        _pin_resource_to_reviewer(judge, rid)

        result = review_manager.submit_verdict(judge, target, "approve", "ok")

        assert result["promoted"] is True
        arc = arc_manager.get_arc(target)
        assert arc["integrity_level"] == "trusted"
        row = res_manager.get_resource(rid)
        assert row["template_verdict"] == "approved"

    def test_missing_resource_id_row_is_graceful(self):
        """Dangling _review_target_resource_id (resource deleted) is tolerated."""
        _, target, _rid = _make_target_and_derived_resource()
        judge = _make_judge_reviewer(target)
        # Pin a resource id that doesn't exist.
        _pin_resource_to_reviewer(judge, 999999)

        # Should not raise — the resources manager will raise ValueError,
        # which the wiring catches and logs.
        result = review_manager.submit_verdict(judge, target, "approve", "ok")
        assert result["accepted"] is True


class TestResourceVerdictState:
    def test_idempotent_repeat_approve_ok(self):
        _, target, rid = _make_target_and_derived_resource()
        judge = _make_judge_reviewer(target)
        _pin_resource_to_reviewer(judge, rid)

        # First approve.
        review_manager.submit_verdict(judge, target, "approve", "ok")
        row = res_manager.get_resource(rid)
        assert row["template_verdict"] == "approved"

        # Re-submitting approve on the Resource is a no-op at manager
        # level.  submit_verdict itself may raise on double-verdict from
        # the arc side, but the parallel Resource path is idempotent —
        # we verify that directly rather than through a second
        # submit_verdict call that might hit unrelated guards.
        res_manager.mark_template_verdict(rid, "approved")
        row2 = res_manager.get_resource(rid)
        assert row2["template_verdict"] == "approved"
