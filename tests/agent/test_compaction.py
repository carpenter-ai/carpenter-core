"""Tests for context window compaction in invocation.py."""

import json
from unittest.mock import patch, MagicMock

import pytest

from carpenter.agent import invocation, conversation
from carpenter.db import get_db
from tests.agent.conftest import _mock_api_response


def _setup_conversation_with_messages(n_messages=20, chars_per_message=1000):
    """Create a conversation with N user/assistant message pairs.

    Returns (conversation_id, message_ids) where message_ids is a list
    of all message IDs in order.
    """
    conv_id = conversation.create_conversation()
    msg_ids = []
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        content = f"Message {i}: " + "x" * chars_per_message
        mid = conversation.add_message(conv_id, role, content)
        msg_ids.append(mid)
    return conv_id, msg_ids


class TestEstimateTokens:
    """Tests for _estimate_tokens."""

    def test_empty_messages(self):
        assert invocation._estimate_tokens([], "") == 0

    def test_string_content(self):
        messages = [
            {"role": "user", "content": "Hello world"},  # 11 chars
            {"role": "assistant", "content": "Hi there"},  # 8 chars
        ]
        # (11 + 8) / 4 = 4
        result = invocation._estimate_tokens(messages, "")
        assert result == 4

    def test_system_prompt_included(self):
        messages = [{"role": "user", "content": "Hi"}]  # 2 chars
        system = "You are a helpful assistant."  # 29 chars
        # (2 + 29) / 4 = 7
        result = invocation._estimate_tokens(messages, system)
        assert result == 7

    def test_structured_content(self):
        block = [{"type": "tool_use", "id": "1", "name": "read_file", "input": {"path": "/tmp/x"}}]
        messages = [{"role": "assistant", "content": block}]
        result = invocation._estimate_tokens(messages)
        # Should count JSON-serialized length
        expected = len(json.dumps(block)) // 4
        assert result == expected

    def test_reasonable_for_typical_conversation(self):
        """A conversation with ~800k chars should estimate ~200k tokens."""
        messages = [
            {"role": "user", "content": "x" * 400000},
            {"role": "assistant", "content": "y" * 400000},
        ]
        result = invocation._estimate_tokens(messages, "")
        assert 190000 <= result <= 210000


class TestShouldCompact:
    """Tests for _should_compact."""

    def test_below_fraction_threshold(self, test_db):
        # 100k tokens, 200k window, 0.8 threshold = 160k needed
        assert invocation._should_compact(100000, 200000) is False

    def test_at_fraction_threshold(self, test_db):
        # 160k tokens, 200k window, 0.8 threshold = exactly at threshold
        assert invocation._should_compact(160000, 200000) is True

    def test_above_fraction_threshold(self, test_db):
        assert invocation._should_compact(180000, 200000) is True

    def test_absolute_threshold_disabled(self, test_db):
        # Default compaction_threshold_tokens = 0 means disabled
        assert invocation._should_compact(50000, 200000) is False

    def test_absolute_threshold_fires(self, test_db, monkeypatch):
        monkeypatch.setitem(
            invocation.config.CONFIG, "compaction_threshold_tokens", 10000
        )
        # 10000 tokens >= 10000 abs threshold, even though fraction (10k/200k=5%) is low
        assert invocation._should_compact(10000, 200000) is True

    def test_absolute_threshold_below(self, test_db, monkeypatch):
        monkeypatch.setitem(
            invocation.config.CONFIG, "compaction_threshold_tokens", 10000
        )
        assert invocation._should_compact(9999, 200000) is False


