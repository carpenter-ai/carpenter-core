"""Tests for the fetch_web_content chat tool and arc.create_batch linking."""

import json
from unittest.mock import patch, MagicMock

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.agent import conversation
from carpenter.agent.invocation import _handle_fetch_web_content
from carpenter.db import get_db


def test_fetch_web_content_creates_parent_and_children():
    """fetch_web_content creates a parent arc with 3 children."""
    conv_id = conversation.get_or_create_conversation()

    result = _handle_fetch_web_content(
        {"url": "https://example.com", "goal": "summarize the page"},
        conversation_id=conv_id,
    )

    assert "Web fetch started" in result
    assert "arc #" in result

    # Extract parent arc ID from result
    import re
    match = re.search(r"arc #(\d+)", result)
    assert match
    parent_id = int(match.group(1))

    # Verify parent arc
    parent = arc_manager.get_arc(parent_id)
    assert parent is not None
    assert parent["agent_type"] == "PLANNER"
    assert parent["status"] in ("active", "waiting")

    # Verify children
    children = arc_manager.get_children(parent_id)
    assert len(children) == 3

    # Check child types and ordering
    children_sorted = sorted(children, key=lambda c: c["step_order"])
    assert children_sorted[0]["agent_type"] == "EXECUTOR"
    assert children_sorted[0]["integrity_level"] == "untrusted"
    assert children_sorted[1]["agent_type"] == "REVIEWER"
    assert children_sorted[2]["agent_type"] == "JUDGE"

    # Verify parent linked to conversation
    conv_arc_ids = conversation.get_conversation_arc_ids(conv_id)
    assert parent_id in conv_arc_ids


def test_fetch_web_content_links_children_to_conversation():
    """All children are linked to the originating conversation."""
    conv_id = conversation.get_or_create_conversation()

    result = _handle_fetch_web_content(
        {"url": "https://example.com/test", "goal": "get info"},
        conversation_id=conv_id,
    )

    import re
    match = re.search(r"arc #(\d+)", result)
    parent_id = int(match.group(1))

    children = arc_manager.get_children(parent_id)
    conv_arc_ids = conversation.get_conversation_arc_ids(conv_id)

    for child in children:
        assert child["id"] in conv_arc_ids


def test_fetch_web_content_enqueues_first_child():
    """The EXECUTOR child is enqueued for dispatch."""
    conv_id = conversation.get_or_create_conversation()

    result = _handle_fetch_web_content(
        {"url": "https://example.com", "goal": "fetch it"},
        conversation_id=conv_id,
    )

    import re
    match = re.search(r"arc #(\d+)", result)
    parent_id = int(match.group(1))

    children = arc_manager.get_children(parent_id)
    executor = [c for c in children if c["agent_type"] == "EXECUTOR"][0]

    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'arc.dispatch' "
            "AND payload_json LIKE ?",
            (f'%{executor["id"]}%',),
        ).fetchone()
    finally:
        db.close()

    assert row is not None


def test_fetch_web_content_requires_url():
    """Missing URL returns an error."""
    result = _handle_fetch_web_content(
        {"url": "", "goal": "something"},
        conversation_id=None,
    )
    assert "Error" in result


def test_fetch_web_content_requires_goal():
    """Missing goal returns an error."""
    result = _handle_fetch_web_content(
        {"url": "https://example.com", "goal": ""},
        conversation_id=None,
    )
    assert "Error" in result


def test_fetch_web_content_no_conversation():
    """Works even without a conversation_id (arcs still created)."""
    result = _handle_fetch_web_content(
        {"url": "https://example.com", "goal": "test without conv"},
        conversation_id=None,
    )
    assert "Web fetch started" in result


