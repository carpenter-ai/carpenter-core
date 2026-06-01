"""Tests for Resource-aware preview in ``arc_notify_handler`` (PR4).

When a completing arc has ``_primary_resource_id`` set in its state,
the chat notification should build its preview from the Resource
content (if trusted) instead of ``_agent_response``.
"""

from unittest.mock import AsyncMock, patch

import pytest

from carpenter.agent import conversation
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager
from carpenter.core.workflows._arc_state import set_arc_state
from carpenter.core.workflows.arc_notify_handler import handle_arc_chat_notify
from carpenter.db import get_db


def _link_conv(conv_id, arc_id):
    db = get_db()
    try:
        db.execute(
            "INSERT INTO conversation_arcs (conversation_id, arc_id) "
            "VALUES (?, ?)",
            (conv_id, arc_id),
        )
        db.commit()
    finally:
        db.close()


def _trusted_resource(arc_id, tmp_path, body) -> int:
    fp = tmp_path / f"pr-{arc_id}.txt"
    fp.write_text(body, encoding="utf-8")
    rid = res_manager.derive_resource(
        content_type="text-summary",
        file_path=str(fp),
        produced_by_arc_id=arc_id,
        produced_by_template="html_to_summary",
        template_verdict="approved",
        byte_size=len(body.encode("utf-8")),
    )
    return rid


def _pending_resource(arc_id, tmp_path, body) -> int:
    fp = tmp_path / f"pend-{arc_id}.txt"
    fp.write_text(body, encoding="utf-8")
    rid = res_manager.derive_resource(
        content_type="text-summary",
        file_path=str(fp),
        produced_by_arc_id=arc_id,
        produced_by_template="html_to_summary",
        template_verdict="pending",
        byte_size=len(body.encode("utf-8")),
    )
    return rid


@pytest.mark.asyncio
async def test_trusted_primary_resource_is_used_for_preview(tmp_path):
    arc_id = arc_manager.create_arc("web-lookup", integrity_level="trusted")
    arc_manager.update_status(arc_id, "active")
    rid = _trusted_resource(
        arc_id, tmp_path, "Oxford weather: 15C and sunny"
    )
    # Sanity: include _agent_response so the test proves preference.
    set_arc_state(arc_id, "_agent_response", "agent fallback response")
    set_arc_state(arc_id, "_primary_resource_id", rid)
    arc_manager.update_status(arc_id, "completed")

    conv_id = conversation.get_or_create_conversation()
    _link_conv(conv_id, arc_id)

    mock_run = AsyncMock()
    with patch(
        "carpenter.core.workflows.arc_notify_handler.thread_pools."
        "run_in_work_pool",
        mock_run,
    ):
        await handle_arc_chat_notify(1, {"arc_id": arc_id})

    msgs = conversation.get_messages(conv_id)
    system_msgs = [m for m in msgs if m["role"] == "system"]
    assert len(system_msgs) == 1
    content = system_msgs[0]["content"]

    # Preview came from Resource, not _agent_response.
    assert "Oxford weather: 15C and sunny" in content
    assert "agent fallback response" not in content
    # Suffix mentions resource id and primary-resource framing.
    assert f"Primary resource: #{rid}" in content
    assert f"read_resource({rid})" in content
    assert "trusted" in content


@pytest.mark.asyncio
async def test_pending_primary_resource_falls_back_with_note(tmp_path):
    arc_id = arc_manager.create_arc("web-lookup", integrity_level="trusted")
    arc_manager.update_status(arc_id, "active")
    rid = _pending_resource(arc_id, tmp_path, "not-yet-approved")
    set_arc_state(arc_id, "_agent_response", "summary from agent")
    set_arc_state(arc_id, "_primary_resource_id", rid)
    arc_manager.update_status(arc_id, "completed")

    conv_id = conversation.get_or_create_conversation()
    _link_conv(conv_id, arc_id)

    mock_run = AsyncMock()
    with patch(
        "carpenter.core.workflows.arc_notify_handler.thread_pools."
        "run_in_work_pool",
        mock_run,
    ):
        await handle_arc_chat_notify(1, {"arc_id": arc_id})

    msgs = conversation.get_messages(conv_id)
    system_msgs = [m for m in msgs if m["role"] == "system"]
    assert len(system_msgs) == 1
    content = system_msgs[0]["content"]

    # Falls back to _agent_response.
    assert "summary from agent" in content
    # But surfaces a note about the unapproved Resource.
    assert "not approved" in content
    assert "verdict=pending" in content
    # Should NOT leak Resource content (it is untrusted).
    assert "not-yet-approved" not in content