class TestCompactMessages:
    """Tests for _compact_messages."""

    @patch("carpenter.agent.invocation._call_with_retries")
    def test_compaction_creates_event_record(self, mock_call, test_db):
        """Compaction inserts a row in compaction_events."""
        conv_id, msg_ids = _setup_conversation_with_messages(20, 500)

        messages = conversation.get_messages(conv_id)
        api_messages = conversation.format_messages_for_api(messages)
        db_ids = [m["id"] for m in messages]

        # Pad db_ids to match api_messages length (merging may have happened)
        while len(db_ids) < len(api_messages):
            db_ids.append(None)
        db_ids = db_ids[:len(api_messages)]

        mock_call.return_value = _mock_api_response(
            "Summary: discussed message topics.", model="test-model"
        )

        new_msgs, new_ids, reclaimed = invocation._compact_messages(
            api_messages, conv_id, db_ids, "system prompt",
        )

        # Should have created a compaction_events row
        db = get_db()
        try:
            row = db.execute(
                "SELECT * FROM compaction_events WHERE conversation_id = ?",
                (conv_id,),
            ).fetchone()
            assert row is not None
            assert row["conversation_id"] == conv_id
            assert row["tokens_reclaimed"] is not None
            assert row["model"] is not None
        finally:
            db.close()

    @patch("carpenter.agent.invocation._call_with_retries")
    def test_compaction_preserves_recent_messages(self, mock_call, test_db):
        """Recent N messages are not compacted."""
        conv_id, msg_ids = _setup_conversation_with_messages(20, 500)

        messages = conversation.get_messages(conv_id)
        api_messages = conversation.format_messages_for_api(messages)
        db_ids = [m["id"] for m in messages]
        while len(db_ids) < len(api_messages):
            db_ids.append(None)
        db_ids = db_ids[:len(api_messages)]

        mock_call.return_value = _mock_api_response("Summary of context.")

        preserve_n = invocation.config.CONFIG.get("compaction_preserve_recent", 8)
        original_tail = api_messages[-preserve_n:]

        new_msgs, new_ids, reclaimed = invocation._compact_messages(
            api_messages, conv_id, db_ids, "system prompt",
        )

        # The result should have 1 summary message + preserve_n tail messages
        assert len(new_msgs) == 1 + preserve_n

        # Verify the tail messages are preserved (content matches)
        for i, orig in enumerate(original_tail):
            assert new_msgs[1 + i]["content"] == orig["content"]

    @patch("carpenter.agent.invocation._call_with_retries")
    def test_compaction_inserts_synthetic_message(self, mock_call, test_db):
        """Compaction inserts a synthetic system message with compaction_event_id."""
        conv_id, msg_ids = _setup_conversation_with_messages(20, 500)

        messages = conversation.get_messages(conv_id)
        api_messages = conversation.format_messages_for_api(messages)
        db_ids = [m["id"] for m in messages]
        while len(db_ids) < len(api_messages):
            db_ids.append(None)
        db_ids = db_ids[:len(api_messages)]

        mock_call.return_value = _mock_api_response("Context summary here.")

        new_msgs, new_ids, reclaimed = invocation._compact_messages(
            api_messages, conv_id, db_ids, "system prompt",
        )

        # Check DB for the synthetic message
        db = get_db()
        try:
            row = db.execute(
                "SELECT * FROM messages WHERE conversation_id = ? AND role = 'system' "
                "AND compaction_event_id IS NOT NULL ORDER BY id DESC LIMIT 1",
                (conv_id,),
            ).fetchone()
            assert row is not None
            assert "Context summary here." in row["content"]
            assert row["compaction_event_id"] is not None

            # Verify the compaction_event_id points to a valid event
            event = db.execute(
                "SELECT * FROM compaction_events WHERE id = ?",
                (row["compaction_event_id"],),
            ).fetchone()
            assert event is not None
        finally:
            db.close()

    @patch("carpenter.agent.invocation._call_with_retries")
    def test_original_messages_not_deleted(self, mock_call, test_db):
        """Compaction does not delete any messages from the database."""
        conv_id, msg_ids = _setup_conversation_with_messages(20, 500)

        messages = conversation.get_messages(conv_id)
        api_messages = conversation.format_messages_for_api(messages)
        db_ids = [m["id"] for m in messages]
        while len(db_ids) < len(api_messages):
            db_ids.append(None)
        db_ids = db_ids[:len(api_messages)]

        original_count = len(messages)

        mock_call.return_value = _mock_api_response("Summary.")

        invocation._compact_messages(
            api_messages, conv_id, db_ids, "system prompt",
        )

        # All original messages should still exist
        after = conversation.get_messages(conv_id)
        # After compaction, we have original_count + 1 synthetic message
        assert len(after) == original_count + 1

        # Verify no original messages were deleted
        after_ids = {m["id"] for m in after}
        for mid in msg_ids:
            assert mid in after_ids

    @patch("carpenter.agent.invocation._call_with_retries")
    def test_compaction_event_has_correct_message_range(self, mock_call, test_db):
        """message_id_start and message_id_end span the compacted segment."""
        conv_id, msg_ids = _setup_conversation_with_messages(20, 500)

        messages = conversation.get_messages(conv_id)
        api_messages = conversation.format_messages_for_api(messages)
        db_ids = [m["id"] for m in messages]
        while len(db_ids) < len(api_messages):
            db_ids.append(None)
        db_ids = db_ids[:len(api_messages)]

        mock_call.return_value = _mock_api_response("Summary.")

        preserve_n = invocation.config.CONFIG.get("compaction_preserve_recent", 8)
        compact_end = len(api_messages) - preserve_n
        expected_ids = [mid for mid in db_ids[:compact_end] if mid is not None]
        expected_start = min(expected_ids)
        expected_end = max(expected_ids)

        invocation._compact_messages(
            api_messages, conv_id, db_ids, "system prompt",
        )

        db = get_db()
        try:
            row = db.execute(
                "SELECT * FROM compaction_events WHERE conversation_id = ?",
                (conv_id,),
            ).fetchone()
            assert row["message_id_start"] == expected_start
            assert row["message_id_end"] == expected_end
        finally:
            db.close()

    @patch("carpenter.agent.invocation._call_with_retries")
    def test_multiple_compactions_in_one_conversation(self, mock_call, test_db):
        """Multiple compaction rounds produce separate events."""
        conv_id, msg_ids = _setup_conversation_with_messages(20, 500)

        messages = conversation.get_messages(conv_id)
        api_messages = conversation.format_messages_for_api(messages)
        db_ids = [m["id"] for m in messages]
        while len(db_ids) < len(api_messages):
            db_ids.append(None)
        db_ids = db_ids[:len(api_messages)]

        mock_call.return_value = _mock_api_response("First summary.")

        # First compaction
        new_msgs, new_ids, reclaimed1 = invocation._compact_messages(
            api_messages, conv_id, db_ids, "system prompt",
        )
        assert reclaimed1 > 0

        # Add more messages to the conversation
        for i in range(10):
            role = "user" if i % 2 == 0 else "assistant"
            conversation.add_message(conv_id, role, f"Extra msg {i}: " + "z" * 500)

        # Re-load messages and compact again
        messages2 = conversation.get_messages(conv_id)
        api_messages2 = conversation.format_messages_for_api(messages2)
        db_ids2 = [m["id"] for m in messages2]
        while len(db_ids2) < len(api_messages2):
            db_ids2.append(None)
        db_ids2 = db_ids2[:len(api_messages2)]

        mock_call.return_value = _mock_api_response("Second summary.")

        new_msgs2, new_ids2, reclaimed2 = invocation._compact_messages(
            api_messages2, conv_id, db_ids2, "system prompt",
        )

        # Should now have 2 compaction events
        db = get_db()
        try:
            events = db.execute(
                "SELECT * FROM compaction_events WHERE conversation_id = ? ORDER BY id",
                (conv_id,),
            ).fetchall()
            assert len(events) == 2
            assert events[0]["id"] != events[1]["id"]
        finally:
            db.close()

    def test_too_few_messages_skips_compaction(self, test_db):
        """If there are fewer messages than preserve_recent, compaction is skipped."""
        conv_id = conversation.create_conversation()
        # Add fewer messages than the preserve count
        for i in range(4):
            role = "user" if i % 2 == 0 else "assistant"
            conversation.add_message(conv_id, role, f"Short msg {i}")

        messages = conversation.get_messages(conv_id)
        api_messages = conversation.format_messages_for_api(messages)
        db_ids = [m["id"] for m in messages]

        # No mock needed — should return early without calling AI
        new_msgs, new_ids, reclaimed = invocation._compact_messages(
            api_messages, conv_id, db_ids, "system prompt",
        )

        assert reclaimed == 0
        assert new_msgs == api_messages

    @patch("carpenter.agent.invocation._call_with_retries")
    def test_compaction_marks_original_messages(self, mock_call, test_db):
        """Original compacted messages get compaction_event_id set."""
        conv_id, msg_ids = _setup_conversation_with_messages(20, 500)

        messages = conversation.get_messages(conv_id)
        api_messages = conversation.format_messages_for_api(messages)
        db_ids = [m["id"] for m in messages]
        while len(db_ids) < len(api_messages):
            db_ids.append(None)
        db_ids = db_ids[:len(api_messages)]

        mock_call.return_value = _mock_api_response("Summary.")

        preserve_n = invocation.config.CONFIG.get("compaction_preserve_recent", 8)
        compact_end = len(api_messages) - preserve_n
        compacted_ids = [mid for mid in db_ids[:compact_end] if mid is not None]

        invocation._compact_messages(
            api_messages, conv_id, db_ids, "system prompt",
        )

        db = get_db()
        try:
            event = db.execute(
                "SELECT id FROM compaction_events WHERE conversation_id = ?",
                (conv_id,),
            ).fetchone()
            event_id = event["id"]

            for mid in compacted_ids:
                row = db.execute(
                    "SELECT compaction_event_id FROM messages WHERE id = ?", (mid,)
                ).fetchone()
                assert row["compaction_event_id"] == event_id
        finally:
            db.close()

    @patch("carpenter.agent.invocation._call_with_retries")
    def test_compaction_returns_reclaimed_tokens(self, mock_call, test_db):
        """Compaction should report positive tokens_reclaimed."""
        conv_id, msg_ids = _setup_conversation_with_messages(20, 2000)

        messages = conversation.get_messages(conv_id)
        api_messages = conversation.format_messages_for_api(messages)
        db_ids = [m["id"] for m in messages]
        while len(db_ids) < len(api_messages):
            db_ids.append(None)
        db_ids = db_ids[:len(api_messages)]

        # Short summary = lots of reclaimed tokens
        mock_call.return_value = _mock_api_response("Brief summary.")

        _, _, reclaimed = invocation._compact_messages(
            api_messages, conv_id, db_ids, "system prompt",
        )

        assert reclaimed > 0

    @patch("carpenter.agent.invocation._call_with_retries")
    def test_compaction_summary_response_none_skips(self, mock_call, test_db):
        """If summarization returns None, compaction is skipped."""
        conv_id, msg_ids = _setup_conversation_with_messages(20, 500)

        messages = conversation.get_messages(conv_id)
        api_messages = conversation.format_messages_for_api(messages)
        db_ids = [m["id"] for m in messages]
        while len(db_ids) < len(api_messages):
            db_ids.append(None)
        db_ids = db_ids[:len(api_messages)]

        mock_call.return_value = None

        new_msgs, new_ids, reclaimed = invocation._compact_messages(
            api_messages, conv_id, db_ids, "system prompt",
        )

        assert reclaimed == 0
        assert new_msgs is api_messages

    @patch("carpenter.agent.invocation._call_with_retries")
    def test_compaction_empty_summary_skips(self, mock_call, test_db):
        """If summarization returns empty text, compaction is skipped."""
        conv_id, msg_ids = _setup_conversation_with_messages(20, 500)

        messages = conversation.get_messages(conv_id)
        api_messages = conversation.format_messages_for_api(messages)
        db_ids = [m["id"] for m in messages]
        while len(db_ids) < len(api_messages):
            db_ids.append(None)
        db_ids = db_ids[:len(api_messages)]

        mock_call.return_value = _mock_api_response("")

        new_msgs, new_ids, reclaimed = invocation._compact_messages(
            api_messages, conv_id, db_ids, "system prompt",
        )

        assert reclaimed == 0
        assert new_msgs is api_messages


