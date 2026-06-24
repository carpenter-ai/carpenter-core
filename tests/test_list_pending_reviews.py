"""Tests for the ``list_pending_reviews`` chat tool."""

import json

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.workflows._arc_state import set_arc_state

# Load the chat tool module directly (it lives in config_seed/chat_tools/
# and is normally imported by the chat tool loader at platform start).
import importlib.util
from pathlib import Path

_PLATFORM_TOOLS_PATH = (
    Path(__file__).parent.parent / "config_seed" / "chat_tools" / "platform_tools.py"
)
_spec = importlib.util.spec_from_file_location(
    "_test_platform_tools", str(_PLATFORM_TOOLS_PATH)
)
_platform_tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_platform_tools)
list_pending_reviews = _platform_tools.list_pending_reviews


def _mk_gated_child(parent_id: int, url: str = "/api/review/abc-123") -> int:
    child_id = arc_manager.add_child(parent_id, "reflection-action-0")
    set_arc_state(child_id, "_review_mode", "human")
    set_arc_state(child_id, "review_url", url)
    return child_id


def test_no_pending_reviews_returns_empty():
    result = json.loads(list_pending_reviews({}))
    assert result == {"pending_reviews": []}


def test_one_pending_review_returned_with_url():
    parent_id = arc_manager.create_arc("reflection-root")
    arc_manager.update_status(parent_id, "active")
    child_id = _mk_gated_child(parent_id, "/api/review/abc-123")

    result = json.loads(list_pending_reviews({}))
    entries = result["pending_reviews"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["arc_id"] == child_id
    assert entry["name"] == "reflection-action-0"
    assert entry["parent_arc_id"] == parent_id
    assert entry["url"] == "/api/review/abc-123"
    assert entry["status"] not in arc_manager.FROZEN_STATUSES
    assert entry["description"]  # non-empty


def test_terminal_arc_is_excluded():
    parent_id = arc_manager.create_arc("reflection-root")
    arc_manager.update_status(parent_id, "active")

    pending_child = _mk_gated_child(parent_id, "/api/review/pending-xyz")

    other_child = _mk_gated_child(parent_id, "/api/review/done-xyz")
    arc_manager.update_status(other_child, "active")
    arc_manager.update_status(other_child, "cancelled")

    result = json.loads(list_pending_reviews({}))
    entries = result["pending_reviews"]
    assert len(entries) == 1
    assert entries[0]["arc_id"] == pending_child
    assert entries[0]["url"] == "/api/review/pending-xyz"


def test_absolute_url_when_public_base_url_set():
    from carpenter import config as cfg

    parent_id = arc_manager.create_arc("reflection-root")
    arc_manager.update_status(parent_id, "active")
    _mk_gated_child(parent_id, "/api/review/xyz-789")

    saved = cfg.CONFIG.get("public_base_url", "")
    cfg.CONFIG["public_base_url"] = "https://example.test"
    try:
        result = json.loads(list_pending_reviews({}))
    finally:
        cfg.CONFIG["public_base_url"] = saved

    entries = result["pending_reviews"]
    assert len(entries) == 1
    assert entries[0]["url"] == "https://example.test/api/review/xyz-789"


def test_arc_without_review_mode_human_excluded():
    """An arc with review_url but no _review_mode='human' (e.g. auto-review)
    should NOT be surfaced — only human-approval reviews are actionable."""
    parent_id = arc_manager.create_arc("reflection-root")
    arc_manager.update_status(parent_id, "active")

    child_id = arc_manager.add_child(parent_id, "auto-action")
    set_arc_state(child_id, "review_url", "/api/review/auto-only")
    # no _review_mode key

    result = json.loads(list_pending_reviews({}))
    assert result["pending_reviews"] == []
