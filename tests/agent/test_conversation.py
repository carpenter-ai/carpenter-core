"""Tests for carpenter.agent.conversation."""

import json
import time
from datetime import datetime, timezone, timedelta

import pytest

from carpenter.agent import conversation
from carpenter.db import get_db


def test_get_or_create_conversation_creates_new():
    """First call creates a new conversation."""
    conv_id = conversation.get_or_create_conversation()
    assert isinstance(conv_id, int)
    assert conv_id > 0


def test_get_or_create_conversation_reuses_active():
    """Subsequent calls within threshold reuse the same conversation."""
    conv_id1 = conversation.get_or_create_conversation()
    conversation.add_message(conv_id1, "user", "Hello")
    conv_id2 = conversation.get_or_create_conversation()
    assert conv_id1 == conv_id2


def test_get_or_create_conversation_new_after_boundary(monkeypatch):
    """After context_compaction_hours, a new conversation is created."""
    conv_id1 = conversation.get_or_create_conversation()
    conversation.add_message(conv_id1, "user", "Hello")

    # Manually set last_message_at to 7 hours ago
    db = get_db()
    try:
        past = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        db.execute(
            "UPDATE conversations SET last_message_at = ? WHERE id = ?",
            (past, conv_id1),
        )
        db.commit()
    finally:
        db.close()

    conv_id2 = conversation.get_or_create_conversation()
    assert conv_id2 != conv_id1


def test_add_message():
    """add_message creates a message and updates last_message_at."""
    conv_id = conversation.get_or_create_conversation()
    msg_id = conversation.add_message(conv_id, "user", "Hello world")
    assert isinstance(msg_id, int)

    conv = conversation.get_conversation(conv_id)
    assert conv["last_message_at"] is not None


def test_get_messages():
    """get_messages returns messages in chronological order."""
    conv_id = conversation.get_or_create_conversation()
    conversation.add_message(conv_id, "user", "First")
    conversation.add_message(conv_id, "assistant", "Second")
    conversation.add_message(conv_id, "user", "Third")

    messages = conversation.get_messages(conv_id)
    assert len(messages) == 3
    assert messages[0]["content"] == "First"
    assert messages[1]["role"] == "assistant"
    assert messages[2]["content"] == "Third"


def test_get_tail_messages():
    """get_tail_messages returns the last N messages."""
    conv_id = conversation.get_or_create_conversation()
    for i in range(20):
        conversation.add_message(conv_id, "user", f"Message {i}")

    tail = conversation.get_tail_messages(conv_id, count=5)
    assert len(tail) == 5
    assert tail[0]["content"] == "Message 15"
    assert tail[-1]["content"] == "Message 19"


def test_get_prior_context():
    """get_prior_context returns tail messages from previous conversation."""
    # Create first conversation with messages
    conv_id1 = conversation.get_or_create_conversation()
    conversation.add_message(conv_id1, "user", "Old message 1")
    conversation.add_message(conv_id1, "assistant", "Old response 1")
    conversation.add_message(conv_id1, "user", "Old message 2")

    # Force a new conversation by aging the first
    db = get_db()
    try:
        past = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        db.execute(
            "UPDATE conversations SET last_message_at = ? WHERE id = ?",
            (past, conv_id1),
        )
        db.commit()
    finally:
        db.close()

    conv_id2 = conversation.get_or_create_conversation()
    assert conv_id2 != conv_id1

    prior = conversation.get_prior_context(conv_id2, count=10)
    assert len(prior) == 3
    assert prior[0]["content"] == "Old message 1"


def test_get_prior_context_no_previous():
    """get_prior_context returns empty list when no previous conversation."""
    conv_id = conversation.get_or_create_conversation()
    prior = conversation.get_prior_context(conv_id)
    assert prior == []


def test_add_message_with_arc_id():
    """Messages can be associated with an arc."""
    # Create an arc first (FK constraint)
    db = get_db()
    try:
        db.execute("INSERT INTO arcs (id, name) VALUES (?, ?)", (100, "test-arc"))
        db.commit()
    finally:
        db.close()

    conv_id = conversation.get_or_create_conversation()
    msg_id = conversation.add_message(conv_id, "user", "About arc 100", arc_id=100)

    messages = conversation.get_messages(conv_id)
    assert messages[0]["arc_id"] == 100


def test_update_token_count():
    """update_token_count sets the token count."""
    conv_id = conversation.get_or_create_conversation()
    conversation.update_token_count(conv_id, 1500)

    conv = conversation.get_conversation(conv_id)
    assert conv["context_tokens"] == 1500


def test_format_messages_for_api():
    """format_messages_for_api includes system messages as user-role with prefix."""
    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
        {"role": "user", "content": "Bye"},
    ]
    api_msgs = conversation.format_messages_for_api(messages)
    # System message is merged with the following user message
    assert len(api_msgs) == 3
    assert api_msgs[0]["role"] == "user"
    assert "[System notification: System prompt]" in api_msgs[0]["content"]
    assert "Hello" in api_msgs[0]["content"]
    assert api_msgs[1] == {"role": "assistant", "content": "Hi there"}