def test_fetch_web_content_goal_in_child_arcs():
    """The user's goal is included in reviewer arc goals; executor has pre-verified script."""
    conv_id = conversation.get_or_create_conversation()
    goal_text = "find the current temperature"

    result = _handle_fetch_web_content(
        {"url": "https://weather.example.com", "goal": goal_text},
        conversation_id=conv_id,
    )

    import re
    match = re.search(r"arc #(\d+)", result)
    parent_id = int(match.group(1))

    children = arc_manager.get_children(parent_id)
    executor = [c for c in children if c["agent_type"] == "EXECUTOR"][0]
    reviewer = [c for c in children if c["agent_type"] == "REVIEWER"][0]

    # Executor goal contains the pre-verified fetch script
    assert "fetch_url" in executor["goal"]
    assert "web.fetch_webpage" in executor["goal"]
    # PR3: executor goal also references the raw resource output path/id
    assert "raw_resource_path" in executor["goal"]
    assert "raw_resource_id" in executor["goal"]
    # Reviewer goal includes the user's goal
    assert goal_text in reviewer["goal"]


def test_fetch_web_content_sets_url_in_arc_state():
    """The URL is pre-set in the EXECUTOR arc's state."""
    from carpenter.core.workflows._arc_state import get_arc_state

    conv_id = conversation.get_or_create_conversation()
    test_url = "https://weather.example.com/test"

    result = _handle_fetch_web_content(
        {"url": test_url, "goal": "get weather"},
        conversation_id=conv_id,
    )

    import re
    match = re.search(r"arc #(\d+)", result)
    parent_id = int(match.group(1))

    children = arc_manager.get_children(parent_id)
    executor = [c for c in children if c["agent_type"] == "EXECUTOR"][0]

    stored_url = get_arc_state(executor["id"], "fetch_url")
    assert stored_url == test_url


# -- _FETCH_SCRIPT sandbox test --


def test_fetch_script_runs_in_restricted_sandbox():
    """_FETCH_SCRIPT compiles and dispatches correctly in RestrictedPython.

    PR3: the script now reads URL + raw_resource_path + raw_resource_id
    from arc state, fetches the URL, writes the HTML to disk via
    files.write, and calls resource.finalize to populate byte_size /
    content_hash on the raw Resource row.
    """
    from carpenter.agent.invocation import _FETCH_SCRIPT
    from carpenter.executor.restricted import RestrictedExecutor

    calls = []

    def handler(tool_name, params):
        calls.append((tool_name, dict(params)))
        if tool_name == "state.get":
            key = params.get("key")
            if key == "fetch_url":
                return {"value": "https://example.com/weather"}
            if key == "raw_resource_path":
                return {"value": "/tmp/raw.html"}
            if key == "raw_resource_id":
                return {"value": 42}
            return {"value": None}
        if tool_name == "web.fetch_webpage":
            return {"content": "<html>sunny</html>", "status_code": 200}
        if tool_name == "files.write":
            return {"success": True}
        if tool_name == "resource.finalize":
            return {"ok": True, "byte_size": 17, "content_hash": "abc"}
        return {"ok": True}

    executor = RestrictedExecutor(tool_handler=handler)
    result = executor.execute(_FETCH_SCRIPT)

    assert result.exit_code == 0, f"Script failed: {result.error}"
    assert result.error == ""

    # 3 state.get + 1 web.fetch_webpage + 1 files.write + 1 resource.finalize
    assert len(calls) == 6, calls
    assert calls[0] == ("state.get", {"key": "fetch_url"})
    assert calls[1] == ("state.get", {"key": "raw_resource_path"})
    assert calls[2] == ("state.get", {"key": "raw_resource_id"})
    assert calls[3] == ("web.fetch_webpage", {"url": "https://example.com/weather"})
    assert calls[4] == ("files.write", {"path": "/tmp/raw.html", "content": "<html>sunny</html>"})
    assert calls[5] == ("resource.finalize", {"resource_id": 42})


# -- dispatch_bridge.py linking test --


def test_dispatch_bridge_has_batch_linking():
    """dispatch_bridge.py includes arc.create_batch in conversation linking."""
    import inspect
    from carpenter.executor import dispatch_bridge

    source = inspect.getsource(dispatch_bridge.validate_and_dispatch)
    assert "arc.create_batch" in source, (
        "dispatch_bridge.validate_and_dispatch should link arc.create_batch "
        "results to conversations"
    )
