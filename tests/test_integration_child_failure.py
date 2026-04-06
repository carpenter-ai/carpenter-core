"""End-to-end test: child fails → parent re-invoked → agent creates alternative."""

import json
from unittest.mock import patch

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.arcs.child_failure_handler import handle_child_failed
from carpenter.db import get_db


@pytest.mark.asyncio
async def test_child_fails_parent_reinvoked_creates_alternative():
    """Full flow: child fails → handler re-invokes parent → agent creates new child."""
    # Setup: parent with a child
    parent = arc_manager.create_arc("parent", goal="Build feature X")
    arc_manager.update_status(parent, "active")
    child = arc_manager.add_child(parent, "attempt-1", goal="First approach")
    arc_manager.update_status(parent, "waiting")
    arc_manager.update_status(child, "active")
    arc_manager.update_status(child, "failed")

    # Verify work item was enqueued
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'arc.child_failed'"
        ).fetchone()
    finally:
        db.close()
    assert row is not None

    payload = json.loads(row["payload_json"])

    # Mock invoke_for_chat to simulate agent creating an alternative child
    def mock_invoke(message, **kwargs):
        # Agent creates a new child arc as alternative
        arc_manager.add_child(parent, "attempt-2", goal="Alternative approach")
        return {"response_text": "Created alternative child arc."}

    with patch("carpenter.agent.invocation.invoke_for_chat", side_effect=mock_invoke):
        await handle_child_failed(work_id=row["id"], payload=payload)

    # Verify: parent is active (re-invoked)
    parent_arc = arc_manager.get_arc(parent)
    assert parent_arc["status"] == "active"

    # Verify: new child was created
    children = arc_manager.get_children(parent)
    assert len(children) == 2
    assert children[0]["name"] == "attempt-1"
    assert children[0]["status"] == "failed"
    assert children[1]["name"] == "attempt-2"
    assert children[1]["status"] == "pending"