def test_add_message_with_content_json():
    """add_message stores content_json when provided."""
    conv_id = conversation.get_or_create_conversation()
    blocks = [{"type": "text", "text": "Hello"}, {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "/tmp/x"}}]
    cj = json.dumps(blocks)
    msg_id = conversation.add_message(
        conv_id, "assistant", "Hello", content_json=cj,
    )
    messages = conversation.get_messages(conv_id)
    assert messages[0]["content_json"] == cj


def test_add_message_content_json_defaults_none():
    """content_json is None by default."""
    conv_id = conversation.get_or_create_conversation()
    conversation.add_message(conv_id, "user", "Hello")
    messages = conversation.get_messages(conv_id)
    assert messages[0]["content_json"] is None


def test_format_messages_uses_content_json():
    """format_messages_for_api uses parsed content_json when present."""
    blocks = [{"type": "text", "text": "Hi"}, {"type": "tool_use", "id": "t1", "name": "x", "input": {}}]
    messages = [
        {"role": "assistant", "content": "Hi", "content_json": json.dumps(blocks)},
    ]
    api_msgs = conversation.format_messages_for_api(messages)
    assert len(api_msgs) == 1
    assert api_msgs[0]["content"] == blocks  # parsed, not string


def test_format_messages_maps_tool_result_to_user():
    """tool_result role is mapped to 'user' for the API."""
    result_blocks = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]
    messages = [
        {"role": "tool_result", "content": "summary", "content_json": json.dumps(result_blocks)},
    ]
    api_msgs = conversation.format_messages_for_api(messages)
    assert len(api_msgs) == 1
    assert api_msgs[0]["role"] == "user"
    assert api_msgs[0]["content"] == result_blocks


def test_format_messages_skips_tool_result_without_content_json():
    """tool_result messages without content_json are skipped."""
    messages = [
        {"role": "tool_result", "content": "summary"},
    ]
    api_msgs = conversation.format_messages_for_api(messages)
    assert len(api_msgs) == 0


def test_format_messages_mixed_structured_and_plain():
    """Mix of structured and plain messages formats correctly."""
    blocks = [{"type": "text", "text": "thinking..."}]
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "thinking...", "content_json": json.dumps(blocks)},
        {"role": "tool_result", "content": "result", "content_json": json.dumps([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}])},
        {"role": "assistant", "content": "Done"},
    ]
    api_msgs = conversation.format_messages_for_api(messages)
    assert len(api_msgs) == 4
    assert api_msgs[0] == {"role": "user", "content": "Hello"}
    assert api_msgs[1]["content"] == blocks
    assert api_msgs[2]["role"] == "user"  # tool_result mapped
    assert api_msgs[3] == {"role": "assistant", "content": "Done"}


# --- Multi-conversation tests ---


def test_get_last_conversation_no_time_boundary(monkeypatch):
    """get_last_conversation returns the same conversation even after 6+ hours."""
    conv_id = conversation.create_conversation()
    conversation.add_message(conv_id, "user", "Hello")

    # Age the conversation past the time boundary
    db = get_db()
    try:
        past = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        db.execute(
            "UPDATE conversations SET last_message_at = ? WHERE id = ?",
            (past, conv_id),
        )
        db.commit()
    finally:
        db.close()

    # get_last_conversation should still return the same conversation
    result = conversation.get_last_conversation()
    assert result == conv_id

    # But get_or_create_conversation would create a new one
    result2 = conversation.get_or_create_conversation()
    assert result2 != conv_id


def test_get_last_conversation_creates_if_none():
    """get_last_conversation creates a conversation if none exist."""
    result = conversation.get_last_conversation()
    assert isinstance(result, int)
    assert result > 0


def test_create_conversation_always_new():
    """create_conversation always returns a new ID."""
    id1 = conversation.create_conversation()
    id2 = conversation.create_conversation()
    assert id1 != id2
    assert isinstance(id1, int)
    assert isinstance(id2, int)


def test_list_conversations_with_preview():
    """list_conversations_with_preview returns title and first user message."""
    conv_id = conversation.create_conversation()
    conversation.set_conversation_title(conv_id, "Test Title")
    conversation.add_message(conv_id, "user", "Hello, this is a test message for preview")

    result = conversation.list_conversations_with_preview()
    assert len(result) >= 1
    match = [c for c in result if c["id"] == conv_id]
    assert len(match) == 1
    assert match[0]["title"] == "Test Title"
    assert match[0]["preview"] == "Hello, this is a test message for preview"


def test_set_conversation_title():
    """set_conversation_title updates the title."""
    conv_id = conversation.create_conversation()
    conversation.set_conversation_title(conv_id, "My Title")

    conv = conversation.get_conversation(conv_id)
    assert conv["title"] == "My Title"


