"""Tests for conversation boundary memory: summaries, recall, prior context."""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

from carpenter.agent import conversation
from carpenter.agent.conversation import generate_summary as _real_generate_summary
from carpenter.db import get_db


class TestConversationSummaryHelpers:
    """Test set/get_conversation_summary round-trip."""

    def test_set_and_get_summary(self, test_db):
        conv_id = conversation.create_conversation()
        assert conversation.get_conversation_summary(conv_id) is None

        conversation.set_conversation_summary(conv_id, "Test summary content")
        result = conversation.get_conversation_summary(conv_id)
        assert result == "Test summary content"

    def test_get_summary_nonexistent(self, test_db):
        assert conversation.get_conversation_summary(9999) is None

    def test_set_summary_overwrites(self, test_db):
        conv_id = conversation.create_conversation()
        conversation.set_conversation_summary(conv_id, "First")
        conversation.set_conversation_summary(conv_id, "Second")
        assert conversation.get_conversation_summary(conv_id) == "Second"


class TestGetPreviousConversationId:
    """Test get_previous_conversation_id helper."""

    def test_no_previous(self, test_db):
        conv_id = conversation.create_conversation()
        assert conversation.get_previous_conversation_id(conv_id) is None

    def test_with_previous(self, test_db):
        conv1 = conversation.create_conversation()
        conv2 = conversation.create_conversation()
        assert conversation.get_previous_conversation_id(conv2) == conv1

    def test_skips_to_immediate_previous(self, test_db):
        conv1 = conversation.create_conversation()
        conv2 = conversation.create_conversation()
        conv3 = conversation.create_conversation()
        assert conversation.get_previous_conversation_id(conv3) == conv2


class TestGetRecentConversations:
    """Test get_recent_conversations for memory hints."""

    def test_empty(self, test_db):
        result = conversation.get_recent_conversations()
        assert result == []

    def test_returns_titled_conversations(self, test_db):
        conv1 = conversation.create_conversation()
        conversation.set_conversation_title(conv1, "First conversation")
        conv2 = conversation.create_conversation()
        # conv2 has no title or summary — should not appear
        conv3 = conversation.create_conversation()
        conversation.set_conversation_title(conv3, "Third conversation")

        result = conversation.get_recent_conversations(limit=5)
        ids = [r["id"] for r in result]
        assert conv3 in ids
        assert conv1 in ids
        assert conv2 not in ids

    def test_returns_summarized_conversations(self, test_db):
        conv_id = conversation.create_conversation()
        conversation.set_conversation_summary(conv_id, "A summary")
        result = conversation.get_recent_conversations(limit=5)
        assert len(result) == 1
        assert result[0]["summary"] == "A summary"

    def test_excludes_archived(self, test_db):
        conv_id = conversation.create_conversation()
        conversation.set_conversation_title(conv_id, "Archived conv")
        conversation.archive_conversation(conv_id)
        result = conversation.get_recent_conversations()
        assert len(result) == 0

    def test_respects_limit(self, test_db):
        for i in range(5):
            cid = conversation.create_conversation()
            conversation.set_conversation_title(cid, f"Conv {i}")
        result = conversation.get_recent_conversations(limit=2)
        assert len(result) == 2