@pytest.mark.asyncio
async def test_arc_without_primary_resource_id_unchanged(tmp_path):
    """Arcs with no ``_primary_resource_id`` retain the original preview path.

    This preserves behaviour for all non-fetch arcs.
    """
    arc_id = arc_manager.create_arc("plain-arc")
    arc_manager.update_status(arc_id, "active")
    set_arc_state(arc_id, "_agent_response", "classic response body")
    arc_manager.update_status(arc_id, "completed")

    conv_id = conversation.get_or_create_conversation()
    _link_conv(conv_id, arc_id)

    mock_run = AsyncMock()
    with patch(
        "carpenter.core.workflows.arc_notify_handler.thread_pools."
        "run_in_work_pool",
        mock_run,
    ):
        await handle_arc_chat_notify(1, {"arc_id": arc_id})

    msgs = conversation.get_messages(conv_id)
    system_msgs = [m for m in msgs if m["role"] == "system"]
    assert len(system_msgs) == 1
    content = system_msgs[0]["content"]
    assert "classic response body" in content
    # Preserves original framing, no primary-resource line.
    assert "Primary resource" not in content
    assert "not approved" not in content


@pytest.mark.asyncio
async def test_rejected_primary_resource_falls_back_with_note(tmp_path):
    arc_id = arc_manager.create_arc("web-lookup", integrity_level="trusted")
    arc_manager.update_status(arc_id, "active")
    fp = tmp_path / "reject.txt"
    fp.write_text("rejected text", encoding="utf-8")
    rid = res_manager.derive_resource(
        content_type="text-summary",
        file_path=str(fp),
        produced_by_arc_id=arc_id,
        produced_by_template="html_to_summary",
        template_verdict="rejected",
    )
    set_arc_state(arc_id, "_agent_response", "agent body")
    set_arc_state(arc_id, "_primary_resource_id", rid)
    arc_manager.update_status(arc_id, "completed")

    conv_id = conversation.get_or_create_conversation()
    _link_conv(conv_id, arc_id)

    mock_run = AsyncMock()
    with patch(
        "carpenter.core.workflows.arc_notify_handler.thread_pools."
        "run_in_work_pool",
        mock_run,
    ):
        await handle_arc_chat_notify(1, {"arc_id": arc_id})

    msgs = conversation.get_messages(conv_id)
    system_msgs = [m for m in msgs if m["role"] == "system"]
    content = system_msgs[0]["content"]
    assert "agent body" in content
    assert "verdict=rejected" in content
    assert "rejected text" not in content  # untrusted content not leaked


@pytest.mark.asyncio
async def test_primary_resource_id_missing_resource_falls_back(tmp_path):
    """If ``_primary_resource_id`` points at a nonexistent id, fall back cleanly."""
    arc_id = arc_manager.create_arc("stale-arc", integrity_level="trusted")
    arc_manager.update_status(arc_id, "active")
    set_arc_state(arc_id, "_agent_response", "agent response")
    set_arc_state(arc_id, "_primary_resource_id", 999_999_999)
    arc_manager.update_status(arc_id, "completed")

    conv_id = conversation.get_or_create_conversation()
    _link_conv(conv_id, arc_id)

    mock_run = AsyncMock()
    with patch(
        "carpenter.core.workflows.arc_notify_handler.thread_pools."
        "run_in_work_pool",
        mock_run,
    ):
        await handle_arc_chat_notify(1, {"arc_id": arc_id})

    msgs = conversation.get_messages(conv_id)
    system_msgs = [m for m in msgs if m["role"] == "system"]
    content = system_msgs[0]["content"]
    # Fell back to classic path.
    assert "agent response" in content
    assert "Primary resource" not in content
