"""Tests for batch arc creation with judge pattern (Phase B)."""

import pytest
from carpenter.tool_backends import arc as arc_backend
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.workflows import review_manager
from carpenter.db import get_db


def test_create_batch_basic():
    """Test basic batch arc creation."""
    result = arc_backend.handle_create_batch({
        "arcs": [
            {"name": "arc1", "goal": "First"},
            {"name": "arc2", "goal": "Second"},
        ]
    })

    assert "arc_ids" in result
    assert len(result["arc_ids"]) == 2

    # Verify arcs were created
    arc1 = arc_manager.get_arc(result["arc_ids"][0])
    arc2 = arc_manager.get_arc(result["arc_ids"][1])

    assert arc1["name"] == "arc1"
    assert arc2["name"] == "arc2"
    assert arc1["integrity_level"] == "trusted"
    assert arc2["integrity_level"] == "trusted"


def test_create_batch_untrusted_with_reviewers():
    """Test batch creation with untrusted arc + reviewers + judge."""
    result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": "tainted_target",
                "integrity_level": "untrusted",
                "output_type": "python",
            },
            {
                "name": "security_reviewer",
                "agent_type": "REVIEWER",
                "integrity_level": "trusted",
                "reviewer_profile": "security-reviewer",
                "step_order": 1,
            },
            {
                "name": "final_judge",
                "agent_type": "JUDGE",
                "integrity_level": "trusted",
                "reviewer_profile": "judge",
                "step_order": 2,
            },
        ]
    })

    assert "arc_ids" in result
    assert len(result["arc_ids"]) == 3

    tainted_id, reviewer_id, judge_id = result["arc_ids"]

    # Verify untrusted arc
    tainted = arc_manager.get_arc(tainted_id)
    assert tainted["integrity_level"] == "untrusted"

    # Verify reviewer arc
    reviewer = arc_manager.get_arc(reviewer_id)
    assert reviewer["agent_type"] == "REVIEWER"
    assert reviewer["integrity_level"] == "trusted"

    # Verify judge arc
    judge = arc_manager.get_arc(judge_id)
    assert judge["agent_type"] == "JUDGE"
    assert judge["integrity_level"] == "trusted"
    assert judge["step_order"] == 2

    # Verify reviewer_profile stored in arc_state
    db = get_db()
    try:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = '_reviewer_profile'",
            (reviewer_id,),
        ).fetchone()
        assert row is not None
        import json
        assert json.loads(row["value_json"]) == "security-reviewer"
    finally:
        db.close()


def test_create_batch_untrusted_without_reviewers():
    """Test that untrusted arcs without reviewers are rejected."""
    result = arc_backend.handle_create_batch({
        "arcs": [
            {"name": "tainted_target", "integrity_level": "untrusted"},
        ]
    })

    assert "error" in result
    assert "require at least one REVIEWER or JUDGE" in result["error"]


def test_create_batch_invalid_reviewer_profile():
    """Test that unknown reviewer_profile is rejected."""
    result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": "reviewer",
                "agent_type": "REVIEWER",
                "reviewer_profile": "nonexistent",
            },
        ]
    })

    assert "error" in result
    assert "Unknown agent_role" in result["error"]


def test_create_batch_multiple_judges():
    """Test that multiple judges are rejected."""
    result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": "tainted",
                "integrity_level": "untrusted",
            },
            {
                "name": "judge1",
                "agent_type": "JUDGE",
                "reviewer_profile": "judge",
            },
            {
                "name": "judge2",
                "agent_type": "JUDGE",
                "reviewer_profile": "judge",
            },
        ]
    })

    assert "error" in result
    assert "Maximum one JUDGE" in result["error"]


