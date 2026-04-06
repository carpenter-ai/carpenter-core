"""Tests for carpenter.core.arc_manager."""

import json
from unittest.mock import patch

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.db import get_db


# ── create_arc ──────────────────────────────────────────────────────

def test_create_arc_root():
    """create_arc returns an integer ID for a root arc with depth 0."""
    arc_id = arc_manager.create_arc("root-arc", goal="do something")
    assert isinstance(arc_id, int)
    assert arc_id > 0

    arc = arc_manager.get_arc(arc_id)
    assert arc["name"] == "root-arc"
    assert arc["goal"] == "do something"
    assert arc["status"] == "pending"
    assert arc["depth"] == 0
    assert arc["parent_id"] is None
    assert arc["updated_at"] is not None


def test_create_arc_nested():
    """create_arc with parent_id sets depth = parent.depth + 1."""
    parent_id = arc_manager.create_arc("parent")
    child_id = arc_manager.create_arc("child", parent_id=parent_id)

    child = arc_manager.get_arc(child_id)
    assert child["depth"] == 1
    assert child["parent_id"] == parent_id


def test_create_arc_logs_history():
    """create_arc inserts a 'created' history entry."""
    arc_id = arc_manager.create_arc("test-arc")
    history = arc_manager.get_history(arc_id)
    assert len(history) == 1
    assert history[0]["entry_type"] == "created"
    assert history[0]["actor"] == "system"
    content = json.loads(history[0]["content_json"])
    assert content["name"] == "test-arc"


# ── add_child ───────────────────────────────────────────────────────

def test_add_child():
    """add_child creates a child arc with auto-incremented step_order."""
    parent_id = arc_manager.create_arc("parent")
    child1 = arc_manager.add_child(parent_id, "step-1")
    child2 = arc_manager.add_child(parent_id, "step-2")

    c1 = arc_manager.get_arc(child1)
    c2 = arc_manager.get_arc(child2)
    assert c1["step_order"] == 0
    assert c2["step_order"] == 1
    assert c1["parent_id"] == parent_id
    assert c2["parent_id"] == parent_id


def test_add_child_frozen_parent_raises():
    """add_child raises ValueError when parent is completed."""
    parent_id = arc_manager.create_arc("parent")
    arc_manager.update_status(parent_id, "active")
    arc_manager.update_status(parent_id, "completed")

    with pytest.raises(ValueError, match="Cannot add child"):
        arc_manager.add_child(parent_id, "late-child")


def test_add_child_nonexistent_parent_raises():
    """add_child raises ValueError when parent does not exist."""
    with pytest.raises(ValueError, match="not found"):
        arc_manager.add_child(99999, "orphan")


# ── get_arc, get_children, get_subtree ──────────────────────────────

def test_get_arc_missing():
    """get_arc returns None for nonexistent arc."""
    assert arc_manager.get_arc(99999) is None


def test_get_children_ordered():
    """get_children returns children ordered by step_order."""
    parent_id = arc_manager.create_arc("parent")
    arc_manager.add_child(parent_id, "alpha")
    arc_manager.add_child(parent_id, "beta")
    arc_manager.add_child(parent_id, "gamma")

    children = arc_manager.get_children(parent_id)
    assert len(children) == 3
    assert [c["name"] for c in children] == ["alpha", "beta", "gamma"]
    assert children[0]["step_order"] < children[1]["step_order"] < children[2]["step_order"]


def test_create_arc_with_wait_until():
    """create_arc stores the wait_until column."""
    future = "2099-01-01T00:00:00"
    arc_id = arc_manager.create_arc("delayed", goal="wait", wait_until=future)
    arc = arc_manager.get_arc(arc_id)
    assert arc["wait_until"] == future


def test_wait_until_blocks_immediate_enqueue():
    """Root arc with future wait_until is NOT immediately enqueued."""
    from carpenter.db import get_db as _get_db

    future = "2099-12-31T23:59:59"
    # Create a code file so the root arc would normally be enqueued immediately
    db = _get_db()
    try:
        db.execute(
            "INSERT INTO code_files (id, file_path, source, review_status) VALUES (?, ?, ?, ?)",
            (9901, "/tmp/wait_test.py", "pass", "approved"),
        )
        db.commit()
    finally:
        db.close()

    arc_id = arc_manager.create_arc(
        "wait-arc", goal="delayed root", code_file_id=9901, wait_until=future,
    )

    # The arc should NOT appear in the work queue
    import json as _json
    db = _get_db()
    try:
        row = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'arc.dispatch' "
            "AND payload_json = ? AND status = 'pending'",
            (_json.dumps({"arc_id": arc_id}),),
        ).fetchone()
    finally:
        db.close()

    assert row is None, "Arc with future wait_until should not be immediately enqueued"


