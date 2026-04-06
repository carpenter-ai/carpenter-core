"""Tests for the submit_code taint leak fix.

Verifies that:
1. Web/network imports from chat context are BLOCKED before execution
2. Non-tainted executions still return inline output
3. get_execution_output uses persisted taint_source for fast lookup
4. Fail-closed behavior is preserved when taint detection errors
5. Non-execution statuses (syntax_error, major_alert) are unaffected
"""

import json
from unittest.mock import patch

import pytest

from carpenter.agent import invocation, conversation
from carpenter.agent.invocation import _execute_chat_tool
from carpenter.chat_tool_loader import get_handler
from carpenter.review.code_reviewer import ReviewResult
from carpenter.review.pipeline import clear_cache
from carpenter.db import get_db
from carpenter.tool_backends import state as state_backend


def _approve_review():
    """Return a mock ReviewResult that approves the code."""
    return ReviewResult(status="approve", reason="", sanitized_code="")


# ---------------------------------------------------------------------------
# Tainted submit_code is BLOCKED from chat context
# ---------------------------------------------------------------------------


class TestTaintedSubmitCodeBlocked:
    """Web/network imports from chat context are blocked before execution."""

    def setup_method(self):
        clear_cache()

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_web_import_blocked(self, mock_review):
        """Code importing carpenter_tools.act.web is blocked from chat context."""
        mock_review.return_value = _approve_review()
        conv_id = conversation.create_conversation()
        code = (
            'from carpenter_tools.act.web import get\n'
            'print("hello from web")\n'
        )
        result = _execute_chat_tool(
            "submit_code",
            {"code": code, "description": "web fetch"},
            conversation_id=conv_id,
        )
        assert "BLOCKED" in result
        assert "hello from web" not in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_blocked_mentions_untrusted_arc(self, mock_review):
        """Blocked message guides user to create an untrusted arc batch."""
        mock_review.return_value = _approve_review()
        conv_id = conversation.create_conversation()
        code = 'from carpenter_tools.act.web import get\nprint("x")\n'
        result = _execute_chat_tool(
            "submit_code",
            {"code": code, "description": "test"},
            conversation_id=conv_id,
        )
        assert "untrusted" in result.lower()
        assert "arc" in result.lower()

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_raw_output_not_in_result(self, mock_review):
        """Raw execution output must never appear in the blocked result."""
        mock_review.return_value = _approve_review()
        conv_id = conversation.create_conversation()
        code = (
            'from carpenter_tools.act.web import get\n'
            'print("SENSITIVE_WEB_RESPONSE_DATA_12345")\n'
        )
        result = _execute_chat_tool(
            "submit_code",
            {"code": code, "description": "fetch"},
            conversation_id=conv_id,
        )
        assert "SENSITIVE_WEB_RESPONSE_DATA_12345" not in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_error_code_with_web_import_blocked(self, mock_review):
        """Code with web import and error is still blocked."""
        mock_review.return_value = _approve_review()
        conv_id = conversation.create_conversation()
        code = (
            'from carpenter_tools.act.web import get\n'
            'raise ValueError("bad input")\n'
        )
        result = _execute_chat_tool(
            "submit_code",
            {"code": code, "description": "error test"},
            conversation_id=conv_id,
        )
        assert "BLOCKED" in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_web_import_without_print_blocked(self, mock_review):
        """Web import code without print is still blocked."""
        mock_review.return_value = _approve_review()
        conv_id = conversation.create_conversation()
        code = (
            'from carpenter_tools.act.web import get\n'
            'x = 1 + 1\n'
        )
        result = _execute_chat_tool(
            "submit_code",
            {"code": code, "description": "simple"},
            conversation_id=conv_id,
        )
        assert "BLOCKED" in result


# ---------------------------------------------------------------------------
# taint_source persisted on code_executions
# ---------------------------------------------------------------------------