def test_create_batch_judge_not_highest_step_order():
    """Test that judge must have highest step_order among reviewers."""
    result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": "tainted",
                "integrity_level": "untrusted",
            },
            {
                "name": "reviewer",
                "agent_type": "REVIEWER",
                "reviewer_profile": "security-reviewer",
                "step_order": 2,
            },
            {
                "name": "judge",
                "agent_type": "JUDGE",
                "reviewer_profile": "judge",
                "step_order": 1,
            },
        ]
    })

    assert "error" in result
    assert "highest step_order" in result["error"]


def test_create_batch_generates_encryption_keys():
    """Test that Fernet encryption keys are generated for untrusted arcs."""
    result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": "tainted",
                "integrity_level": "untrusted",
            },
            {
                "name": "reviewer",
                "agent_type": "REVIEWER",
                "reviewer_profile": "security-reviewer",
            },
        ]
    })

    assert "arc_ids" in result
    tainted_id, reviewer_id = result["arc_ids"]

    # Verify encryption key exists
    db = get_db()
    try:
        row = db.execute(
            "SELECT fernet_key_encrypted FROM review_keys "
            "WHERE target_arc_id = ? AND reviewer_arc_id = ?",
            (tainted_id, reviewer_id),
        ).fetchone()
        assert row is not None
        assert row["fernet_key_encrypted"] is not None
    finally:
        db.close()


def test_judge_approval_promotes_target():
    """Test that judge approval promotes the target arc."""
    # Create batch with untrusted + judge
    result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": "tainted",
                "integrity_level": "untrusted",
            },
            {
                "name": "judge",
                "agent_type": "JUDGE",
                "reviewer_profile": "judge",
            },
        ]
    })

    tainted_id, judge_id = result["arc_ids"]

    # Judge approves
    review_result = review_manager.submit_verdict(
        reviewer_arc_id=judge_id,
        target_arc_id=tainted_id,
        decision="approve",
        reason="Looks good",
    )

    assert review_result["accepted"] is True
    assert review_result["promoted"] is True

    # Verify target is promoted
    tainted = arc_manager.get_arc(tainted_id)
    assert tainted["integrity_level"] == "trusted"


def test_judge_rejection_fails_target():
    """Test that judge rejection fails the target arc."""
    # Create batch with untrusted + judge
    result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": "tainted",
                "integrity_level": "untrusted",
            },
            {
                "name": "judge",
                "agent_type": "JUDGE",
                "reviewer_profile": "judge",
            },
        ]
    })

    tainted_id, judge_id = result["arc_ids"]

    # Activate target arc first
    arc_manager.update_status(tainted_id, "active")

    # Judge rejects
    review_result = review_manager.submit_verdict(
        reviewer_arc_id=judge_id,
        target_arc_id=tainted_id,
        decision="reject",
        reason="Security issue",
    )

    assert review_result["accepted"] is True
    assert review_result["promoted"] is False

    # Verify target is failed
    tainted = arc_manager.get_arc(tainted_id)
    assert tainted["status"] == "failed"


def test_individual_reviewer_verdict_ignored():
    """Test that individual reviewer verdicts don't trigger promotion."""
    # Create batch with untrusted + reviewer + judge
    result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": "tainted",
                "integrity_level": "untrusted",
            },
            {
                "name": "reviewer",
                "agent_type": "REVIEWER",
                "reviewer_profile": "security-reviewer",
            },
            {
                "name": "judge",
                "agent_type": "JUDGE",
                "reviewer_profile": "judge",
            },
        ]
    })

    tainted_id, reviewer_id, judge_id = result["arc_ids"]

    # Reviewer approves (should not promote)
    review_result = review_manager.submit_verdict(
        reviewer_arc_id=reviewer_id,
        target_arc_id=tainted_id,
        decision="approve",
        reason="Security looks good",
    )

    assert review_result["accepted"] is True
    assert review_result["promoted"] is False

    # Verify target is still untrusted
    tainted = arc_manager.get_arc(tainted_id)
    assert tainted["integrity_level"] == "untrusted"


