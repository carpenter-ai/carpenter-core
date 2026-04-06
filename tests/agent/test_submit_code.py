"""Tests for submit_code tool integration in chat mode (Phase 3 security model)."""

import json
from unittest.mock import patch, MagicMock

import pytest

from carpenter.agent import invocation, conversation
from carpenter.agent.invocation import _execute_chat_tool
from carpenter.review.pipeline import PipelineResult, clear_cache
from carpenter.db import get_db
from carpenter.chat_tool_loader import get_tool_defs_for_api, get_loaded_tools


# --- Tool definition tests ---


class TestToolDefinitionSecurity:
    """Verify that chat tool definitions only contain safe read-only tools
    plus submit_code and a few meta-tools (platform boundary)."""

    # Tools that should NOT exist in chat (action tools removed)
    REMOVED_TOOLS = {
        "write_file", "set_state", "start_coding_change",
        "create_arc", "add_child_arc", "update_arc_status", "cancel_arc",
        "fetch_webpage", "run_python",
        "rename_conversation", "archive_conversation",
        "grant_arc_read_access",
        "request_restart", "change_config",
        "request_credential", "verify_credential", "import_credential_file",
        "subscribe_webhook", "delete_webhook",
        "create_schedule", "cancel_schedule",
    }

    # Tools that SHOULD exist (safe read-only + submit_code + meta)
    EXPECTED_TOOLS = {
        # File tools
        "read_file", "list_files", "file_count",
        # State
        "get_state",
        # Arc tools
        "list_arcs", "get_arc_detail", "list_recent_activity",
        # Introspection
        "list_tool_calls", "list_code_executions", "get_execution_output",
        "list_conversations", "list_api_calls", "get_cache_stats",
        "get_conversation_messages",
        # KB tools
        "kb_describe", "kb_search", "kb_links_in", "get_kb_health",
        # Platform info
        "get_platform_status", "list_config_keys", "list_models", "list_schedules",
        # Utilities
        "reverse_string",
        # Platform tools (injected)
        "submit_code", "escalate_current_arc", "escalate",
    }

    def test_no_action_tools_exposed(self):
        """Action tools must NOT appear in chat tool definitions."""
        tool_names = set(get_loaded_tools().keys())
        for removed in self.REMOVED_TOOLS:
            assert removed not in tool_names, f"{removed} should be removed"

    def test_expected_tools_present(self):
        """All expected safe tools must be present."""
        tool_names = set(get_loaded_tools().keys())
        for expected in self.EXPECTED_TOOLS:
            assert expected in tool_names, f"{expected} should be present"



# --- Submit code handler tests ---