def test_get_subtree():
    """get_subtree returns all descendants in depth/step_order order."""
    root = arc_manager.create_arc("root")
    c1 = arc_manager.add_child(root, "c1")
    c2 = arc_manager.add_child(root, "c2")
    gc1 = arc_manager.add_child(c1, "gc1")

    subtree = arc_manager.get_subtree(root)
    assert len(subtree) == 3
    names = [n["name"] for n in subtree]
    # depth-1 nodes first (c1, c2), then depth-2 (gc1)
    assert names == ["c1", "c2", "gc1"]


# ── update_status ───────────────────────────────────────────────────

def test_update_status_valid_transition():
    """Valid transitions update status and log history."""
    arc_id = arc_manager.create_arc("test")
    arc_manager.update_status(arc_id, "active")

    arc = arc_manager.get_arc(arc_id)
    assert arc["status"] == "active"

    history = arc_manager.get_history(arc_id)
    status_entries = [h for h in history if h["entry_type"] == "status_changed"]
    assert len(status_entries) == 1
    content = json.loads(status_entries[0]["content_json"])
    assert content["old_status"] == "pending"
    assert content["new_status"] == "active"


def test_update_status_invalid_transition_raises():
    """Invalid transitions raise ValueError."""
    arc_id = arc_manager.create_arc("test")
    # pending -> completed is not allowed
    with pytest.raises(ValueError, match="Invalid transition"):
        arc_manager.update_status(arc_id, "completed")


def test_update_status_frozen_raises():
    """No transitions allowed from completed status."""
    arc_id = arc_manager.create_arc("test")
    arc_manager.update_status(arc_id, "active")
    arc_manager.update_status(arc_id, "completed")

    with pytest.raises(ValueError, match="Invalid transition"):
        arc_manager.update_status(arc_id, "active")


# ── cancel_arc ──────────────────────────────────────────────────────

def test_cancel_arc_cascades():
    """cancel_arc cancels self and all pending/active/waiting descendants."""
    root = arc_manager.create_arc("root")
    c1 = arc_manager.add_child(root, "c1")
    c2 = arc_manager.add_child(root, "c2")
    gc1 = arc_manager.add_child(c1, "gc1")

    count = arc_manager.cancel_arc(root)
    assert count == 4  # root + c1 + c2 + gc1

    assert arc_manager.get_arc(root)["status"] == "cancelled"
    assert arc_manager.get_arc(c1)["status"] == "cancelled"
    assert arc_manager.get_arc(c2)["status"] == "cancelled"
    assert arc_manager.get_arc(gc1)["status"] == "cancelled"


def test_cancel_arc_skips_already_completed():
    """cancel_arc does not cancel already-completed children."""
    root = arc_manager.create_arc("root")
    c1 = arc_manager.add_child(root, "c1")
    c2 = arc_manager.add_child(root, "c2")

    # Complete c1
    arc_manager.update_status(c1, "active")
    arc_manager.update_status(c1, "completed")

    count = arc_manager.cancel_arc(root)
    assert count == 2  # root + c2 (c1 already completed)
    assert arc_manager.get_arc(c1)["status"] == "completed"
    assert arc_manager.get_arc(c2)["status"] == "cancelled"


# ── add_history, get_history ────────────────────────────────────────

def test_add_and_get_history():
    """add_history inserts and get_history retrieves entries."""
    arc_id = arc_manager.create_arc("test")
    hid = arc_manager.add_history(arc_id, "note", {"text": "hello"}, actor="user")
    assert isinstance(hid, int)

    history = arc_manager.get_history(arc_id)
    # 1 from create_arc + 1 manual
    assert len(history) == 2
    manual = [h for h in history if h["entry_type"] == "note"]
    assert len(manual) == 1
    assert json.loads(manual[0]["content_json"]) == {"text": "hello"}
    assert manual[0]["actor"] == "user"


# ── check_dependencies ─────────────────────────────────────────────

def test_check_dependencies_first_sibling():
    """First sibling (no preceding) always has dependencies met."""
    parent = arc_manager.create_arc("parent")
    c1 = arc_manager.add_child(parent, "c1")
    assert arc_manager.check_dependencies(c1) is True


def test_check_dependencies_preceding_incomplete():
    """Dependencies not met when preceding sibling is not completed."""
    parent = arc_manager.create_arc("parent")
    c1 = arc_manager.add_child(parent, "c1")
    c2 = arc_manager.add_child(parent, "c2")

    assert arc_manager.check_dependencies(c2) is False


def test_check_dependencies_preceding_complete():
    """Dependencies met when all preceding siblings are completed."""
    parent = arc_manager.create_arc("parent")
    c1 = arc_manager.add_child(parent, "c1")
    c2 = arc_manager.add_child(parent, "c2")

    arc_manager.update_status(c1, "active")
    arc_manager.update_status(c1, "completed")

    assert arc_manager.check_dependencies(c2) is True