class TestGenerateSummary:
    """Test generate_summary function (mocked AI client).

    These tests call the real generate_summary directly (imported as
    _real_generate_summary) to bypass the autouse _no_summary_generation
    fixture.
    """

    def test_generates_summary(self, test_db):
        conv_id = conversation.create_conversation()
        conversation.add_message(conv_id, "user", "Hello, how are you?")
        conversation.add_message(conv_id, "assistant", "I'm doing well, thanks!")

        mock_resp = {"content": [{"type": "text", "text": "## Topics\n- Greeting"}]}
        with patch.dict("carpenter.config.CONFIG", {
            "ai_provider": "anthropic",
            "model_roles": {"summary": "", "default": ""},
        }):
            with patch("carpenter.agent.providers.anthropic.call", return_value=mock_resp):
                with patch("carpenter.agent.providers.anthropic.extract_text", return_value="## Topics\n- Greeting"):
                    _real_generate_summary(conv_id)

        result = conversation.get_conversation_summary(conv_id)
        assert result == "## Topics\n- Greeting"

    def test_no_messages_skips(self, test_db):
        conv_id = conversation.create_conversation()
        # No messages — should return without error
        with patch.dict("carpenter.config.CONFIG", {
            "ai_provider": "anthropic",
            "model_roles": {"summary": "", "default": ""},
        }):
            _real_generate_summary(conv_id)
        assert conversation.get_conversation_summary(conv_id) is None

    def test_truncates_long_conversations(self, test_db):
        conv_id = conversation.create_conversation()
        # Add many messages totaling > 6000 chars
        for i in range(50):
            conversation.add_message(conv_id, "user", f"Message {i}: " + "x" * 200)

        mock_resp = {"content": [{"type": "text", "text": "Summary"}]}
        with patch.dict("carpenter.config.CONFIG", {
            "ai_provider": "anthropic",
            "model_roles": {"summary": "", "default": ""},
        }):
            with patch("carpenter.agent.providers.anthropic.call", return_value=mock_resp) as mock_call:
                with patch("carpenter.agent.providers.anthropic.extract_text", return_value="Summary"):
                    _real_generate_summary(conv_id)

        # Verify the prompt was truncated (not all 50 messages)
        call_args = mock_call.call_args
        prompt = call_args[0][1][0]["content"]
        assert len(prompt) < 10000  # Should be much less than 50 * 200

    def test_handles_ai_error_gracefully(self, test_db):
        conv_id = conversation.create_conversation()
        conversation.add_message(conv_id, "user", "Test")

        with patch.dict("carpenter.config.CONFIG", {
            "ai_provider": "anthropic",
            "model_roles": {"summary": "", "default": ""},
        }):
            with patch("carpenter.agent.providers.anthropic.call", side_effect=Exception("API error")):
                # Should not raise
                _real_generate_summary(conv_id)

        assert conversation.get_conversation_summary(conv_id) is None


class TestBoundarySummaryTrigger:
    """Test that summary generation is triggered at conversation boundary."""

    def test_boundary_triggers_summary_thread(self, test_db, monkeypatch):
        # Create a conversation with an old last_message_at
        conv_id = conversation.create_conversation()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        db = get_db()
        db.execute(
            "UPDATE conversations SET last_message_at = ? WHERE id = ?",
            (old_time, conv_id),
        )
        db.commit()
        db.close()

        # Mock threading.Thread to capture summary generation
        thread_args = []
        mock_thread = MagicMock()
        mock_thread.start = MagicMock()

        def capture_thread(*args, **kwargs):
            thread_args.append((args, kwargs))
            return mock_thread

        # Monkeypatch generate_summary to a no-op for this test
        monkeypatch.setattr(
            "carpenter.agent.conversation.generate_summary",
            lambda cid: None,
        )

        monkeypatch.setattr(
            "carpenter.agent.conversation.threading.Thread",
            capture_thread,
        )

        new_conv_id = conversation.get_or_create_conversation()
        assert new_conv_id != conv_id  # New conversation created
        assert len(thread_args) == 1  # Thread was started
        # Verify the old conversation ID was passed
        assert thread_args[0][1]["args"] == (conv_id,)



