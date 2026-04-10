"""Tests for arc completion → chat conversation notification."""

import json
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import work_queue
from carpenter.core.workflows._arc_state import set_arc_state, get_arc_state
from carpenter.core.workflows.arc_notify_handler import handle_arc_chat_notify
from carpenter.agent import conversation
from carpenter.db import get_db


# -- Enqueue tests (manager.py integration) --


def test_completed_root_arc_enqueues_chat_notify():
    """Completing a conversation-linked root arc enqueues an arc.chat_notify work item."""
    arc_id = arc_manager.create_arc("test-root")
    arc_manager.update_status(arc_id, "active")

    # Link arc to a conversation (required since PR #217)
    conv_id = conversation.get_or_create_conversation()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO conversation_arcs (conversation_id, arc_id) VALUES (?, ?)",
            (conv_id, arc_id),
        )
        db.commit()
    finally:
        db.close()

    arc_manager.update_status(arc_id, "completed")

    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'arc.chat_notify'"
        ).fetchone()
    finally:
        db.close()

    assert row is not None
    payload = json.loads(row["payload_json"])
    assert payload["arc_id"] == arc_id


def test_failed_root_arc_enqueues_chat_notify():
    """Failing a conversation-linked root arc enqueues an arc.chat_notify work item."""
    arc_id = arc_manager.create_arc("test-root")
    arc_manager.update_status(arc_id, "active")

    # Link arc to a conversation (required since PR #217)
    conv_id = conversation.get_or_create_conversation()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO conversation_arcs (conversation_id, arc_id) VALUES (?, ?)",
            (conv_id, arc_id),
        )
        db.commit()
    finally:
        db.close()

    arc_manager.update_status(arc_id, "failed")

    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'arc.chat_notify'"
        ).fetchone()
    finally:
        db.close()

    assert row is not None
    payload = json.loads(row["payload_json"])
    assert payload["arc_id"] == arc_id


def test_child_arc_does_not_enqueue_chat_notify():
    """Completing a child arc should NOT enqueue arc.chat_notify."""
    parent = arc_manager.create_arc("parent")
    arc_manager.update_status(parent, "active")
    child = arc_manager.add_child(parent, "child", goal="sub-task")
    arc_manager.update_status(child, "active")
    arc_manager.update_status(child, "completed")

    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'arc.chat_notify'"
        ).fetchone()
    finally:
        db.close()

    assert row is None


def test_unlinked_root_arc_does_not_enqueue_chat_notify():
    """Completing a root arc NOT linked to a conversation should NOT enqueue arc.chat_notify."""
    arc_id = arc_manager.create_arc("unlinked-root")
    arc_manager.update_status(arc_id, "active")
    # Do NOT link to any conversation
    arc_manager.update_status(arc_id, "completed")

    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'arc.chat_notify'"
        ).fetchone()
    finally:
        db.close()

    assert row is None


# -- Handler tests --


@pytest.mark.asyncio
async def test_handler_skips_missing_arc():
    """Handler returns early if the arc doesn't exist."""
    with patch(
        "carpenter.core.workflows.arc_notify_handler.arc_manager.get_arc",
        return_value=None,
    ):
        # Should not raise
        await handle_arc_chat_notify(1, {"arc_id": 99999})


@pytest.mark.asyncio
async def test_handler_skips_silent_completed_arc():
    """Silent completed arcs are not notified."""
    arc_id = arc_manager.create_arc("silent-arc")
    arc_manager.update_status(arc_id, "active")
    set_arc_state(arc_id, "_silent", True)
    arc_manager.update_status(arc_id, "completed")

    with patch(
        "carpenter.core.workflows.arc_notify_handler.thread_pools"
    ) as mock_tp:
        await handle_arc_chat_notify(1, {"arc_id": arc_id})
        mock_tp.run_in_work_pool.assert_not_called()


@pytest.mark.asyncio
async def test_handler_notifies_for_silent_failed_arc():
    """Silent failed arcs ARE notified (failure always escalates)."""
    arc_id = arc_manager.create_arc("silent-fail")
    arc_manager.update_status(arc_id, "active")
    set_arc_state(arc_id, "_silent", True)
    arc_manager.update_status(arc_id, "failed")

    conv_id = conversation.get_or_create_conversation()

    # Link conversation to arc
    db = get_db()
    try:
        db.execute(
            "INSERT INTO conversation_arcs (conversation_id, arc_id) VALUES (?, ?)",
            (conv_id, arc_id),
        )
        db.commit()
    finally:
        db.close()

    mock_invoke = MagicMock(return_value={"response": "ok"})
    mock_run = AsyncMock(return_value={"response": "ok"})

    with patch(
        "carpenter.core.workflows.arc_notify_handler.invocation.invoke_for_chat",
        mock_invoke,
    ), patch(
        "carpenter.core.workflows.arc_notify_handler.thread_pools.run_in_work_pool",
        mock_run,
    ):
        await handle_arc_chat_notify(1, {"arc_id": arc_id})
        mock_run.assert_called_once()