def test_untrusted_child_does_not_affect_parent():
    """Test that adding an untrusted child does NOT affect the trusted parent."""
    # Create parent
    parent_id = arc_manager.create_arc("parent", "Parent goal")

    # Create batch with untrusted arc as child
    result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": "tainted_child",
                "parent_id": parent_id,
                "integrity_level": "untrusted",
            },
            {
                "name": "judge",
                "parent_id": parent_id,
                "agent_type": "JUDGE",
                "reviewer_profile": "judge",
            },
        ]
    })

    # Parent stays trusted -- I2 (HTTP 403) is the real enforcement
    parent = arc_manager.get_arc(parent_id)
    assert parent["integrity_level"] == "trusted"


def test_judge_approval_promotes_child_parent_stays_trusted():
    """Test that judge approval promotes the target; parent stays trusted."""
    # Create parent
    parent_id = arc_manager.create_arc("parent", "Parent goal")

    # Create batch with untrusted child + judge
    result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": "tainted_child",
                "parent_id": parent_id,
                "integrity_level": "untrusted",
            },
            {
                "name": "judge",
                "parent_id": parent_id,
                "agent_type": "JUDGE",
                "reviewer_profile": "judge",
            },
        ]
    })

    child_id, judge_id = result["arc_ids"]

    # Parent stays trusted (no upward propagation)
    parent = arc_manager.get_arc(parent_id)
    assert parent["integrity_level"] == "trusted"

    # Judge approves child
    review_manager.submit_verdict(
        reviewer_arc_id=judge_id,
        target_arc_id=child_id,
        decision="approve",
    )

    # Child promoted, parent still trusted
    child = arc_manager.get_arc(child_id)
    parent = arc_manager.get_arc(parent_id)
    assert child["integrity_level"] == "trusted"
    assert parent["integrity_level"] == "trusted"


def test_create_individual_untrusted_arc_rejected():
    """Test that individual untrusted arc creation is rejected."""
    with pytest.raises(ValueError, match="Cannot create individual untrusted arc"):
        arc_manager.create_arc(
            "tainted",
            integrity_level="untrusted",
        )


def test_batch_rollback_on_error():
    """Test that batch creation is atomic (all-or-nothing)."""
    # Try to create batch with invalid parent_id consistency
    result = arc_backend.handle_create_batch({
        "arcs": [
            {"name": "arc1", "parent_id": 1},
            {"name": "arc2", "parent_id": 2},  # Different parent -- should fail
        ]
    })

    assert "error" in result

    # Verify no arcs were created
    db = get_db()
    try:
        count = db.execute(
            "SELECT COUNT(*) as cnt FROM arcs WHERE name IN ('arc1', 'arc2')"
        ).fetchone()
        assert count["cnt"] == 0
    finally:
        db.close()


def test_auto_assign_step_order():
    """Test that step_order is auto-assigned if not provided."""
    result = arc_backend.handle_create_batch({
        "arcs": [
            {"name": "arc1"},
            {"name": "arc2"},
            {"name": "arc3"},
        ]
    })

    arc1, arc2, arc3 = [arc_manager.get_arc(id) for id in result["arc_ids"]]

    # Verify step_order is sequential
    assert arc1["step_order"] == 0
    assert arc2["step_order"] == 1
    assert arc3["step_order"] == 2


def test_reviewer_target_linkage():
    """Test that reviewers are linked to untrusted targets via arc_state."""
    result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": "tainted",
                "integrity_level": "untrusted",
            },
            {
                "name": "reviewer",
                "agent_type": "REVIEWER",
                "reviewer_profile": "security-reviewer",
            },
        ]
    })

    tainted_id, reviewer_id = result["arc_ids"]

    # Verify _review_target linkage
    db = get_db()
    try:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = '_review_target'",
            (reviewer_id,),
        ).fetchone()
        assert row is not None
        import json
        target_id = json.loads(row["value_json"])
        assert target_id == tainted_id
    finally:
        db.close()