class TestSubmitCodeHandler:
    """Test the submit_code handler in _execute_chat_tool."""

    def setup_method(self):
        clear_cache()

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_approved_code_executes(self, mock_review):
        """Approved code is saved and executed."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="a = 1",
        )
        result = _execute_chat_tool(
            "submit_code",
            {"code": 'print("approved!")', "description": "test"},
        )
        assert "approved!" in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_minor_concern_still_executes(self, mock_review):
        """Minor concern code executes but includes reviewer note."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="minor", reason="Unusual pattern detected",
            sanitized_code="a = 1",
        )
        result = _execute_chat_tool(
            "submit_code",
            {"code": 'print("minor")', "description": "test"},
        )
        assert "minor" in result
        assert "Reviewer note" in result
        assert "Unusual pattern" in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_major_alert_blocks_execution(self, mock_review):
        """Major alert prevents code execution."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="major", reason="Intent mismatch",
            sanitized_code="a = 1",
        )
        result = _execute_chat_tool(
            "submit_code",
            {"code": 'import os; os.remove("/")', "description": "test"},
        )
        assert "REJECTED" in result
        assert "Intent mismatch" in result
        # Should NOT have executed — no code_files record for this
        db = get_db()
        try:
            rows = db.execute(
                "SELECT * FROM code_files WHERE source = 'chat_agent'"
            ).fetchall()
        finally:
            db.close()
        # No record for the rejected code
        for row in rows:
            assert 'import os; os.remove' not in open(row["file_path"]).read()

    def test_syntax_error_caught_early(self):
        """Syntax errors are caught before reaching the reviewer."""
        result = _execute_chat_tool(
            "submit_code",
            {"code": "def f(\n  invalid", "description": "bad code"},
        )
        assert "Syntax error" in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_cached_approval_skips_review(self, mock_review):
        """Identical resubmission skips the reviewer call."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="a = 1",
        )
        code = 'print("cached test")'
        conv_id = conversation.get_or_create_conversation()

        # First call — goes through review
        result1 = _execute_chat_tool(
            "submit_code",
            {"code": code, "description": "first"},
            conversation_id=conv_id,
        )
        assert mock_review.call_count == 1
        assert "cached test" in result1

        # Second call with same code — cached, review skipped
        result2 = _execute_chat_tool(
            "submit_code",
            {"code": code, "description": "second"},
            conversation_id=conv_id,
        )
        # review_code should NOT have been called a second time
        assert mock_review.call_count == 1
        assert "cached test" in result2

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_runtime_error_reported(self, mock_review):
        """Runtime failure in executed code is reported."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="a = 1",
        )
        result = _execute_chat_tool(
            "submit_code",
            {"code": "import sys; sys.exit(1)", "description": "fail"},
        )
        assert "[failed]" in result


# --- Removed tool handler tests ---


class TestRemovedToolHandlers:
    """Verify that removed action tools return 'Unknown tool' errors."""

    @pytest.mark.parametrize("tool_name", [
        "write_file", "set_state", "start_coding_change",
        "create_arc", "add_child_arc", "update_arc_status",
        "cancel_arc", "fetch_webpage", "run_python",
    ])
    def test_removed_tool_returns_unknown(self, tool_name):
        """Removed action tools return 'Unknown tool' error."""
        result = _execute_chat_tool(tool_name, {})
        assert "Unknown tool" in result



# --- Chat integration test ---


class TestSubmitCodeInChatLoop:
    """Test submit_code through the full invoke_for_chat loop."""

    @patch("carpenter.review.pipeline.review_code_for_intent")
    @patch("carpenter.agent.invocation.claude_client")
    def test_chat_submit_code_flow(self, mock_client, mock_review):
        """Full chat loop: agent uses submit_code, review approves, code runs."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="a = 1",
        )

        tool_response = {
            "content": [
                {"type": "tool_use", "id": "tool_1", "name": "submit_code",
                 "input": {"code": 'print("hello")', "description": "greet"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 50, "output_tokens": 30},
        }
        text_response = {
            "content": [{"type": "text", "text": "Code executed successfully."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 80, "output_tokens": 20},
        }

        mock_client.call.side_effect = [tool_response, text_response]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("Run some code", api_key="test-key")

        assert result["response_text"] is not None
        assert mock_client.call.call_count == 2

        # The tool result fed back should contain the execution output
        second_call_msgs = mock_client.call.call_args_list[1][0][1]
        tool_result_msg = second_call_msgs[-1]
        assert tool_result_msg["role"] == "user"

    @patch("carpenter.review.pipeline.review_code_for_intent")
    @patch("carpenter.agent.invocation.claude_client")
    def test_chat_submit_code_rejected(self, mock_client, mock_review):
        """Full chat loop: agent uses submit_code, review rejects."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="major", reason="Suspicious deletion",
            sanitized_code="a = 1",
        )

        tool_response = {
            "content": [
                {"type": "tool_use", "id": "tool_1", "name": "submit_code",
                 "input": {"code": 'import shutil; shutil.rmtree("/")',
                           "description": "cleanup"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 50, "output_tokens": 30},
        }
        text_response = {
            "content": [{"type": "text", "text": "I see the code was rejected."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 80, "output_tokens": 20},
        }

        mock_client.call.side_effect = [tool_response, text_response]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("Delete everything", api_key="test-key")

        # The tool result sent back to Claude should contain rejection
        second_call_msgs = mock_client.call.call_args_list[1][0][1]
        tool_result_content = second_call_msgs[-1]["content"]
        assert any("REJECTED" in str(b) for b in tool_result_content)