@pytest.mark.asyncio
async def test_handler_builds_completed_message_with_result():
    """Completed arc with _agent_response includes result in message."""
    arc_id = arc_manager.create_arc("weather-check")
    arc_manager.update_status(arc_id, "active")
    set_arc_state(arc_id, "_agent_response", "It is 15C and sunny in Oxford.")
    arc_manager.update_status(arc_id, "completed")

    conv_id = conversation.get_or_create_conversation()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO conversation_arcs (conversation_id, arc_id) VALUES (?, ?)",
            (conv_id, arc_id),
        )
        db.commit()
    finally:
        db.close()

    mock_run = AsyncMock()
    with patch(
        "carpenter.core.workflows.arc_notify_handler.thread_pools.run_in_work_pool",
        mock_run,
    ):
        await handle_arc_chat_notify(1, {"arc_id": arc_id})

    # Check the system message was added and is hidden
    msgs = conversation.get_messages(conv_id)
    system_msgs = [m for m in msgs if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert system_msgs[0]["content"]  # non-empty
    assert system_msgs[0]["hidden"], "Arc notify messages should be hidden from UI"


@pytest.mark.asyncio
async def test_handler_builds_failed_message():
    """Failed arc notification message mentions failure."""
    arc_id = arc_manager.create_arc("broken-task")
    arc_manager.update_status(arc_id, "active")
    arc_manager.update_status(arc_id, "failed")

    conv_id = conversation.get_or_create_conversation()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO conversation_arcs (conversation_id, arc_id) VALUES (?, ?)",
            (conv_id, arc_id),
        )
        db.commit()
    finally:
        db.close()

    mock_run = AsyncMock()
    with patch(
        "carpenter.core.workflows.arc_notify_handler.thread_pools.run_in_work_pool",
        mock_run,
    ):
        await handle_arc_chat_notify(1, {"arc_id": arc_id})

    msgs = conversation.get_messages(conv_id)
    # Filter to just our notification (root_failure handler may also add one)
    notify_msgs = [
        m for m in msgs
        if m["role"] == "system" and m.get("arc_id") == arc_id
    ]
    assert len(notify_msgs) == 1
    assert notify_msgs[0]["content"]  # non-empty
    assert notify_msgs[0]["hidden"], "Arc notify messages should be hidden from UI"


@pytest.mark.asyncio
async def test_handler_truncates_long_result():
    """Long _agent_response is truncated to RESULT_PREVIEW_MAX."""
    arc_id = arc_manager.create_arc("verbose-arc")
    arc_manager.update_status(arc_id, "active")
    long_response = "x" * 5000
    set_arc_state(arc_id, "_agent_response", long_response)
    arc_manager.update_status(arc_id, "completed")

    conv_id = conversation.get_or_create_conversation()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO conversation_arcs (conversation_id, arc_id) VALUES (?, ?)",
            (conv_id, arc_id),
        )
        db.commit()
    finally:
        db.close()

    mock_run = AsyncMock()
    with patch(
        "carpenter.core.workflows.arc_notify_handler.thread_pools.run_in_work_pool",
        mock_run,
    ):
        await handle_arc_chat_notify(1, {"arc_id": arc_id})

    msgs = conversation.get_messages(conv_id)
    system_msgs = [m for m in msgs if m["role"] == "system"]
    assert len(system_msgs) == 1
    # Content should be shorter than the full 5000-char response
    assert len(system_msgs[0]["content"]) < 5000
    assert system_msgs[0]["hidden"], "Arc notify messages should be hidden from UI"


@pytest.mark.asyncio
async def test_handler_includes_read_arc_result_nudge_when_truncated():
    """When result is truncated, notification includes nudge to use read_arc_result."""
    arc_id = arc_manager.create_arc("big-result-arc")
    arc_manager.update_status(arc_id, "active")
    long_response = "data_" * 2000  # 10000 chars, well over 4000 limit
    set_arc_state(arc_id, "_agent_response", long_response)
    arc_manager.update_status(arc_id, "completed")

    conv_id = conversation.get_or_create_conversation()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO conversation_arcs (conversation_id, arc_id) VALUES (?, ?)",
            (conv_id, arc_id),
        )
        db.commit()
    finally:
        db.close()

    mock_run = AsyncMock()
    with patch(
        "carpenter.core.workflows.arc_notify_handler.thread_pools.run_in_work_pool",
        mock_run,
    ):
        await handle_arc_chat_notify(1, {"arc_id": arc_id})

    msgs = conversation.get_messages(conv_id)
    system_msgs = [m for m in msgs if m["role"] == "system"]
    assert len(system_msgs) == 1
    content = system_msgs[0]["content"]
    # Should contain the nudge with tool name and arc_id
    assert "read_arc_result" in content
    assert f"arc_id={arc_id}" in content


