"""Tests for tool persistence and execution in carpenter.agent.invocation.

Moved from test_invocation.py — covers tool call persistence, API call metrics,
_save_tool_calls helper, and submit_code chat tool.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from carpenter.agent import invocation, conversation
from carpenter.db import get_db
from tests.agent.conftest import _mock_api_response


class TestToolCallPersistence:
    """Tests for tool call persistence in invoke_for_chat."""

    @patch("carpenter.agent.invocation.claude_client")
    def test_tool_use_persists_messages(self, mock_client):
        """Tool use creates assistant + tool_result + final assistant messages."""
        # First call: tool_use response
        tool_response = {
            "content": [
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "tu_1", "name": "get_state", "input": {"key": "foo"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        # Second call: final text response
        final_response = {
            "content": [{"type": "text", "text": "The value is bar."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 200, "output_tokens": 30},
        }
        mock_client.call.side_effect = [tool_response, final_response]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("What is foo?", api_key="test-key")

        messages = conversation.get_messages(result["conversation_id"])
        # user + assistant(tool_use) + tool_result + assistant(final)
        assert len(messages) == 4
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        # Tool use message contains the text content, tool details in content_json
        assert messages[1]["content"] == "Let me check."
        assert messages[1]["content_json"] is not None
        blocks = json.loads(messages[1]["content_json"])
        assert any(b.get("type") == "tool_use" and b.get("name") == "get_state" for b in blocks)
        assert messages[2]["role"] == "tool_result"
        assert messages[2]["content_json"] is not None
        assert messages[3]["role"] == "assistant"
        assert messages[3]["content"] == "The value is bar."

    @patch("carpenter.agent.invocation.claude_client")
    def test_tool_calls_table_populated(self, mock_client):
        """Tool use creates entries in the tool_calls table."""
        tool_response = {
            "content": [
                {"type": "tool_use", "id": "tu_abc", "name": "read_file", "input": {"path": "/tmp/x"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        final_response = {
            "content": [{"type": "text", "text": "Done."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 200, "output_tokens": 30},
        }
        mock_client.call.side_effect = [tool_response, final_response]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("Read file", api_key="test-key")

        db = get_db()
        try:
            rows = db.execute(
                "SELECT * FROM tool_calls WHERE conversation_id = ?",
                (result["conversation_id"],),
            ).fetchall()
        finally:
            db.close()

        assert len(rows) == 1
        row = dict(rows[0])
        assert row["tool_use_id"] == "tu_abc"
        assert row["tool_name"] == "read_file"
        assert json.loads(row["input_json"]) == {"path": "/tmp/x"}
        assert row["result_text"] is not None
        assert row["duration_ms"] is not None
        assert row["duration_ms"] >= 0

    @patch("carpenter.agent.invocation.claude_client")
    def test_reload_conversation_has_structured_content(self, mock_client):
        """After tool use, reloaded conversation has structured messages for API."""
        tool_response = {
            "content": [
                {"type": "tool_use", "id": "tu_2", "name": "get_state", "input": {"key": "x"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        final_response = {
            "content": [{"type": "text", "text": "Got it."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 200, "output_tokens": 30},
        }
        mock_client.call.side_effect = [tool_response, final_response]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("Check x", api_key="test-key")

        # Reload and format for API
        messages = conversation.get_messages(result["conversation_id"])
        api_msgs = conversation.format_messages_for_api(messages)

        # Should have: user, assistant(structured), user(tool_result structured), assistant(plain)
        assert len(api_msgs) == 4
        assert api_msgs[0]["role"] == "user"
        assert api_msgs[1]["role"] == "assistant"
        assert isinstance(api_msgs[1]["content"], list)  # structured
        assert api_msgs[2]["role"] == "user"  # tool_result mapped to user
        assert isinstance(api_msgs[2]["content"], list)  # structured
        assert api_msgs[3]["role"] == "assistant"
        assert api_msgs[3]["content"] == "Got it."

    @patch("carpenter.agent.invocation.claude_client")
    @patch("carpenter.agent.invocation.config")
    @patch("carpenter.agent.invocation._build_chat_system_prompt", return_value="You are Carpenter.")
    @patch("carpenter.agent.invocation._select_chat_tools", return_value=[])
    def test_forced_final_response_on_iteration_limit(self, mock_tools, mock_prompt, mock_config, mock_client):
        """When tool loop exits mid-tool-use, a final response is forced."""
        # Set iteration limit to 2 so we hit it quickly
        mock_config.CONFIG = {
            "chat_tool_iterations": 2,
            "mechanical_retry_max": 1,
        }

        # First call: tool_use
        tool_response_1 = {
            "content": [
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "/tmp/x"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

        # Second call: tool_use again (hits iteration limit)
        tool_response_2 = {
            "content": [
                {"type": "text", "text": "Now checking another."},
                {"type": "tool_use", "id": "tu_2", "name": "read_file", "input": {"path": "/tmp/y"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 120, "output_tokens": 55},
        }

        # Third call: forced final response (tools=None)
        forced_final_response = {
            "content": [{"type": "text", "text": "I found the files and they contain data."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 150, "output_tokens": 20},
        }

        mock_client.call.side_effect = [tool_response_1, tool_response_2, forced_final_response]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("Check files", api_key="test-key")

        # Should have a response despite hitting iteration limit
        assert result["response_text"] is not None
        assert "I found the files" in result["response_text"]

        # Check that the final call was made with tools=None
        assert mock_client.call.call_count == 3
        final_call_kwargs = mock_client.call.call_args_list[2][1]
        assert final_call_kwargs.get("tools") is None

        # Messages should include the forced final response
        messages = conversation.get_messages(result["conversation_id"])
        # user + assistant(tool1) + tool_result + assistant(tool2) + tool_result + assistant(final)
        assert len(messages) == 6
        assert messages[-1]["role"] == "assistant"
        assert "I found the files" in messages[-1]["content"]

    @patch("carpenter.agent.invocation.claude_client")
    @patch("carpenter.agent.invocation.config")
    @patch("carpenter.agent.invocation._build_chat_system_prompt", return_value="You are Carpenter.")
    @patch("carpenter.agent.invocation._select_chat_tools", return_value=[])
    def test_forced_final_response_when_no_text_collected(self, mock_tools, mock_prompt, mock_config, mock_client):
        """When agent uses tools but never provides text, final response is forced."""
        # Set iteration limit to 1 to trigger forced response immediately
        mock_config.CONFIG = {
            "chat_tool_iterations": 1,
            "mechanical_retry_max": 1,
        }

        # Tool use with no accompanying text
        tool_response = {
            "content": [
                {"type": "tool_use", "id": "tu_1", "name": "get_state", "input": {"key": "x"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }

        # Forced final response
        forced_final_response = {
            "content": [{"type": "text", "text": "The value is 42."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 120, "output_tokens": 10},
        }

        mock_client.call.side_effect = [tool_response, forced_final_response]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("What is x?", api_key="test-key")

        # Should have a response
        assert result["response_text"] == "The value is 42."

        # Final call should have tools=None
        assert mock_client.call.call_count == 2
        final_call_kwargs = mock_client.call.call_args_list[1][1]
        assert final_call_kwargs.get("tools") is None


class TestAsyncToolShortCircuit:
    """Tests for the async tool short-circuit in invoke_for_chat.

    When ALL tools in a turn are async (e.g. fetch_web_content) and the
    model already produced visible text alongside the tool call, the
    post-tool API call should be skipped to avoid a redundant
    acknowledgment message.
    """

    @patch("carpenter.agent.invocation._handle_fetch_web_content")
    @patch("carpenter.agent.invocation.claude_client")
    def test_async_tool_with_text_skips_post_tool_call(self, mock_client, mock_fetch):
        """fetch_web_content with visible text -> no extra API call."""
        mock_fetch.return_value = (
            "Web fetch started (arc #99). The content will be fetched, "
            "reviewed, and the result will be delivered to this conversation "
            "automatically. Do NOT poll or check arc status — just tell the "
            "user the result is on its way and stop."
        )

        # Single API call: model says text + calls fetch_web_content
        tool_response = {
            "content": [
                {"type": "text", "text": "I'll fetch the weather for you."},
                {
                    "type": "tool_use",
                    "id": "tu_fw",
                    "name": "fetch_web_content",
                    "input": {"url": "https://wttr.in/Oxford", "goal": "weather"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        mock_client.call.side_effect = [tool_response]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("What's the weather?", api_key="k")

        # Only 1 API call — the post-tool call was skipped
        assert mock_client.call.call_count == 1

        # Response text is the acknowledgment from the tool_use turn
        assert "I'll fetch the weather" in result["response_text"]

        messages = conversation.get_messages(result["conversation_id"])
        # user + assistant(tool_use with text) + tool_result = 3 messages
        # No extra assistant message after tool_result
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert "I'll fetch the weather" in assistant_msgs[0]["content"]

    @patch("carpenter.agent.invocation._handle_fetch_web_content")
    @patch("carpenter.agent.invocation.claude_client")
    def test_async_tool_without_text_allows_post_tool_call(self, mock_client, mock_fetch):
        """fetch_web_content without visible text -> post-tool API call proceeds."""
        mock_fetch.return_value = (
            "Web fetch started (arc #99). Do NOT poll or check arc status."
        )

        # First call: tool_use with no visible text
        tool_response = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_fw",
                    "name": "fetch_web_content",
                    "input": {"url": "https://wttr.in/Oxford", "goal": "weather"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        # Second call: model generates acknowledgment
        ack_response = {
            "content": [{"type": "text", "text": "Fetching the weather now."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 200, "output_tokens": 20},
        }
        mock_client.call.side_effect = [tool_response, ack_response]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("Weather?", api_key="k")

        # 2 API calls — post-tool call was allowed because no visible text
        assert mock_client.call.call_count == 2
        assert "Fetching the weather" in result["response_text"]

    @patch("carpenter.agent.invocation.claude_client")
    def test_non_async_tool_with_text_still_makes_post_tool_call(self, mock_client):
        """Regular (non-async) tools always proceed with the post-tool API call."""
        tool_response = {
            "content": [
                {"type": "text", "text": "Let me check."},
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "get_state",
                    "input": {"key": "foo"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        final_response = {
            "content": [{"type": "text", "text": "The value is bar."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 200, "output_tokens": 30},
        }
        mock_client.call.side_effect = [tool_response, final_response]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("What is foo?", api_key="k")

        # 2 API calls — regular tools are not short-circuited
        assert mock_client.call.call_count == 2
        assert "The value is bar" in result["response_text"]


class TestApiCallPersistence:
    """Tests for API call metrics being persisted in invoke_for_chat."""

    @patch("carpenter.agent.invocation.claude_client")
    def test_api_call_saved_on_simple_response(self, mock_client):
        """A simple (no tool_use) chat response persists API call metrics."""
        mock_client.call.return_value = {
            "content": [{"type": "text", "text": "Hello!"}],
            "stop_reason": "end_turn",
            "model": "claude-haiku-4-5-20251001",
            "usage": {
                "input_tokens": 500,
                "output_tokens": 25,
                "cache_creation_input_tokens": 300,
                "cache_read_input_tokens": 100,
            },
        }
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("Hi", api_key="test-key")

        db = get_db()
        try:
            rows = db.execute(
                "SELECT * FROM api_calls WHERE conversation_id = ?",
                (result["conversation_id"],),
            ).fetchall()
        finally:
            db.close()

        assert len(rows) == 1
        row = dict(rows[0])
        assert row["model"] == "claude-haiku-4-5-20251001"
        assert row["input_tokens"] == 500
        assert row["output_tokens"] == 25
        assert row["cache_creation_input_tokens"] == 300
        assert row["cache_read_input_tokens"] == 100
        assert row["stop_reason"] == "end_turn"

    @patch("carpenter.agent.invocation.claude_client")
    def test_api_call_saved_per_turn(self, mock_client):
        """Tool_use loop saves API call metrics for each turn."""
        tool_response = {
            "content": [
                {"type": "tool_use", "id": "tu_1", "name": "get_state", "input": {"key": "x"}},
            ],
            "stop_reason": "tool_use",
            "model": "haiku",
            "usage": {"input_tokens": 400, "output_tokens": 30,
                      "cache_creation_input_tokens": 200, "cache_read_input_tokens": 0},
        }
        final_response = {
            "content": [{"type": "text", "text": "Done."}],
            "stop_reason": "end_turn",
            "model": "haiku",
            "usage": {"input_tokens": 600, "output_tokens": 20,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 200},
        }
        mock_client.call.side_effect = [tool_response, final_response]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("Check x", api_key="test-key")

        db = get_db()
        try:
            rows = db.execute(
                "SELECT * FROM api_calls WHERE conversation_id = ? ORDER BY id ASC",
                (result["conversation_id"],),
            ).fetchall()
        finally:
            db.close()

        assert len(rows) == 2
        assert rows[0]["stop_reason"] == "tool_use"
        assert rows[0]["cache_creation_input_tokens"] == 200
        assert rows[1]["stop_reason"] == "end_turn"
        assert rows[1]["cache_read_input_tokens"] == 200


class TestSaveToolCalls:
    """Tests for _save_tool_calls helper."""

    def test_saves_tool_call_records(self):
        """_save_tool_calls inserts records into tool_calls table."""
        conv_id = conversation.get_or_create_conversation()
        msg_id = conversation.add_message(conv_id, "assistant", "test")

        blocks = [
            {"type": "text", "text": "thinking..."},
            {"type": "tool_use", "id": "tu_a", "name": "read_file", "input": {"path": "/a"}},
            {"type": "tool_use", "id": "tu_b", "name": "write_file", "input": {"path": "/b", "content": "hi"}},
        ]
        results = {"tu_a": "file content", "tu_b": "File written successfully."}
        timings = {"tu_a": 15, "tu_b": 42}

        invocation._save_tool_calls(conv_id, msg_id, blocks, results, timings)

        db = get_db()
        try:
            rows = db.execute(
                "SELECT * FROM tool_calls WHERE conversation_id = ? ORDER BY id",
                (conv_id,),
            ).fetchall()
        finally:
            db.close()

        assert len(rows) == 2
        assert rows[0]["tool_name"] == "read_file"
        assert rows[0]["duration_ms"] == 15
        assert rows[0]["result_text"] == "file content"
        assert rows[1]["tool_name"] == "write_file"
        assert rows[1]["duration_ms"] == 42

    def test_skips_non_tool_use_blocks(self):
        """_save_tool_calls ignores text blocks."""
        conv_id = conversation.get_or_create_conversation()
        msg_id = conversation.add_message(conv_id, "assistant", "test")

        blocks = [{"type": "text", "text": "just text"}]
        invocation._save_tool_calls(conv_id, msg_id, blocks, {}, {})

        db = get_db()
        try:
            count = db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        finally:
            db.close()
        assert count == 0


class TestSubmitCodeTool:
    """Tests for the submit_code chat tool (security review pipeline)."""

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_submit_code_approved(self, mock_review):
        """submit_code executes approved code and returns output."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="",
        )
        result = invocation._execute_chat_tool(
            "submit_code",
            {"code": 'print("hello from submit")', "description": "test script"},
        )
        assert "hello from submit" in result
        assert "[failed]" not in result.lower()

    def test_submit_code_syntax_error(self):
        """submit_code returns syntax error for invalid code."""
        result = invocation._execute_chat_tool(
            "submit_code",
            {"code": "def f(\n", "description": "bad code"},
        )
        assert "Syntax error" in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_submit_code_major_alert(self, mock_review):
        """submit_code rejects code on major alert."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="major", reason="Code does not match intent",
            sanitized_code="",
        )
        result = invocation._execute_chat_tool(
            "submit_code",
            {"code": 'print("suspicious")', "description": "test"},
        )
        assert "REJECTED" in result
        assert "Code does not match intent" in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_submit_code_saves_to_code_files(self, mock_review):
        """submit_code creates a code_files record on approval."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="",
        )
        invocation._execute_chat_tool(
            "submit_code",
            {"code": 'print("audit")', "description": "audit test"},
        )
        db = get_db()
        try:
            rows = db.execute(
                "SELECT * FROM code_files WHERE source = 'chat_agent'"
            ).fetchall()
        finally:
            db.close()
        assert len(rows) >= 1
        assert "audit_test" in rows[-1]["file_path"]
