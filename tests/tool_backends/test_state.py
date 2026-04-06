"""Tests for the per-arc state tool backend."""
from carpenter.tool_backends.state import (
    handle_get,
    handle_set,
    handle_delete,
    handle_list,
)


def test_set_and_get(create_bare_arc):
    arc_id = create_bare_arc()
    handle_set({"arc_id": arc_id, "key": "color", "value": "blue"})
    result = handle_get({"arc_id": arc_id, "key": "color"})
    assert result["value"] == "blue"


def test_get_default(create_bare_arc):
    arc_id = create_bare_arc()
    result = handle_get({"arc_id": arc_id, "key": "missing", "default": 42})
    assert result["value"] == 42


def test_delete(create_bare_arc):
    arc_id = create_bare_arc()
    handle_set({"arc_id": arc_id, "key": "temp", "value": "exists"})
    result = handle_get({"arc_id": arc_id, "key": "temp"})
    assert result["value"] == "exists"

    handle_delete({"arc_id": arc_id, "key": "temp"})
    result = handle_get({"arc_id": arc_id, "key": "temp", "default": None})
    assert result["value"] is None


def test_list_keys(create_bare_arc):
    arc_id = create_bare_arc()
    handle_set({"arc_id": arc_id, "key": "a", "value": 1})
    handle_set({"arc_id": arc_id, "key": "b", "value": 2})
    handle_set({"arc_id": arc_id, "key": "c", "value": 3})
    result = handle_list({"arc_id": arc_id})
    assert sorted(result["keys"]) == ["a", "b", "c"]


def test_set_overwrite(create_bare_arc):
    arc_id = create_bare_arc()
    handle_set({"arc_id": arc_id, "key": "counter", "value": 1})
    handle_set({"arc_id": arc_id, "key": "counter", "value": 99})
    result = handle_get({"arc_id": arc_id, "key": "counter"})
    assert result["value"] == 99