# ── dispatch_arc ────────────────────────────────────────────────────

def test_dispatch_arc_no_code():
    """dispatch_arc without code_file_id returns invoke_agent action."""
    arc_id = arc_manager.create_arc("agent-arc")
    result = arc_manager.dispatch_arc(arc_id)

    assert result["action"] == "invoke_agent"
    assert result["arc_id"] == arc_id
    assert arc_manager.get_arc(arc_id)["status"] == "active"


def test_dispatch_arc_with_code():
    """dispatch_arc with code_file_id calls code_manager.execute."""
    # Insert a code_file row to satisfy FK
    db = get_db()
    try:
        db.execute(
            "INSERT INTO code_files (id, file_path, source) VALUES (?, ?, ?)",
            (1, "/tmp/test.py", "test"),
        )
        db.commit()
    finally:
        db.close()

    arc_id = arc_manager.create_arc("code-arc", code_file_id=1)

    mock_result = {"execution_id": 10, "exit_code": 0, "execution_status": "success"}
    with patch("carpenter.core.code_manager.execute", return_value=mock_result):
        result = arc_manager.dispatch_arc(arc_id)

    assert result["action"] == "execute_code"
    assert result["arc_id"] == arc_id
    assert result["result"] == mock_result
    assert arc_manager.get_arc(arc_id)["status"] == "active"


def test_dispatch_arc_frozen_raises():
    """dispatch_arc raises ValueError for completed arc."""
    arc_id = arc_manager.create_arc("done-arc")
    arc_manager.update_status(arc_id, "active")
    arc_manager.update_status(arc_id, "completed")

    with pytest.raises(ValueError, match="Cannot dispatch"):
        arc_manager.dispatch_arc(arc_id)


# ── freeze_arc ──────────────────────────────────────────────────────

def test_freeze_arc_no_children():
    """freeze_arc completes an active arc with no children."""
    arc_id = arc_manager.create_arc("leaf")
    arc_manager.update_status(arc_id, "active")
    arc_manager.freeze_arc(arc_id)

    assert arc_manager.get_arc(arc_id)["status"] == "completed"


def test_freeze_arc_children_pending():
    """freeze_arc sets status to waiting when children are not all completed."""
    parent = arc_manager.create_arc("parent")
    arc_manager.update_status(parent, "active")
    arc_manager.add_child(parent, "child")

    arc_manager.freeze_arc(parent)
    assert arc_manager.get_arc(parent)["status"] == "waiting"


def test_freeze_arc_failed_child_all_frozen():
    """freeze_arc propagates failure when any child failed and all are frozen."""
    parent = arc_manager.create_arc("parent")
    arc_manager.update_status(parent, "active")
    c1 = arc_manager.add_child(parent, "c1")
    c2 = arc_manager.add_child(parent, "c2")

    # Complete c1, fail c2
    arc_manager.update_status(c1, "active")
    arc_manager.update_status(c1, "completed")
    arc_manager.update_status(c2, "active")
    arc_manager.update_status(c2, "failed")

    arc_manager.freeze_arc(parent)
    assert arc_manager.get_arc(parent)["status"] == "failed"


def test_freeze_arc_failed_child_some_pending():
    """freeze_arc stays waiting when a child failed but others still pending."""
    parent = arc_manager.create_arc("parent")
    arc_manager.update_status(parent, "active")
    c1 = arc_manager.add_child(parent, "c1")
    c2 = arc_manager.add_child(parent, "c2")

    # Fail c1, leave c2 pending
    arc_manager.update_status(c1, "active")
    arc_manager.update_status(c1, "failed")

    arc_manager.freeze_arc(parent)
    assert arc_manager.get_arc(parent)["status"] == "waiting"


# ── check_dependencies_detailed ──────────────────────────────────────

def test_check_dependencies_detailed_all_complete():
    """check_dependencies_detailed reports satisfied when all done."""
    parent = arc_manager.create_arc("parent")
    c1 = arc_manager.add_child(parent, "c1")
    c2 = arc_manager.add_child(parent, "c2")
    arc_manager.update_status(c1, "active")
    arc_manager.update_status(c1, "completed")

    result = arc_manager.check_dependencies_detailed(c2)
    assert result["satisfied"] is True
    assert result["blocked_by_pending"] == []
    assert result["blocked_by_failed"] == []