def test_link_arc_to_conversation():
    """link_arc_to_conversation links and get_conversation_arc_ids retrieves."""
    # Create an arc for FK
    db = get_db()
    try:
        db.execute("INSERT INTO arcs (id, name) VALUES (?, ?)", (200, "test-link"))
        db.commit()
    finally:
        db.close()

    conv_id = conversation.create_conversation()
    conversation.link_arc_to_conversation(conv_id, 200)

    arc_ids = conversation.get_conversation_arc_ids(conv_id)
    assert 200 in arc_ids


def test_link_arc_idempotent():
    """Duplicate link_arc_to_conversation doesn't error."""
    db = get_db()
    try:
        db.execute("INSERT INTO arcs (id, name) VALUES (?, ?)", (201, "test-idem"))
        db.commit()
    finally:
        db.close()

    conv_id = conversation.create_conversation()
    conversation.link_arc_to_conversation(conv_id, 201)
    conversation.link_arc_to_conversation(conv_id, 201)  # Should not raise

    arc_ids = conversation.get_conversation_arc_ids(conv_id)
    assert arc_ids.count(201) == 1


# --- System message formatting tests ---


def test_format_messages_system_message_prefix():
    """System messages get [System notification: ...] prefix in API format."""
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
        {"role": "system", "content": "Review ready"},
        {"role": "user", "content": "Thanks"},
    ]
    api_msgs = conversation.format_messages_for_api(messages)
    # system + next user should be merged into one user message
    assert len(api_msgs) == 3
    assert api_msgs[0] == {"role": "user", "content": "Hello"}
    assert api_msgs[1] == {"role": "assistant", "content": "Hi"}
    assert api_msgs[2]["role"] == "user"
    assert "[System notification: Review ready]" in api_msgs[2]["content"]
    assert "Thanks" in api_msgs[2]["content"]


def test_format_messages_consecutive_same_role_merged():
    """Consecutive same-role messages with string content are merged."""
    messages = [
        {"role": "system", "content": "Notification A"},
        {"role": "system", "content": "Notification B"},
        {"role": "assistant", "content": "Response"},
    ]
    api_msgs = conversation.format_messages_for_api(messages)
    # Two system->user messages merged into one user message
    assert len(api_msgs) == 2
    assert api_msgs[0]["role"] == "user"
    assert "[System notification: Notification A]" in api_msgs[0]["content"]
    assert "[System notification: Notification B]" in api_msgs[0]["content"]
    assert api_msgs[1] == {"role": "assistant", "content": "Response"}


def test_format_messages_merge_list_then_str():
    """list + str same-role: str is appended as a text block to avoid consecutive user messages."""
    result_blocks = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]
    messages = [
        {"role": "tool_result", "content": "summary", "content_json": json.dumps(result_blocks)},
        {"role": "system", "content": "Arc completed"},
    ]
    api_msgs = conversation.format_messages_for_api(messages)
    # Must be merged to a single user message (two consecutive user messages are API-invalid)
    assert len(api_msgs) == 1
    assert api_msgs[0]["role"] == "user"
    content = api_msgs[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
    assert content[-1] == {"type": "text", "text": "[System notification: Arc completed]"}


def test_format_messages_merge_str_then_list():
    """str + list same-role: str is dropped, only tool_result list kept.

    This handles the case where a system notification (review verdict) appears between an
    assistant tool_use block and its corresponding tool_result.  The user message must
    contain only the tool_result block(s); mixing text + tool_result in the same user
    message is not accepted by the Anthropic API.  The system notification is advisory;
    the AI receives the essential outcome via the tool_result block.
    """
    use_blocks = [{"type": "tool_use", "id": "t1", "name": "submit_code", "input": {}}]
    result_blocks = [{"type": "tool_result", "tool_use_id": "t1", "content": "REJECTED"}]
    messages = [
        {"role": "assistant", "content": "", "content_json": json.dumps(use_blocks)},
        {"role": "system", "content": "LLM reviewer verdict: MAJOR"},
        {"role": "tool_result", "content": "Code REJECTED", "content_json": json.dumps(result_blocks)},
    ]
    api_msgs = conversation.format_messages_for_api(messages)
    # assistant, then single merged user message with ONLY the tool_result (text dropped)
    assert len(api_msgs) == 2
    assert api_msgs[0]["role"] == "assistant"
    assert api_msgs[1]["role"] == "user"
    content = api_msgs[1]["content"]
    assert isinstance(content, list)
    assert len(content) == 1
    assert content[0] == {"type": "tool_result", "tool_use_id": "t1", "content": "REJECTED"}


def test_format_messages_system_only():
    """A conversation with only system messages formats correctly."""
    messages = [
        {"role": "system", "content": "Arc started"},
    ]
    api_msgs = conversation.format_messages_for_api(messages)
    assert len(api_msgs) == 1
    assert api_msgs[0] == {"role": "user", "content": "[System notification: Arc started]"}
