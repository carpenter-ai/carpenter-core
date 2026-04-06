"""Tests for ad-hoc AI review (request_ai_review)."""

import json

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.db import get_db
from carpenter.tool_backends.arc import handle_request_ai_review


def _create_waiting_coding_change_arc() -> int:
    """Helper: create a coding-change arc in 'waiting' status with diff in arc_state."""
    arc_id = arc_manager.create_arc(
        name="coding-change: test",
        goal="test coding change",
    )

    # Add template children (mimics real coding-change template)
    db = get_db()
    try:
        # Add await-approval child at step_order 2
        db.execute(
            "INSERT INTO arcs (name, goal, parent_id, step_order, depth, "
            "integrity_level, output_type, agent_type, arc_role, from_template) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("await-approval", "Wait for human review", arc_id, 2, 1,
             "trusted", "python", "EXECUTOR", "worker", True),
        )

        # Store diff and workspace_path in arc_state
        for key, value in [
            ("diff", "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new"),
            ("workspace_path", "/tmp/test-workspace"),
            ("review_id", "test-review-123"),
        ]:
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                (arc_id, key, json.dumps(value)),
            )

        db.commit()
    finally:
        db.close()

    # Set arc to 'waiting' status
    arc_manager.update_status(arc_id, "active")
    arc_manager.update_status(arc_id, "waiting")

    return arc_id


def test_request_ai_review_creates_reviewer_arc():
    """handle_request_ai_review creates a REVIEWER child with correct properties."""
    arc_id = _create_waiting_coding_change_arc()

    result = handle_request_ai_review({
        "target_arc_id": arc_id,
        "model": "sonnet",
    })

    assert "error" not in result
    reviewer_id = result["arc_id"]
    assert isinstance(reviewer_id, int)

    reviewer = arc_manager.get_arc(reviewer_id)
    assert reviewer["parent_id"] == arc_id
    assert reviewer["agent_type"] == "REVIEWER"
    assert reviewer["arc_role"] == "worker"
    assert reviewer["step_order"] == 2  # same as await-approval
    assert reviewer["agent_config_id"] is not None

    # Verify agent_config points to a Sonnet model
    db = get_db()
    try:
        row = db.execute(
            "SELECT model FROM agent_configs WHERE id = ?",
            (reviewer["agent_config_id"],),
        ).fetchone()
    finally:
        db.close()

    assert row is not None
    assert "sonnet" in row["model"].lower()


def test_request_ai_review_rejects_non_waiting_arc():
    """handle_request_ai_review rejects arcs not in 'waiting' status."""
    arc_id = arc_manager.create_arc(name="test-arc", goal="test")

    result = handle_request_ai_review({
        "target_arc_id": arc_id,
        "model": "sonnet",
    })

    assert "error" in result
    assert "waiting" in result["error"]


def test_request_ai_review_rejects_missing_diff():
    """handle_request_ai_review rejects arcs with no diff in arc_state."""
    arc_id = arc_manager.create_arc(name="test-arc", goal="test")
    arc_manager.update_status(arc_id, "active")
    arc_manager.update_status(arc_id, "waiting")

    result = handle_request_ai_review({
        "target_arc_id": arc_id,
        "model": "sonnet",
    })

    assert "error" in result
    assert "diff" in result["error"].lower() or "No diff" in result["error"]


def test_request_ai_review_stores_review_target():
    """The reviewer arc has _review_target pointing to the target arc."""
    arc_id = _create_waiting_coding_change_arc()

    result = handle_request_ai_review({
        "target_arc_id": arc_id,
        "model": "sonnet",
    })

    reviewer_id = result["arc_id"]

    db = get_db()
    try:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = '_review_target'",
            (reviewer_id,),
        ).fetchone()
    finally:
        db.close()

    assert row is not None
    assert json.loads(row["value_json"]) == arc_id


def test_request_ai_review_with_focus_areas():
    """Focus areas are included in the reviewer's goal."""
    arc_id = _create_waiting_coding_change_arc()

    result = handle_request_ai_review({
        "target_arc_id": arc_id,
        "model": "sonnet",
        "focus_areas": "security, performance",
    })

    reviewer_id = result["arc_id"]
    reviewer = arc_manager.get_arc(reviewer_id)
    assert "security, performance" in reviewer["goal"]


def test_request_ai_review_logs_history():
    """Requesting a review logs ad_hoc_review_requested on the target arc."""
    arc_id = _create_waiting_coding_change_arc()

    result = handle_request_ai_review({
        "target_arc_id": arc_id,
        "model": "sonnet",
    })

    history = arc_manager.get_history(arc_id)
    review_entries = [h for h in history if h["entry_type"] == "ad_hoc_review_requested"]
    assert len(review_entries) == 1
    content = json.loads(review_entries[0]["content_json"])
    assert content["model"] == "sonnet"
    assert content["reviewer_arc_id"] == result["arc_id"]


def test_request_ai_review_invalid_model():
    """handle_request_ai_review rejects unknown model identifiers."""
    arc_id = _create_waiting_coding_change_arc()

    result = handle_request_ai_review({
        "target_arc_id": arc_id,
        "model": "nonexistent_model",
    })

    assert "error" in result


def test_get_ai_reviews_returns_ad_hoc_only():
    """_get_ai_reviews returns only non-template REVIEWER arcs with worker role."""
    from carpenter.api.review import _get_ai_reviews

    arc_id = _create_waiting_coding_change_arc()

    # Create an ad-hoc reviewer (non-template)
    handle_request_ai_review({
        "target_arc_id": arc_id,
        "model": "sonnet",
    })

    # Create a template reviewer (should be excluded)
    db = get_db()
    try:
        db.execute(
            "INSERT INTO arcs (name, goal, parent_id, step_order, depth, "
            "integrity_level, output_type, agent_type, arc_role, from_template) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("verify-correctness", "Template verifier", arc_id, 3, 1,
             "trusted", "python", "REVIEWER", "verifier", True),
        )
        db.commit()
    finally:
        db.close()

    reviews = _get_ai_reviews(arc_id)

    # Should only include the ad-hoc reviewer, not the template one
    assert len(reviews) == 1
    assert reviews[0]["status"] == "pending"
    assert "sonnet" in reviews[0]["model"].lower()
