"""Tests for carpenter.core.review_manager."""

import json
import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.workflows import review_manager
from carpenter.core.trust.audit import get_trust_events
from carpenter.db import get_db


# ── create_review_arc ────────────────────────────────────────────────

def test_create_review_arc_sets_integrity_level():
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    review_id = review_manager.create_review_arc(target, "security-review")

    review = arc_manager.get_arc(review_id)
    assert review["integrity_level"] == "trusted"
    assert review["agent_type"] == "REVIEWER"
    assert review["parent_id"] == parent


def test_create_review_arc_sets_review_target_state():
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    review_id = review_manager.create_review_arc(target, "reviewer")

    db = get_db()
    try:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = '_review_target'",
            (review_id,),
        ).fetchone()
    finally:
        db.close()
    assert row is not None
    assert json.loads(row["value_json"]) == target


def test_create_review_arc_target_not_found():
    with pytest.raises(ValueError, match="not found"):
        review_manager.create_review_arc(99999, "reviewer")


def test_create_review_arc_audit_event():
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    review_manager.create_review_arc(target, "reviewer")

    events = get_trust_events(arc_id=target, event_type="review_arc_created")
    assert len(events) >= 1


# ── submit_verdict ───────────────────────────────────────────────────

def test_submit_verdict_approve():
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    reviewer = review_manager.create_review_arc(target, "reviewer")

    result = review_manager.submit_verdict(reviewer, target, "approve", "looks good")
    assert result["accepted"] is True


def test_submit_verdict_records_history_on_target():
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    reviewer = review_manager.create_review_arc(target, "reviewer")

    review_manager.submit_verdict(reviewer, target, "approve", "ok")

    history = arc_manager.get_history(target)
    verdict_entries = [h for h in history if h["entry_type"] == "review_verdict"]
    assert len(verdict_entries) == 1
    content = json.loads(verdict_entries[0]["content_json"])
    assert content["decision"] == "approve"
    assert content["reviewer_arc_id"] == reviewer
    assert verdict_entries[0]["actor"] == "system"


def test_submit_verdict_invalid_reviewer():
    """Non-designated reviewer should be rejected."""
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    # Create a random arc that's NOT a reviewer
    random_arc = arc_manager.create_arc("random")

    with pytest.raises(ValueError, match="not a designated reviewer"):
        review_manager.submit_verdict(random_arc, target, "approve", "ok")


def test_submit_verdict_wrong_target():
    """Reviewer designated for one target can't review another."""
    parent = arc_manager.create_arc("parent")
    target1 = arc_manager.add_child(parent, "target-1", integrity_level="untrusted")
    target2 = arc_manager.add_child(parent, "target-2", integrity_level="untrusted")
    reviewer = review_manager.create_review_arc(target1, "reviewer")

    with pytest.raises(ValueError, match="designated for arc"):
        review_manager.submit_verdict(reviewer, target2, "approve", "ok")


def test_submit_verdict_invalid_decision():
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    reviewer = review_manager.create_review_arc(target, "reviewer")

    with pytest.raises(ValueError, match="Invalid decision"):
        review_manager.submit_verdict(reviewer, target, "maybe", "not sure")


def test_submit_verdict_trust_audit_events():
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    reviewer = review_manager.create_review_arc(target, "reviewer")

    review_manager.submit_verdict(reviewer, target, "approve", "ok")

    events = get_trust_events(arc_id=target, event_type="review_verdict")
    assert len(events) >= 1