class TestTaintSourcePersisted:
    """taint detection works for tainted code via get_execution_output."""

    def setup_method(self):
        clear_cache()

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_taint_detected_at_query_time(self, mock_review):
        """get_execution_output detects taint by re-parsing the code file.

        Even when taint_source is not persisted (e.g. direct code_manager execution),
        the get_execution_output path re-parses the code to detect taint.
        """
        from carpenter.core import code_manager
        mock_review.return_value = _approve_review()
        code = (
            'from carpenter_tools.act.web import get\n'
            'print("taint persist test")\n'
        )
        save_result = code_manager.save_code(code, source="test", name="tainted")
        exec_result = code_manager.execute(save_result["code_file_id"])

        # get_execution_output should detect taint via code file re-parse
        output_result = get_handler("get_execution_output")(
            {"execution_id": exec_result["execution_id"]}
        )
        assert "taint persist test" not in output_result
        assert "withheld" in output_result.lower()

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_clean_execution_has_no_taint_source(self, mock_review):
        """Clean (non-tainted) executions have NULL taint_source."""
        mock_review.return_value = _approve_review()
        code = 'print("clean")\n'
        result = _execute_chat_tool(
            "submit_code",
            {"code": code, "description": "clean test"},
        )
        # Clean execution returns inline output, not JSON
        assert "clean" in result

        # Check that the most recent execution has no taint_source
        db = get_db()
        try:
            row = db.execute(
                "SELECT taint_source FROM code_executions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            db.close()
        assert row["taint_source"] is None


# ---------------------------------------------------------------------------
# Non-tainted executions still return inline output
# ---------------------------------------------------------------------------


class TestCleanExecutionUnchanged:
    """Non-tainted code still gets inline output in the return value."""

    def setup_method(self):
        clear_cache()

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_clean_code_returns_inline_output(self, mock_review):
        """Clean code execution returns output inline (not JSON metadata)."""
        mock_review.return_value = _approve_review()
        code = 'print("visible_output_789")\n'
        result = _execute_chat_tool(
            "submit_code",
            {"code": code, "description": "clean"},
        )
        assert "visible_output_789" in result
        # Should NOT be JSON metadata
        with pytest.raises(json.JSONDecodeError):
            parsed = json.loads(result)
            # If it does parse, it shouldn't have our metadata fields
            assert "output_key" not in parsed


# ---------------------------------------------------------------------------
# get_execution_output uses persisted taint_source
# ---------------------------------------------------------------------------


class TestGetExecutionOutputTaintColumn:
    """get_execution_output uses the persisted taint_source for fast lookup."""

    def setup_method(self):
        clear_cache()

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_withholds_output_for_tainted_execution(self, mock_review):
        """get_execution_output withholds output when taint_source is set.

        Uses code_manager.execute() directly to bypass the chat-context BLOCKED
        pre-check, since arc executors bypass the pre-check.
        """
        from carpenter.core import code_manager
        mock_review.return_value = _approve_review()
        code = (
            'from carpenter_tools.act.web import get\n'
            'print("SECRET_DATA_FOR_OUTPUT_TEST")\n'
        )
        save_result = code_manager.save_code(code, source="test", name="tainted")
        exec_result = code_manager.execute(save_result["code_file_id"])

        output_result = get_handler("get_execution_output")(
            {"execution_id": exec_result["execution_id"]}
        )
        assert "SECRET_DATA_FOR_OUTPUT_TEST" not in output_result
        assert "withheld" in output_result.lower()

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_shows_output_for_clean_execution(self, mock_review):
        """get_execution_output shows output for clean executions."""
        mock_review.return_value = _approve_review()
        code = 'print("CLEAN_OUTPUT_VISIBLE")\n'
        result = _execute_chat_tool(
            "submit_code",
            {"code": code, "description": "clean"},
        )
        # Get the execution ID from the DB
        db = get_db()
        try:
            row = db.execute(
                "SELECT id FROM code_executions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            db.close()

        output_result = get_handler("get_execution_output")(
            {"execution_id": row["id"]}
        )
        assert "CLEAN_OUTPUT_VISIBLE" in output_result


# ---------------------------------------------------------------------------
# Fail-closed behavior preserved
# ---------------------------------------------------------------------------


class TestFailClosedBehavior:
    """Taint detection failure results in metadata-only return (fail-closed)."""

    def setup_method(self):
        clear_cache()

    @patch("carpenter.review.pipeline.review_code_for_intent")
    @patch("carpenter.security.trust.check_code_for_taint", side_effect=Exception("taint check crashed"))
    def test_taint_check_error_returns_metadata(self, mock_taint, mock_review):
        """When taint detection fails, treat as tainted (fail-closed)."""
        mock_review.return_value = _approve_review()
        conv_id = conversation.create_conversation()
        code = 'print("should be withheld on error")\n'
        result = _execute_chat_tool(
            "submit_code",
            {"code": code, "description": "error test"},
            conversation_id=conv_id,
        )
        # Should be JSON metadata because fail-closed treats as tainted
        metadata = json.loads(result)
        assert metadata["status"] == "executed"
        assert "output_key" in metadata
        assert "should be withheld on error" not in result


# ---------------------------------------------------------------------------
# Non-execution statuses unaffected
# ---------------------------------------------------------------------------


class TestNonExecutionStatusesUnchanged:
    """Syntax errors, major alerts, rejected code return unchanged strings."""

    def test_syntax_error_returns_plain_string(self):
        """Syntax errors return a plain error string, not JSON metadata."""
        result = _execute_chat_tool(
            "submit_code",
            {"code": "def f(\n  invalid", "description": "bad"},
        )
        assert "Syntax error" in result
        # Must NOT be JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(result)

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_major_alert_returns_plain_string(self, mock_review):
        """Major alert returns a plain rejection string, not JSON metadata."""
        mock_review.return_value = ReviewResult(
            status="major", reason="Dangerous operation", sanitized_code="",
        )
        result = _execute_chat_tool(
            "submit_code",
            {"code": 'import os; os.remove("/")', "description": "bad"},
        )
        assert "REJECTED" in result
        assert "Dangerous operation" in result
        # Must NOT be JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(result)


# ---------------------------------------------------------------------------
# Network module taint detection
# ---------------------------------------------------------------------------


class TestNetworkModuleTaint:
    """Code using network modules (httpx, requests, etc.) is blocked from chat."""

    def setup_method(self):
        clear_cache()

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_httpx_import_blocked(self, mock_review):
        """Code importing httpx is blocked from chat context."""
        mock_review.return_value = _approve_review()
        conv_id = conversation.create_conversation()
        code = 'import httpx\nprint("httpx_output")\n'
        result = _execute_chat_tool(
            "submit_code",
            {"code": code, "description": "httpx test"},
            conversation_id=conv_id,
        )
        assert "httpx_output" not in result
        assert "BLOCKED" in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_requests_import_blocked(self, mock_review):
        """Code importing requests is blocked from chat context."""
        mock_review.return_value = _approve_review()
        conv_id = conversation.create_conversation()
        code = 'import requests\nprint("requests_output")\n'
        result = _execute_chat_tool(
            "submit_code",
            {"code": code, "description": "requests test"},
            conversation_id=conv_id,
        )
        assert "requests_output" not in result
        assert "BLOCKED" in result