class TestBuildMessageIdMap:
    """Tests for _build_message_id_map."""

    def test_simple_mapping(self, test_db):
        """1:1 mapping when no merging occurs."""
        conv_id = conversation.create_conversation()
        m1 = conversation.add_message(conv_id, "user", "Hello")
        m2 = conversation.add_message(conv_id, "assistant", "Hi")
        m3 = conversation.add_message(conv_id, "user", "How are you?")

        messages = conversation.get_messages(conv_id)
        api_messages = conversation.format_messages_for_api(messages)

        result = invocation._build_message_id_map(messages, api_messages)
        assert len(result) == len(api_messages)
        assert m1 in result
        assert m2 in result
        assert m3 in result

    def test_length_matches_api_messages(self, test_db):
        """Output always matches api_messages length."""
        conv_id = conversation.create_conversation()
        for i in range(10):
            role = "user" if i % 2 == 0 else "assistant"
            conversation.add_message(conv_id, role, f"Message {i}")

        messages = conversation.get_messages(conv_id)
        api_messages = conversation.format_messages_for_api(messages)

        result = invocation._build_message_id_map(messages, api_messages)
        assert len(result) == len(api_messages)


class TestCompactionIntegration:
    """Integration tests: compaction within invoke_for_chat."""

    @patch("carpenter.agent.invocation._call_with_retries")
    def test_compaction_triggers_in_chat_loop(self, mock_call, test_db, monkeypatch):
        """Compaction fires when token threshold is exceeded during chat."""
        # Set a very low absolute threshold to trigger compaction
        monkeypatch.setitem(
            invocation.config.CONFIG, "compaction_threshold_tokens", 100
        )

        # Create a conversation with lots of content
        conv_id = conversation.create_conversation()
        for i in range(20):
            role = "user" if i % 2 == 0 else "assistant"
            conversation.add_message(conv_id, role, f"Message {i}: " + "x" * 2000)

        # The first call is the compaction summarization, the second is the actual chat response
        mock_call.side_effect = [
            # Compaction summary response
            _mock_api_response("Compacted summary of earlier conversation."),
            # Final chat response
            _mock_api_response("Hello! Here is my response."),
        ]

        result = invocation.invoke_for_chat(
            "New user question",
            conversation_id=conv_id,
        )

        assert result["response_text"] == "Hello! Here is my response."
        assert result["conversation_id"] == conv_id

        # Verify compaction event was created
        db = get_db()
        try:
            events = db.execute(
                "SELECT * FROM compaction_events WHERE conversation_id = ?",
                (conv_id,),
            ).fetchall()
            assert len(events) >= 1
        finally:
            db.close()

    @patch("carpenter.agent.invocation._call_with_retries")
    def test_no_compaction_below_threshold(self, mock_call, test_db):
        """No compaction when below threshold (default: 80% of 200k)."""
        # Create a small conversation
        conv_id = conversation.create_conversation()
        conversation.add_message(conv_id, "user", "Hello")
        conversation.add_message(conv_id, "assistant", "Hi there")

        mock_call.return_value = _mock_api_response("Response to your question.")

        result = invocation.invoke_for_chat(
            "How are you?",
            conversation_id=conv_id,
        )

        assert result["response_text"] == "Response to your question."

        # Should only have been called once (no compaction call)
        assert mock_call.call_count == 1

        # No compaction events
        db = get_db()
        try:
            events = db.execute(
                "SELECT * FROM compaction_events WHERE conversation_id = ?",
                (conv_id,),
            ).fetchall()
            assert len(events) == 0
        finally:
            db.close()


class TestTokenEstimation:
    """Additional tests for _estimate_tokens to verify reasonableness."""

    def test_empty_system_and_messages(self):
        assert invocation._estimate_tokens([], "") == 0

    def test_only_system_prompt(self):
        # 40 chars / 4 = 10 tokens
        result = invocation._estimate_tokens([], "a" * 40)
        assert result == 10

    def test_mixed_content_types(self):
        """Messages with both string and structured content."""
        messages = [
            {"role": "user", "content": "Hello"},  # 5 chars
            {"role": "assistant", "content": [
                {"type": "text", "text": "Hi"},
                {"type": "tool_use", "id": "1", "name": "foo", "input": {}},
            ]},
        ]
        result = invocation._estimate_tokens(messages, "")
        # string "Hello" = 5 chars, JSON of list will be some amount
        assert result > 0
