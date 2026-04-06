"""Tests for the arc tool backend."""
import pytest
from carpenter.tool_backends.arc import (
    handle_create,
    handle_add_child,
    handle_get,
    handle_get_children,
    handle_cancel,
    handle_update_status,
    handle_get_history,
    handle_grant_read_access,
)
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import template_manager


def test_handle_create():
    result = handle_create({"name": "test-arc", "goal": "do stuff"})
    assert "arc_id" in result
    assert isinstance(result["arc_id"], int)


def test_handle_add_child():
    parent = handle_create({"name": "parent-arc"})
    child = handle_add_child({
        "parent_id": parent["arc_id"],
        "name": "child-arc",
        "goal": "child goal",
    })
    assert "arc_id" in child
    assert child["arc_id"] != parent["arc_id"]


def test_handle_get():
    created = handle_create({"name": "fetch-me", "goal": "test goal"})
    result = handle_get({"arc_id": created["arc_id"]})
    assert "arc" in result
    assert result["arc"]["name"] == "fetch-me"
    assert result["arc"]["goal"] == "test goal"
    assert result["arc"]["status"] == "pending"


def test_handle_get_not_found():
    result = handle_get({"arc_id": 99999})
    assert "error" in result


def test_handle_get_children():
    parent = handle_create({"name": "parent"})
    handle_add_child({"parent_id": parent["arc_id"], "name": "child-1"})
    handle_add_child({"parent_id": parent["arc_id"], "name": "child-2"})
    result = handle_get_children({"arc_id": parent["arc_id"]})
    assert len(result["children"]) == 2
    names = [c["name"] for c in result["children"]]
    assert "child-1" in names
    assert "child-2" in names


def test_handle_cancel():
    parent = handle_create({"name": "cancel-parent"})
    handle_add_child({"parent_id": parent["arc_id"], "name": "cancel-child-1"})
    handle_add_child({"parent_id": parent["arc_id"], "name": "cancel-child-2"})
    result = handle_cancel({"arc_id": parent["arc_id"]})
    assert result["cancelled_count"] == 3

    # Verify parent is cancelled
    parent_arc = handle_get({"arc_id": parent["arc_id"]})
    assert parent_arc["arc"]["status"] == "cancelled"


def test_handle_update_status():
    created = handle_create({"name": "status-arc"})
    handle_update_status({"arc_id": created["arc_id"], "status": "active"})
    arc = handle_get({"arc_id": created["arc_id"]})
    assert arc["arc"]["status"] == "active"

    # Verify history was logged
    history = handle_get_history({"arc_id": created["arc_id"]})
    entries = history["history"]
    status_changes = [e for e in entries if e["entry_type"] == "status_changed"]
    assert len(status_changes) == 1


def test_handle_grant_read_access():
    """grant_read_access creates a read grant between two arcs."""
    parent = handle_create({"name": "grant-parent"})
    child1 = handle_add_child({"parent_id": parent["arc_id"], "name": "child-1"})
    child2 = handle_add_child({"parent_id": parent["arc_id"], "name": "child-2"})
    result = handle_grant_read_access({
        "reader_arc_id": child1["arc_id"],
        "target_arc_id": child2["arc_id"],
        "depth": "subtree",
    })
    assert "grant_id" in result
    assert result["reader_arc_id"] == child1["arc_id"]
    assert result["target_arc_id"] == child2["arc_id"]


def test_handle_grant_read_access_invalid_arc():
    """grant_read_access returns error for invalid arc IDs."""
    result = handle_grant_read_access({
        "reader_arc_id": 99998,
        "target_arc_id": 99999,
    })
    assert "error" in result


def test_handle_add_child_rejects_template_arc(tmp_path):
    """handle_add_child should reject template-created arcs."""
    # Create a simple template
    yaml_content = """
name: test-template
description: Test template
steps:
  - name: step-1
    description: First step
    order: 1
"""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(yaml_content)

    # Load template and instantiate
    tid = template_manager.load_template(str(yaml_file))
    parent_id = arc_manager.create_arc("parent-arc", template_id=tid)
    arc_ids = template_manager.instantiate_template(tid, parent_id)

    # Try to add child to template arc via backend - should raise
    with pytest.raises(ValueError, match="Cannot add child to arc .* created by template"):
        handle_add_child({
            "parent_id": arc_ids[0],
            "name": "illegal-child",
            "goal": "This should fail"
        })