class TestPriorContextPrefersSummary:
    """Test that prior context uses summary when available."""

    def test_uses_summary_over_raw_messages(self, test_db, monkeypatch):
        """When previous conversation has a summary, use it instead of raw tail.

        Prior summary now lives in the **per-turn context block** attached to
        the live user message (not the cached system prompt) — see
        invocation._build_per_turn_context_block. We verify the summary
        lands somewhere in the API call, allowing either location.
        """
        # Create previous conversation with summary
        conv1 = conversation.create_conversation()
        conversation.add_message(conv1, "user", "Old raw message")
        conversation.set_conversation_summary(conv1, "Previous conversation summary")

        # Create current conversation (simulating boundary)
        conv2 = conversation.create_conversation()
        conversation.add_message(conv2, "user", "New message")

        # Mock the AI call to capture what system prompt + messages were sent
        captured = []

        def mock_call_with_retries(system, messages, **kwargs):
            captured.append((system, messages))
            return {
                "content": [{"type": "text", "text": "Response"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }

        monkeypatch.setattr(
            "carpenter.agent.invocation._call_with_retries",
            mock_call_with_retries,
        )
        monkeypatch.setattr(
            "carpenter.agent.invocation._get_client",
            lambda _m=None: MagicMock(extract_text=lambda r: "Response", extract_code=lambda r: None, extract_code_from_text=lambda t: None),
        )
        # Need to mock claude_client.extract_code_from_text
        monkeypatch.setattr(
            "carpenter.agent.invocation.claude_client",
            MagicMock(extract_code_from_text=lambda t: None),
        )

        # Invoke without explicit conversation_id (single-medium mode)
        # The function will call get_or_create_conversation() which will return conv2
        # Then look for prior context from conv1
        from carpenter.agent import invocation
        # We need to patch get_or_create_conversation to return conv2
        monkeypatch.setattr(
            "carpenter.agent.conversation.get_or_create_conversation",
            lambda: conv2,
        )

        result = invocation.invoke_for_chat("New message", _message_already_saved=False)

        assert len(captured) == 1
        system_text, messages = captured[0]
        # Serialize everything we sent into one searchable blob
        blob = system_text + "\n" + json.dumps(messages)
        assert "Previous conversation summary" in blob

    def test_falls_back_to_raw_messages(self, test_db, monkeypatch):
        """When previous conversation has no summary, use raw tail messages.

        Prior tail is now carried in the per-turn context block on the
        live user message rather than in the cached system prompt.
        """
        conv1 = conversation.create_conversation()
        conversation.add_message(conv1, "user", "Old raw message content")

        conv2 = conversation.create_conversation()
        conversation.add_message(conv2, "user", "New message")

        captured = []

        def mock_call_with_retries(system, messages, **kwargs):
            captured.append((system, messages))
            return {
                "content": [{"type": "text", "text": "Response"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }

        monkeypatch.setattr(
            "carpenter.agent.invocation._call_with_retries",
            mock_call_with_retries,
        )
        monkeypatch.setattr(
            "carpenter.agent.invocation._get_client",
            lambda _m=None: MagicMock(extract_text=lambda r: "Response", extract_code=lambda r: None),
        )
        monkeypatch.setattr(
            "carpenter.agent.invocation.claude_client",
            MagicMock(extract_code_from_text=lambda t: None),
        )
        monkeypatch.setattr(
            "carpenter.agent.conversation.get_or_create_conversation",
            lambda: conv2,
        )

        from carpenter.agent import invocation
        result = invocation.invoke_for_chat("New message", _message_already_saved=False)

        assert len(captured) == 1
        system_text, messages = captured[0]
        blob = system_text + "\n" + json.dumps(messages)
        # Should contain raw message content somewhere in the call
        assert "Old raw message content" in blob


def _age_conversation(conv_id, hours=7):
    old_time = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    db = get_db()
    try:
        db.execute(
            "UPDATE conversations SET last_message_at = ? WHERE id = ?",
            (old_time, conv_id),
        )
        db.commit()
    finally:
        db.close()


def _no_summary_thread(monkeypatch):
    monkeypatch.setattr(
        "carpenter.agent.conversation.threading.Thread",
        lambda *a, **k: MagicMock(),
    )


class TestTaintAcrossBoundary:
    """Taint follows content across the conversation boundary (T3)."""

    def test_rollover_inherits_taint(self, test_db, monkeypatch):
        from carpenter.security.trust import (
            get_taint_sources, is_conversation_tainted, record_taint,
        )
        _no_summary_thread(monkeypatch)
        old = conversation.create_conversation()
        conversation.add_message(old, "user", "fetch that page")
        record_taint(old, "carpenter_tools.act.web")
        _age_conversation(old)

        new = conversation.get_or_create_conversation()
        assert new != old
        assert is_conversation_tainted(new)
        assert get_taint_sources(new) == ["carpenter_tools.act.web"]

    def test_rollover_from_clean_stays_clean(self, test_db, monkeypatch):
        from carpenter.security.trust import is_conversation_tainted
        _no_summary_thread(monkeypatch)
        old = conversation.create_conversation()
        conversation.add_message(old, "user", "hello")
        _age_conversation(old)

        new = conversation.get_or_create_conversation()
        assert new != old
        assert not is_conversation_tainted(new)

    def test_archived_conversation_not_resumed_or_rolled_over(self, test_db, monkeypatch):
        """An archived conversation (e.g. a finished arc's) is never picked up."""
        threads = []
        monkeypatch.setattr(
            "carpenter.agent.conversation.threading.Thread",
            lambda *a, **k: threads.append(k) or MagicMock(),
        )
        user_conv = conversation.create_conversation()
        conversation.add_message(user_conv, "user", "hi")
        arc_conv = conversation.create_conversation()
        conversation.add_message(arc_conv, "user", "raw untrusted page text")
        conversation.archive_conversation(arc_conv)

        assert conversation.get_or_create_conversation() == user_conv
        assert threads == []

        _age_conversation(user_conv)
        new = conversation.get_or_create_conversation()
        assert new not in (user_conv, arc_conv)
        assert [t["args"] for t in threads] == [(user_conv,)]

    def test_previous_conversation_skips_archived(self, test_db):
        conv1 = conversation.create_conversation()
        conv2 = conversation.create_conversation()
        conversation.archive_conversation(conv2)
        conv3 = conversation.create_conversation()
        assert conversation.get_previous_conversation_id(conv3) == conv1

    def test_injected_summary_carries_taint(self, test_db, monkeypatch):
        """Single-medium mode: injecting a tainted summary taints the new conversation."""
        from carpenter.security.trust import is_conversation_tainted, record_taint

        conv1 = conversation.create_conversation()
        conversation.add_message(conv1, "user", "Old raw message")
        conversation.set_conversation_summary(conv1, "Summary written from tainted content")
        record_taint(conv1, "carpenter_tools.act.web")

        conv2 = conversation.create_conversation()

        captured = []

        def mock_call_with_retries(system, messages, **kwargs):
            captured.append((system, messages))
            return {
                "content": [{"type": "text", "text": "Response"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }

        monkeypatch.setattr(
            "carpenter.agent.invocation._call_with_retries", mock_call_with_retries,
        )
        monkeypatch.setattr(
            "carpenter.agent.invocation._get_client",
            lambda _m=None: MagicMock(extract_text=lambda r: "Response", extract_code=lambda r: None),
        )
        monkeypatch.setattr(
            "carpenter.agent.invocation.claude_client",
            MagicMock(extract_code_from_text=lambda t: None),
        )
        monkeypatch.setattr(
            "carpenter.agent.conversation.get_or_create_conversation", lambda: conv2,
        )

        from carpenter.agent import invocation
        invocation.invoke_for_chat("New message", _message_already_saved=False)

        blob = captured[0][0] + json.dumps(captured[0][1])
        assert "Summary written from tainted content" in blob
        assert is_conversation_tainted(conv2)

    def test_taint_propagation_failure_drops_prior_context(self, test_db, monkeypatch):
        """Fail closed: if taint cannot be copied, the prior context is not injected."""
        conv1 = conversation.create_conversation()
        conversation.set_conversation_summary(conv1, "Carried summary text")
        conv2 = conversation.create_conversation()

        captured = []

        def mock_call_with_retries(system, messages, **kwargs):
            captured.append((system, messages))
            return {
                "content": [{"type": "text", "text": "Response"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }

        def boom(*a, **k):
            raise RuntimeError("db unavailable")

        monkeypatch.setattr(
            "carpenter.agent.invocation._call_with_retries", mock_call_with_retries,
        )
        monkeypatch.setattr(
            "carpenter.agent.invocation._get_client",
            lambda _m=None: MagicMock(extract_text=lambda r: "Response", extract_code=lambda r: None),
        )
        monkeypatch.setattr(
            "carpenter.agent.invocation.claude_client",
            MagicMock(extract_code_from_text=lambda t: None),
        )
        monkeypatch.setattr(
            "carpenter.agent.conversation.get_or_create_conversation", lambda: conv2,
        )
        monkeypatch.setattr("carpenter.security.trust.inherit_taint", boom)

        from carpenter.agent import invocation
        invocation.invoke_for_chat("New message", _message_already_saved=False)

        blob = captured[0][0] + json.dumps(captured[0][1])
        assert "Carried summary text" not in blob


class TestSummaryCoversTail:
    """The boundary summary is written from the end of the conversation."""

    def test_prompt_keeps_latest_messages(self, test_db):
        conv_id = conversation.create_conversation()
        conversation.add_message(conv_id, "user", "FIRST-MESSAGE " + "a" * 300)
        for i in range(40):
            conversation.add_message(conv_id, "user", f"Filler {i}: " + "x" * 200)
        conversation.add_message(conv_id, "user", "LAST-MESSAGE pending item")

        with patch.dict("carpenter.config.CONFIG", {
            "ai_provider": "anthropic",
            "model_roles": {"summary": "", "default": ""},
        }):
            with patch(
                "carpenter.agent.providers.anthropic.call",
                return_value={"content": [{"type": "text", "text": "S"}]},
            ) as mock_call:
                with patch("carpenter.agent.providers.anthropic.extract_text", return_value="S"):
                    _real_generate_summary(conv_id)

        prompt = mock_call.call_args[0][1][0]["content"]
        assert "LAST-MESSAGE pending item" in prompt
        assert "FIRST-MESSAGE" not in prompt
        # Chronological order is preserved within the kept window.
        assert prompt.index("Filler 39") < prompt.index("LAST-MESSAGE")


class TestSchemaMigration:
    """Test that the summary column migration is idempotent."""

    def test_migration_idempotent(self, test_db):
        """Running migration again should not error."""
        from carpenter.db import _migrate, get_db
        db = get_db()
        try:
            _migrate(db)  # Second run should be safe
        finally:
            db.close()
