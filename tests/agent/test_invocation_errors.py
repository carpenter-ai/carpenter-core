"""Tests for error classification in carpenter.agent.invocation.

Moved from test_invocation.py — covers error classification integration
in _call_with_retries and invoke_for_chat.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from carpenter.agent import invocation, conversation
from carpenter.db import get_db
from tests.agent.conftest import _mock_api_response


class TestErrorClassification:
    """Tests for error classification integration."""

    @patch("carpenter.agent.invocation.time")
    @patch("carpenter.agent.invocation.claude_client")
    def test_call_with_retries_returns_error_info_on_429(self, mock_client, mock_time):
        """Test that _call_with_retries returns ErrorInfo for rate limit errors."""
        # Create mock 429 exception
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"retry-after": "30"}

        mock_exception = Exception("Rate limit")
        mock_exception.response = mock_response

        mock_client.call.side_effect = mock_exception
        mock_time.sleep = MagicMock()

        result = invocation._call_with_retries(
            "system", [], api_key="key", max_retries=2,
        )

        # Should return dict with _error key
        assert result is not None
        assert "_error" in result
        error_info = result["_error"]
        assert error_info.type == "RateLimitError"
        assert error_info.status_code == 429
        assert error_info.retry_after == 30.0
        assert error_info.retry_count == 2

    @patch("carpenter.agent.invocation.time")
    @patch("carpenter.agent.invocation.claude_client")
    def test_call_with_retries_returns_error_info_on_network_error(self, mock_client, mock_time):
        """Test that _call_with_retries returns ErrorInfo for network errors."""
        class TimeoutException(Exception):
            pass

        mock_client.call.side_effect = TimeoutException("Connection timed out")
        mock_time.sleep = MagicMock()

        result = invocation._call_with_retries(
            "system", [], api_key="key", max_retries=3,
        )

        assert result is not None
        assert "_error" in result
        error_info = result["_error"]
        assert error_info.type == "NetworkError"
        assert error_info.retry_count == 3
        msg_lower = error_info.message.lower()
        assert "timeout" in msg_lower or "timed out" in msg_lower

    @patch("carpenter.agent.invocation.claude_client")
    def test_chat_saves_error_as_system_message(self, mock_client):
        """Test that chat() saves API errors as system messages with metadata."""
        # Create mock network error
        class ConnectError(Exception):
            pass

        mock_client.call.side_effect = ConnectError("Failed to connect")

        # Clear any existing conversations
        db = get_db()
        db.execute("DELETE FROM messages")
        db.execute("DELETE FROM conversations")
        db.commit()

        result = invocation.invoke_for_chat("Test message")

        # Should return error in response_text
        assert "connection" in result["response_text"].lower()

        # Check that message was saved with system role
        messages = db.execute(
            "SELECT role, content, content_json FROM messages WHERE conversation_id = ?",
            (result["conversation_id"],)
        ).fetchall()

        # Should have user message + error message
        assert len(messages) >= 2
        error_msg = [m for m in messages if m[0] == "system"][0]
        assert "connection" in error_msg[1].lower()

        # Check content_json has error_info
        content_json = json.loads(error_msg[2])
        assert "error_info" in content_json
        assert content_json["error_info"]["type"] == "NetworkError"
        assert content_json["error_info"]["retry_count"] == 4  # Default max_retries

    @patch("carpenter.agent.invocation.claude_client")
    def test_chat_error_with_partial_response(self, mock_client):
        """Test that chat() handles error after partial response collection."""
        # First call succeeds, second call fails
        mock_client.call.side_effect = [
            _mock_api_response("Partial response"),
            Exception("Network error"),
        ]

        db = get_db()
        db.execute("DELETE FROM messages")
        db.execute("DELETE FROM conversations")
        db.commit()

        result = invocation.invoke_for_chat("Test message")

        # Should return the partial response collected before error
        assert "Partial response" in result["response_text"]

        # Should NOT save an error message (only save error if no text collected)
        messages = db.execute(
            "SELECT role FROM messages WHERE conversation_id = ?",
            (result["conversation_id"],)
        ).fetchall()

        system_messages = [m for m in messages if m[0] == "system"]
        assert len(system_messages) == 0  # No error message saved

    @patch("carpenter.agent.invocation.time")
    @patch("carpenter.agent.invocation.claude_client")
    def test_error_info_includes_model_and_provider(self, mock_client, mock_time):
        """Test that ErrorInfo includes model and provider information."""
        mock_client.call.side_effect = Exception("Generic error")
        mock_time.sleep = MagicMock()

        result = invocation._call_with_retries(
            "system", [],
            api_key="key",
            max_retries=2,
            model="claude-3-5-sonnet-20241022",
        )

        assert "_error" in result
        error_info = result["_error"]
        assert error_info.model == "claude-3-5-sonnet-20241022"
        assert error_info.provider == "anthropic"

    @patch("carpenter.agent.invocation.claude_client")
    def test_error_message_backward_compatible(self, mock_client):
        """Test that old code checking 'if response is None' still works."""
        mock_client.call.side_effect = Exception("Error")

        db = get_db()
        db.execute("DELETE FROM messages")
        db.execute("DELETE FROM conversations")
        db.commit()

        result = invocation.invoke_for_chat("Test")

        # Should still return a valid result dict with error text
        assert result is not None
        assert "conversation_id" in result
        assert "response_text" in result
        assert result["response_text"]  # Should have error message