def test_check_dependencies_detailed_failed_predecessor():
    """check_dependencies_detailed reports failed predecessors."""
    parent = arc_manager.create_arc("parent")
    c1 = arc_manager.add_child(parent, "c1", goal="first step")
    c2 = arc_manager.add_child(parent, "c2")
    arc_manager.update_status(c1, "active")
    arc_manager.update_status(c1, "failed")

    result = arc_manager.check_dependencies_detailed(c2)
    assert result["satisfied"] is False
    assert c1 in result["blocked_by_failed"]
    assert len(result["failed_predecessors"]) == 1
    assert result["failed_predecessors"][0]["name"] == "c1"


# ── is_frozen ───────────────────────────────────────────────────────

def test_is_frozen():
    """is_frozen returns True for completed/failed/cancelled, False for others."""
    arc_id = arc_manager.create_arc("test")
    assert arc_manager.is_frozen(arc_id) is False

    arc_manager.update_status(arc_id, "active")
    assert arc_manager.is_frozen(arc_id) is False

    arc_manager.update_status(arc_id, "completed")
    assert arc_manager.is_frozen(arc_id) is True


def test_is_frozen_nonexistent():
    """is_frozen returns False for nonexistent arc."""
    assert arc_manager.is_frozen(99999) is False


# ── check_activation ────────────────────────────────────────────────

def test_check_activation_no_activations():
    """check_activation returns True when no activations registered."""
    arc_id = arc_manager.create_arc("test")
    assert arc_manager.check_activation(arc_id) is True


def test_check_activation_with_matching_event():
    """check_activation returns True when a matching processed event exists."""
    arc_id = arc_manager.create_arc("test")

    db = get_db()
    try:
        db.execute(
            "INSERT INTO arc_activations (arc_id, event_type) VALUES (?, ?)",
            (arc_id, "webhook.received"),
        )
        db.execute(
            "INSERT INTO events (event_type, payload_json, processed) "
            "VALUES (?, ?, ?)",
            ("webhook.received", '{}', True),
        )
        db.commit()
    finally:
        db.close()

    assert arc_manager.check_activation(arc_id) is True


def test_check_activation_no_matching_event():
    """check_activation returns False when no matching processed event exists."""
    arc_id = arc_manager.create_arc("test")

    db = get_db()
    try:
        db.execute(
            "INSERT INTO arc_activations (arc_id, event_type) VALUES (?, ?)",
            (arc_id, "webhook.received"),
        )
        db.commit()
    finally:
        db.close()

    assert arc_manager.check_activation(arc_id) is False


# ── agent_config helpers ──────────────────────────────────────────


def test_get_or_create_agent_config_basic():
    """get_or_create_agent_config creates a row and returns an int ID."""
    config_id = arc_manager.get_or_create_agent_config(
        model="anthropic:claude-sonnet-4-20250514",
    )
    assert isinstance(config_id, int)
    assert config_id > 0


def test_get_or_create_agent_config_dedup():
    """Same parameters return the same config ID (dedup via unique index)."""
    id1 = arc_manager.get_or_create_agent_config(
        model="anthropic:claude-sonnet-4-20250514",
        agent_role="security-reviewer",
        temperature=0.2,
    )
    id2 = arc_manager.get_or_create_agent_config(
        model="anthropic:claude-sonnet-4-20250514",
        agent_role="security-reviewer",
        temperature=0.2,
    )
    assert id1 == id2


def test_get_or_create_agent_config_different_params():
    """Different parameters produce different config IDs."""
    id1 = arc_manager.get_or_create_agent_config(
        model="anthropic:claude-sonnet-4-20250514",
    )
    id2 = arc_manager.get_or_create_agent_config(
        model="anthropic:claude-haiku-4-5-20251001",
    )
    assert id1 != id2


def test_get_or_create_agent_config_null_fields():
    """NULL fields are handled correctly in dedup."""
    id1 = arc_manager.get_or_create_agent_config(
        model="anthropic:claude-sonnet-4-20250514",
        agent_role=None,
        temperature=None,
        max_tokens=None,
    )
    id2 = arc_manager.get_or_create_agent_config(
        model="anthropic:claude-sonnet-4-20250514",
    )
    assert id1 == id2


def test_get_agent_config_returns_row():
    """get_agent_config returns a dict with all fields."""
    config_id = arc_manager.get_or_create_agent_config(
        model="anthropic:claude-sonnet-4-20250514",
        agent_role="security-reviewer",
        temperature=0.2,
        max_tokens=4096,
    )
    cfg = arc_manager.get_agent_config(config_id)
    assert cfg is not None
    assert cfg["model"] == "anthropic:claude-sonnet-4-20250514"
    assert cfg["agent_role"] == "security-reviewer"
    assert cfg["temperature"] == pytest.approx(0.2)
    assert cfg["max_tokens"] == 4096


def test_get_agent_config_returns_none():
    """get_agent_config returns None for missing ID."""
    assert arc_manager.get_agent_config(99999) is None
