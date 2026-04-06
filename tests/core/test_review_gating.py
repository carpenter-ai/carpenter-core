"""Tests for review pass/fail gating.

Verifies that _check_review_verdicts blocks children when a preceding
required_pass sibling has a non-pass verdict.
"""

import json

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.arcs import dispatch_handler as arc_dispatch_handler
from carpenter.db import get_db


def _set_arc_state(arc_id, key, value):
    """Helper to set arc_state for test setup."""
    db = get_db()
    try:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?) "
            "ON CONFLICT(arc_id, key) DO UPDATE SET value_json = excluded.value_json",
            (arc_id, key, json.dumps(value)),
        )
        db.commit()
    finally:
        db.close()


class TestReviewGating:
    """Tests for _check_review_verdicts gating logic."""

    def test_child_blocked_by_fail_verdict(self, test_db):
        """Child should be blocked when a required_pass sibling has fail verdict."""
        parent = arc_manager.create_arc("project", goal="Test gating")

        # Step at order 4: static-analysis (required_pass, completed with fail verdict)
        step1 = arc_manager.create_arc(
            name="static-analysis", goal="Run analysis",
            parent_id=parent, step_order=4,
        )
        arc_manager.update_status(step1, "active")
        arc_manager.update_status(step1, "completed")
        _set_arc_state(step1, "_required_pass", True)
        _set_arc_state(step1, "_verdict", "fail")

        # Step at order 6: human-approval (should be blocked)
        step2 = arc_manager.create_arc(
            name="human-approval", goal="Approve",
            parent_id=parent, step_order=6,
        )

        children = arc_manager.get_children(parent)
        child_dict = next(c for c in children if c["id"] == step2)

        result = arc_dispatch_handler._check_review_verdicts(child_dict, children)
        assert result is False

    def test_child_allowed_on_pass_verdict(self, test_db):
        """Child should be allowed when required_pass sibling has pass verdict."""
        parent = arc_manager.create_arc("project", goal="Test gating")

        step1 = arc_manager.create_arc(
            name="static-analysis", goal="Run analysis",
            parent_id=parent, step_order=4,
        )
        arc_manager.update_status(step1, "active")
        arc_manager.update_status(step1, "completed")
        _set_arc_state(step1, "_required_pass", True)
        _set_arc_state(step1, "_verdict", "pass")

        step2 = arc_manager.create_arc(
            name="human-approval", goal="Approve",
            parent_id=parent, step_order=6,
        )

        children = arc_manager.get_children(parent)
        child_dict = next(c for c in children if c["id"] == step2)

        result = arc_dispatch_handler._check_review_verdicts(child_dict, children)
        assert result is True

    def test_child_allowed_without_required_pass(self, test_db):
        """Child should be allowed when preceding sibling has no required_pass flag."""
        parent = arc_manager.create_arc("project", goal="Test gating")

        step1 = arc_manager.create_arc(
            name="propose-change", goal="Propose",
            parent_id=parent, step_order=1,
        )
        arc_manager.update_status(step1, "active")
        arc_manager.update_status(step1, "completed")

        step2 = arc_manager.create_arc(
            name="audit-impact", goal="Audit",
            parent_id=parent, step_order=2,
        )

        children = arc_manager.get_children(parent)
        child_dict = next(c for c in children if c["id"] == step2)

        result = arc_dispatch_handler._check_review_verdicts(child_dict, children)
        assert result is True

    def test_default_pass_when_no_verdict_set(self, test_db):
        """required_pass step that completed without _verdict defaults to pass."""
        parent = arc_manager.create_arc("project", goal="Test gating")

        step1 = arc_manager.create_arc(
            name="static-analysis", goal="Run analysis",
            parent_id=parent, step_order=4,
        )
        arc_manager.update_status(step1, "active")
        arc_manager.update_status(step1, "completed")
        _set_arc_state(step1, "_required_pass", True)
        # No _verdict set — should default to pass

        step2 = arc_manager.create_arc(
            name="human-approval", goal="Approve",
            parent_id=parent, step_order=6,
        )

        children = arc_manager.get_children(parent)
        child_dict = next(c for c in children if c["id"] == step2)

        result = arc_dispatch_handler._check_review_verdicts(child_dict, children)
        assert result is True

    def test_dict_verdict_with_verdict_key(self, test_db):
        """Verdict stored as dict with 'verdict' key should be read correctly."""
        parent = arc_manager.create_arc("project", goal="Test gating")

        step1 = arc_manager.create_arc(
            name="agentic-review", goal="Review",
            parent_id=parent, step_order=5,
        )
        arc_manager.update_status(step1, "active")
        arc_manager.update_status(step1, "completed")
        _set_arc_state(step1, "_required_pass", True)
        _set_arc_state(step1, "_verdict", {"verdict": "fail", "reason": "bad code"})

        step2 = arc_manager.create_arc(
            name="human-approval", goal="Approve",
            parent_id=parent, step_order=6,
        )

        children = arc_manager.get_children(parent)
        child_dict = next(c for c in children if c["id"] == step2)

        result = arc_dispatch_handler._check_review_verdicts(child_dict, children)
        assert result is False

    def test_pending_sibling_not_checked(self, test_db):
        """Pending (not completed) siblings should not be checked for verdict."""
        parent = arc_manager.create_arc("project", goal="Test gating")

        # Step at order 4: still pending
        step1 = arc_manager.create_arc(
            name="static-analysis", goal="Run analysis",
            parent_id=parent, step_order=4,
        )
        _set_arc_state(step1, "_required_pass", True)
        _set_arc_state(step1, "_verdict", "fail")
        # Status remains "pending" — should NOT be checked

        step2 = arc_manager.create_arc(
            name="human-approval", goal="Approve",
            parent_id=parent, step_order=6,
        )

        children = arc_manager.get_children(parent)
        child_dict = next(c for c in children if c["id"] == step2)

        result = arc_dispatch_handler._check_review_verdicts(child_dict, children)
        assert result is True