@pytest.mark.asyncio
async def test_handler_no_nudge_when_result_fits():
    """Short results that fit within RESULT_PREVIEW_MAX should not include nudge."""
    arc_id = arc_manager.create_arc("short-result-arc")
    arc_manager.update_status(arc_id, "active")
    short_response = "Brief answer: 42"
    set_arc_state(arc_id, "_agent_response", short_response)
    arc_manager.update_status(arc_id, "completed")

    conv_id = conversation.get_or_create_conversation()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO conversation_arcs (conversation_id, arc_id) VALUES (?, ?)",
            (conv_id, arc_id),
        )
        db.commit()
    finally:
        db.close()

    mock_run = AsyncMock()
    with patch(
        "carpenter.core.workflows.arc_notify_handler.thread_pools.run_in_work_pool",
        mock_run,
    ):
        await handle_arc_chat_notify(1, {"arc_id": arc_id})

    msgs = conversation.get_messages(conv_id)
    system_msgs = [m for m in msgs if m["role"] == "system"]
    assert len(system_msgs) == 1
    content = system_msgs[0]["content"]
    # Should NOT contain the nudge
    assert "read_arc_result" not in content


@pytest.mark.asyncio
async def test_handler_falls_back_to_last_conversation():
    """When no conversation is linked, handler uses the most recent one."""
    arc_id = arc_manager.create_arc("orphan-arc")
    arc_manager.update_status(arc_id, "active")
    set_arc_state(arc_id, "_agent_response", "Done.")
    arc_manager.update_status(arc_id, "completed")

    # No conversation_arcs link — handler should fall back
    conv_id = conversation.get_or_create_conversation()

    mock_run = AsyncMock()
    with patch(
        "carpenter.core.workflows.arc_notify_handler.thread_pools.run_in_work_pool",
        mock_run,
    ):
        await handle_arc_chat_notify(1, {"arc_id": arc_id})

    # Message should land in the existing conversation
    msgs = conversation.get_messages(conv_id)
    system_msgs = [m for m in msgs if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert system_msgs[0]["content"]  # non-empty
    assert system_msgs[0]["hidden"], "Arc notify messages should be hidden from UI"


@pytest.mark.asyncio
async def test_handler_creates_conversation_when_none_exists():
    """When no conversation exists at all, handler creates one."""
    arc_id = arc_manager.create_arc("first-arc")
    arc_manager.update_status(arc_id, "active")
    arc_manager.update_status(arc_id, "completed")

    mock_run = AsyncMock()
    with patch(
        "carpenter.core.workflows.arc_notify_handler.thread_pools.run_in_work_pool",
        mock_run,
    ):
        await handle_arc_chat_notify(1, {"arc_id": arc_id})

    # A conversation should have been created with the system message
    db = get_db()
    try:
        row = db.execute("SELECT COUNT(*) as cnt FROM conversations").fetchone()
        assert row["cnt"] >= 1
        # Check message exists and is hidden
        msg_row = db.execute(
            "SELECT * FROM messages WHERE role = 'system'"
        ).fetchone()
        assert msg_row is not None
        assert msg_row["content"]  # non-empty
        assert msg_row["hidden"], "Arc notify messages should be hidden from UI"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_handler_skips_archived_conversation():
    """Archived conversation is skipped in favor of fallback."""
    arc_id = arc_manager.create_arc("archived-conv-arc")
    arc_manager.update_status(arc_id, "active")
    set_arc_state(arc_id, "_agent_response", "Result.")
    arc_manager.update_status(arc_id, "completed")

    # Create and archive a conversation linked to the arc
    conv_id = conversation.get_or_create_conversation()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO conversation_arcs (conversation_id, arc_id) VALUES (?, ?)",
            (conv_id, arc_id),
        )
        db.execute(
            "UPDATE conversations SET archived = TRUE WHERE id = ?", (conv_id,)
        )
        db.commit()
    finally:
        db.close()

    # Create a second (non-archived) conversation to fall back to
    conv_id2 = conversation._create_conversation(get_db())

    mock_run = AsyncMock()
    with patch(
        "carpenter.core.workflows.arc_notify_handler.thread_pools.run_in_work_pool",
        mock_run,
    ):
        await handle_arc_chat_notify(1, {"arc_id": arc_id})

    # Message should be in the non-archived conversation
    msgs = conversation.get_messages(conv_id2)
    system_msgs = [m for m in msgs if m["role"] == "system"]
    assert len(system_msgs) == 1


@pytest.mark.asyncio
async def test_handler_invokes_chat_with_correct_params():
    """Verify invoke_for_chat is called with _system_triggered=True."""
    arc_id = arc_manager.create_arc("invoke-test")
    arc_manager.update_status(arc_id, "active")
    set_arc_state(arc_id, "_agent_response", "42")
    arc_manager.update_status(arc_id, "completed")

    conv_id = conversation.get_or_create_conversation()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO conversation_arcs (conversation_id, arc_id) VALUES (?, ?)",
            (conv_id, arc_id),
        )
        db.commit()
    finally:
        db.close()

    mock_run = AsyncMock()
    with patch(
        "carpenter.core.workflows.arc_notify_handler.thread_pools.run_in_work_pool",
        mock_run,
    ):
        await handle_arc_chat_notify(1, {"arc_id": arc_id})

    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    # First positional arg is invoke_for_chat function
    # Second positional arg is the message (non-empty string)
    assert isinstance(args[1], str) and args[1]
    assert kwargs["conversation_id"] == conv_id
    assert kwargs["_message_already_saved"] is True
    assert kwargs["_system_triggered"] is True
