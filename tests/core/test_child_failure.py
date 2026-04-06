"""Tests for child failure notification and parent re-invocation."""

import json
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import work_queue
from carpenter.core.arcs.child_failure_handler import handle_child_failed
from carpenter.db import get_db


def test_child_failure_enqueues_work_item():
    """When a child fails, a work item is enqueued for the parent."""
    parent = arc_manager.create_arc("parent")
    arc_manager.update_status(parent, "active")
    child = arc_manager.add_child(parent, "child", goal="do stuff")

    # Parent goes to waiting, child goes active then fails
    arc_manager.update_status(parent, "waiting")
    arc_manager.update_status(child, "active")

    # Failing the child should enqueue arc.child_failed
    arc_manager.update_status(child, "failed")

    # Check work queue
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'arc.child_failed'"
        ).fetchone()
    finally:
        db.close()

    assert row is not None
    payload = json.loads(row["payload_json"])
    assert payload["parent_id"] == parent
    assert payload["failed_child_id"] == child


def test_child_failure_no_notification_for_template_parent():
    """Template-managed parents are not notified of child failures."""
    parent = arc_manager.create_arc("template-parent", from_template=True)
    arc_manager.update_status(parent, "active")

    # Add child directly since template parents normally block add_child
    # We need to create the child without template check
    db = get_db()
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        cursor = db.execute(
            "INSERT INTO arcs (name, goal, parent_id, step_order, depth, "
            "integrity_level, output_type, agent_type, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("child", "do stuff", parent, 0, 1, "trusted", "python", "EXECUTOR", now),
        )
        child = cursor.lastrowid
        db.execute(
            "INSERT INTO arc_history (arc_id, entry_type, content_json, actor) "
            "VALUES (?, ?, ?, ?)",
            (child, "created", json.dumps({"name": "child"}), "system"),
        )
        db.commit()
    finally:
        db.close()

    arc_manager.update_status(parent, "waiting")
    arc_manager.update_status(child, "active")
    arc_manager.update_status(child, "failed")

    # No work item should be enqueued
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'arc.child_failed'"
        ).fetchone()
    finally:
        db.close()

    assert row is None


def test_child_failure_no_notification_when_parent_not_waiting():
    """No notification when parent is not in waiting status."""
    parent = arc_manager.create_arc("parent")
    arc_manager.update_status(parent, "active")
    child = arc_manager.add_child(parent, "child")
    arc_manager.update_status(child, "active")

    # Parent is still active (not waiting)
    arc_manager.update_status(child, "failed")

    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'arc.child_failed'"
        ).fetchone()
    finally:
        db.close()

    assert row is None


def test_child_failure_root_arc_no_parent_notification():
    """Root arc failure does not enqueue parent notification."""
    root = arc_manager.create_arc("root")
    arc_manager.update_status(root, "active")
    arc_manager.update_status(root, "failed")

    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'arc.child_failed'"
        ).fetchone()
    finally:
        db.close()

    assert row is None


def test_escalation_policy_fail():
    """When escalation policy is 'fail', no re-invocation happens."""
    parent = arc_manager.create_arc("parent")
    arc_manager.update_status(parent, "active")

    # Set escalation policy to "fail"
    db = get_db()
    try:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
            (parent, "_escalation_policy", json.dumps("fail")),
        )
        db.commit()
    finally:
        db.close()

    child = arc_manager.add_child(parent, "child")
    arc_manager.update_status(parent, "waiting")
    arc_manager.update_status(child, "active")
    arc_manager.update_status(child, "failed")

    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'arc.child_failed'"
        ).fetchone()
    finally:
        db.close()

    assert row is None


@pytest.mark.asyncio
async def test_handle_child_failed_re_invokes_parent():
    """Handler transitions parent to active and invokes chat agent."""
    parent = arc_manager.create_arc("parent", goal="build something")
    arc_manager.update_status(parent, "active")
    child = arc_manager.add_child(parent, "child", goal="first try")
    arc_manager.update_status(parent, "waiting")
    arc_manager.update_status(child, "active")
    arc_manager.update_status(child, "failed")

    payload = {
        "parent_id": parent,
        "failed_child_id": child,
        "failed_child_name": "child",
        "failed_child_goal": "first try",
    }

    with patch("carpenter.agent.invocation.invoke_for_chat") as mock_invoke:
        mock_invoke.return_value = {"response_text": "I'll create an alternative."}
        await handle_child_failed(work_id=1, payload=payload)

    # Parent should be active (transitioned from waiting)
    assert arc_manager.get_arc(parent)["status"] == "active"

    # invoke_for_chat should have been called
    mock_invoke.assert_called_once()
    call_kwargs = mock_invoke.call_args
    assert "_system_triggered" in call_kwargs.kwargs or (
        len(call_kwargs.args) > 0
    )


def test_root_arc_failure_with_escalation_stack(monkeypatch):
    """Root arc failure with an escalation stack creates an escalated sibling."""
    import carpenter.config
    monkeypatch.setitem(
        carpenter.config.CONFIG, "escalation", {
            "stacks": {
                "default": ["model-small", "model-medium", "model-large"],
            },
        },
    )
    carpenter.config.CONFIG.setdefault("model_roles", {})["default"] = "model-small"

    root = arc_manager.create_arc("root-arc", goal="do something important")
    arc_manager.update_status(root, "active")
    arc_manager.update_status(root, "failed")

    # Should have created an escalated arc
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM arcs WHERE name LIKE '%escalated%'"
        ).fetchone()
    finally:
        db.close()

    assert row is not None
    assert row["goal"] == "do something important"

    # Check agent_config_id points to the escalated model
    assert row["agent_config_id"] is not None
    cfg = arc_manager.get_agent_config(row["agent_config_id"])
    assert cfg["model"] == "model-medium"

    # Check _escalated_from in arc_state
    db = get_db()
    try:
        escalated_row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = '_escalated_from'",
            (row["id"],),
        ).fetchone()
    finally:
        db.close()

    assert json.loads(escalated_row["value_json"]) == root

    # Check original arc is marked as escalated
    original = arc_manager.get_arc(root)
    assert original["status"] == "escalated"


def test_root_arc_failure_at_top_of_stack_notifies_human(monkeypatch):
    """Root arc at top of escalation stack notifies human."""
    import carpenter.config
    monkeypatch.setitem(
        carpenter.config.CONFIG, "escalation", {
            "stacks": {
                "default": ["model-small", "model-large"],
            },
        },
    )
    carpenter.config.CONFIG.setdefault("model_roles", {})["default"] = "model-large"

    root = arc_manager.create_arc("root-arc", goal="important task")
    arc_manager.update_status(root, "active")

    with patch("carpenter.core.notifications.notify") as mock_notify:
        arc_manager.update_status(root, "failed")
        mock_notify.assert_called_once()
        assert "top of escalation" in mock_notify.call_args[0][0]
