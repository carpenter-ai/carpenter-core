"""Tests for the read_arc_result chat tool."""

import json
import sys
from pathlib import Path

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.workflows._arc_state import set_arc_state, get_arc_state
from carpenter.db import get_db


def _load_tool():
    """Import and return the read_arc_result handler from config_seed."""
    config_seed = Path(__file__).parent.parent / "config_seed" / "chat_tools"
    # Import the module directly to get the handler
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_test_arcs_tools", str(config_seed / "arcs.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.read_arc_result


@pytest.fixture
def read_arc_result():
    return _load_tool()


# -- Basic functionality --


def test_read_arc_result_returns_full_content(read_arc_result):
    """read_arc_result returns the full _agent_response for a completed arc."""
    arc_id = arc_manager.create_arc("test-read")
    arc_manager.update_status(arc_id, "active")
    full_response = "A" * 10000
    set_arc_state(arc_id, "_agent_response", full_response)
    arc_manager.update_status(arc_id, "completed")

    result = read_arc_result({"arc_id": arc_id})
    assert result == full_response


def test_read_arc_result_short_content(read_arc_result):
    """Short content is returned without pagination metadata."""
    arc_id = arc_manager.create_arc("test-short")
    arc_manager.update_status(arc_id, "active")
    set_arc_state(arc_id, "_agent_response", "Hello world")
    arc_manager.update_status(arc_id, "completed")

    result = read_arc_result({"arc_id": arc_id})
    assert result == "Hello world"
    assert "[Showing" not in result


def test_read_arc_result_not_found(read_arc_result):
    """Returns error for nonexistent arc."""
    result = read_arc_result({"arc_id": 99999})
    assert "not found" in result


def test_read_arc_result_rejects_non_completed(read_arc_result):
    """Returns error for arcs that are not in completed status."""
    arc_id = arc_manager.create_arc("test-active")
    arc_manager.update_status(arc_id, "active")
    set_arc_state(arc_id, "_agent_response", "partial result")

    result = read_arc_result({"arc_id": arc_id})
    assert "active" in result
    assert "only works for completed" in result


def test_read_arc_result_rejects_failed(read_arc_result):
    """Returns error for failed arcs."""
    arc_id = arc_manager.create_arc("test-failed")
    arc_manager.update_status(arc_id, "active")
    set_arc_state(arc_id, "_agent_response", "something")
    arc_manager.update_status(arc_id, "failed")

    result = read_arc_result({"arc_id": arc_id})
    assert "failed" in result
    assert "only works for completed" in result


def test_read_arc_result_no_content(read_arc_result):
    """Returns informative message when arc has no result."""
    arc_id = arc_manager.create_arc("test-empty")
    arc_manager.update_status(arc_id, "active")
    arc_manager.update_status(arc_id, "completed")

    result = read_arc_result({"arc_id": arc_id})
    assert "no result content" in result


# -- Child arc fallback --


def test_read_arc_result_reads_child_response(read_arc_result):
    """Falls back to child arc _agent_response when root has none."""
    parent_id = arc_manager.create_arc("test-parent")
    arc_manager.update_status(parent_id, "active")

    child_id = arc_manager.add_child(parent_id, "child-executor", goal="do work")
    arc_manager.update_status(child_id, "active")
    set_arc_state(child_id, "_agent_response", "child result data")
    arc_manager.update_status(child_id, "completed")
    arc_manager.update_status(parent_id, "completed")

    result = read_arc_result({"arc_id": parent_id})
    assert "child result data" in result


def test_read_arc_result_prefers_later_child(read_arc_result):
    """Prefers REVIEWER/JUDGE child (later step_order) over EXECUTOR."""
    parent_id = arc_manager.create_arc("test-parent-multi")
    arc_manager.update_status(parent_id, "active")

    exec_child = arc_manager.add_child(parent_id, "executor", goal="execute")
    arc_manager.update_status(exec_child, "active")
    set_arc_state(exec_child, "_agent_response", "raw executor output")
    arc_manager.update_status(exec_child, "completed")

    review_child = arc_manager.add_child(parent_id, "reviewer", goal="review")
    arc_manager.update_status(review_child, "active")
    set_arc_state(review_child, "_agent_response", "refined reviewer summary")
    arc_manager.update_status(review_child, "completed")

    arc_manager.update_status(parent_id, "completed")

    result = read_arc_result({"arc_id": parent_id})
    assert "refined reviewer summary" in result


# -- Pagination --


def test_read_arc_result_offset_and_limit(read_arc_result):
    """Offset and limit parameters paginate through large results."""
    arc_id = arc_manager.create_arc("test-paginate")
    arc_manager.update_status(arc_id, "active")
    # Create content with identifiable sections
    full_response = "AAAAABBBBBCCCCC"
    set_arc_state(arc_id, "_agent_response", full_response)
    arc_manager.update_status(arc_id, "completed")

    # Read first 5 chars
    result = read_arc_result({"arc_id": arc_id, "offset": 0, "limit": 5})
    assert "AAAAA" in result
    assert "[Showing" in result
    assert "10 remaining" in result

    # Read middle 5 chars
    result = read_arc_result({"arc_id": arc_id, "offset": 5, "limit": 5})
    assert "BBBBB" in result

    # Read last 5 chars
    result = read_arc_result({"arc_id": arc_id, "offset": 10, "limit": 5})
    assert "CCCCC" in result
    assert "0 remaining" not in result  # No remaining when at end


def test_read_arc_result_default_limit_returns_all_moderate_content(read_arc_result):
    """Default limit of 50000 chars returns all content for moderate-sized results."""
    arc_id = arc_manager.create_arc("test-moderate")
    arc_manager.update_status(arc_id, "active")
    moderate_response = "x" * 10000
    set_arc_state(arc_id, "_agent_response", moderate_response)
    arc_manager.update_status(arc_id, "completed")

    result = read_arc_result({"arc_id": arc_id})
    assert len(result) == 10000
    assert "[Showing" not in result
