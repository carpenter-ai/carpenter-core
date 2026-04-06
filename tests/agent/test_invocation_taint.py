"""Tests for taint isolation in carpenter.agent.invocation.

Moved from test_invocation.py — covers taint leak prevention in submit_code
and get_execution_output.
"""

import json
import os
from unittest.mock import patch

import pytest

from carpenter.agent import invocation, conversation
from carpenter.chat_tool_loader import get_handler
from carpenter.db import get_db


class TestTaintIsolation:
    """Tests for taint leak prevention in submit_code and get_execution_output.

    Verifies that:
    - Web/network imports from chat context are BLOCKED before execution
    - Clean code still returns inline output
    - get_execution_output withholds output for tainted code
    """

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_submit_code_tainted_blocked(self, mock_review):
        """submit_code with web import is BLOCKED from chat context."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="",
        )
        conv_id = conversation.create_conversation()
        tainted_code = (
            'from carpenter_tools.act.web import get\n'
            'print("SENSITIVE_WEB_CONTENT_LEAKED")\n'
        )
        result = invocation._execute_chat_tool(
            "submit_code",
            {"code": tainted_code, "description": "web fetch"},
            conversation_id=conv_id,
        )
        # The raw output must NOT appear in the result
        assert "SENSITIVE_WEB_CONTENT_LEAKED" not in result
        # Result is a BLOCKED message
        assert "BLOCKED" in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_submit_code_clean_returns_full_output(self, mock_review):
        """submit_code with clean code returns the full execution output."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="",
        )
        conv_id = conversation.create_conversation()
        clean_code = 'print("CLEAN_OUTPUT_VISIBLE")\n'
        result = invocation._execute_chat_tool(
            "submit_code",
            {"code": clean_code, "description": "clean script"},
            conversation_id=conv_id,
        )
        # Clean output should be visible
        assert "CLEAN_OUTPUT_VISIBLE" in result
        # Should NOT contain taint warnings
        assert "untrusted data" not in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_submit_code_tainted_without_conversation_id(self, mock_review):
        """submit_code with tainted code but no conversation_id is also BLOCKED.

        The pre-execution taint check blocks web imports from chat context
        regardless of conversation_id.
        """
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="",
        )
        tainted_code = (
            'from carpenter_tools.act.web import get\n'
            'print("NO_CONV_OUTPUT")\n'
        )
        result = invocation._execute_chat_tool(
            "submit_code",
            {"code": tainted_code, "description": "no conv"},
            # No conversation_id
        )
        # Web imports are blocked from chat context regardless of conversation_id
        assert "BLOCKED" in result
        assert "NO_CONV_OUTPUT" not in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_submit_code_tainted_records_taint(self, mock_review):
        """submit_code with web import blocks execution and returns BLOCKED.

        The taint pre-check prevents execution entirely, so no taint is
        recorded in the database. The BLOCKED message guides the user to
        create an untrusted arc instead.
        """
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="",
        )
        conv_id = conversation.create_conversation()
        tainted_code = (
            'from carpenter_tools.act.web import get\n'
            'print("tainted")\n'
        )
        result = invocation._execute_chat_tool(
            "submit_code",
            {"code": tainted_code, "description": "taint test"},
            conversation_id=conv_id,
        )
        # Verify the code was BLOCKED (not executed)
        assert "BLOCKED" in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_get_execution_output_blocks_tainted(self, mock_review):
        """get_execution_output withholds output for tainted code."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="",
        )
        from carpenter.core import code_manager
        # Save and execute tainted code that produces output
        tainted_code = (
            'from carpenter_tools.act.web import get\n'
            'print("SECRET_WEB_DATA")\n'
        )
        save_result = code_manager.save_code(tainted_code, source="test", name="tainted_exec")
        exec_result = code_manager.execute(save_result["code_file_id"])

        # Now try to read via get_execution_output
        result = get_handler("get_execution_output")(
            {"execution_id": exec_result["execution_id"]}
        )
        # Raw output must NOT be returned
        assert "SECRET_WEB_DATA" not in result
        # Should indicate output was withheld
        assert "withheld" in result.lower()
        assert "untrusted" in result.lower()
        assert "carpenter_tools.act.web" in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_get_execution_output_allows_clean(self, mock_review):
        """get_execution_output returns full output for clean code."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="",
        )
        from carpenter.core import code_manager
        clean_code = 'print("CLEAN_DATA_OK")\n'
        save_result = code_manager.save_code(clean_code, source="test", name="clean_exec")
        exec_result = code_manager.execute(save_result["code_file_id"])

        result = get_handler("get_execution_output")(
            {"execution_id": exec_result["execution_id"]}
        )
        # Clean output should be visible
        assert "CLEAN_DATA_OK" in result
        # Should NOT have taint warnings
        assert "withheld" not in result.lower()

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_submit_code_tainted_blocked_with_output_bytes(self, mock_review):
        """submit_code with web import is BLOCKED, not executed."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="",
        )
        conv_id = conversation.create_conversation()
        tainted_code = (
            'from carpenter_tools.act.web import get\n'
            'print("twelve chars")\n'
        )
        result = invocation._execute_chat_tool(
            "submit_code",
            {"code": tainted_code, "description": "bytes test"},
            conversation_id=conv_id,
        )
        # Code is blocked before execution
        assert "BLOCKED" in result
        assert "twelve chars" not in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_submit_code_tainted_blocked_no_state(self, mock_review):
        """submit_code with web import is BLOCKED, nothing stored in arc state."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="",
        )
        conv_id = conversation.create_conversation()
        tainted_code = (
            'from carpenter_tools.act.web import get\n'
            'print("stored output")\n'
        )
        result = invocation._execute_chat_tool(
            "submit_code",
            {"code": tainted_code, "description": "log path test"},
            conversation_id=conv_id,
        )
        # Code is blocked, not executed
        assert "BLOCKED" in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_submit_code_tainted_blocked_no_output(self, mock_review):
        """submit_code with web import is BLOCKED, raw output never appears."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="",
        )
        conv_id = conversation.create_conversation()
        tainted_code = (
            'from carpenter_tools.act.web import get\n'
            'print("STORED_OUTPUT_CONTENT")\n'
        )
        result = invocation._execute_chat_tool(
            "submit_code",
            {"code": tainted_code, "description": "stored access"},
            conversation_id=conv_id,
        )
        # Result does NOT contain raw output
        assert "STORED_OUTPUT_CONTENT" not in result
        assert "BLOCKED" in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    @patch("carpenter.security.trust.check_code_for_taint")
    def test_submit_code_fail_closed_on_taint_check_error(
        self, mock_taint_check, mock_review,
    ):
        """submit_code fails closed when check_code_for_taint raises an exception.

        If the taint detection mechanism itself fails, output must be withheld
        rather than leaked.  The result is JSON metadata (same as tainted).
        """
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="",
        )
        # Make taint check raise an exception
        mock_taint_check.side_effect = RuntimeError("AST parse exploded")

        conv_id = conversation.create_conversation()
        code = 'print("SHOULD_NOT_LEAK_ON_ERROR")\n'
        result = invocation._execute_chat_tool(
            "submit_code",
            {"code": code, "description": "fail closed test"},
            conversation_id=conv_id,
        )
        # Raw output must NOT appear (fail-closed)
        assert "SHOULD_NOT_LEAK_ON_ERROR" not in result
        # Result is JSON metadata (fail-closed treats as tainted)
        metadata = json.loads(result)
        assert metadata["status"] == "executed"
        assert "output_key" in metadata

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_get_execution_output_fail_closed_on_missing_code_file(self, mock_review):
        """get_execution_output fails closed when the code file is missing.

        If the code file has been deleted or is unreadable, the taint check
        cannot determine safety, so output must be withheld.
        """
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="",
        )
        from carpenter.core import code_manager
        # Save and execute clean code that produces output
        code = 'print("HIDDEN_WHEN_FILE_GONE")\n'
        save_result = code_manager.save_code(code, source="test", name="vanishing")
        exec_result = code_manager.execute(save_result["code_file_id"])

        # Delete the code file so taint check cannot read it
        os.remove(save_result["file_path"])

        result = get_handler("get_execution_output")(
            {"execution_id": exec_result["execution_id"]}
        )
        # Output must be withheld (fail-closed)
        assert "HIDDEN_WHEN_FILE_GONE" not in result
        assert "withheld" in result.lower()
        assert "could not verify taint status" in result.lower()

    @patch("carpenter.review.pipeline.review_code_for_intent")
    @patch("carpenter.security.trust.check_code_for_taint")
    def test_get_execution_output_fail_closed_on_taint_check_error(
        self, mock_taint_check, mock_review,
    ):
        """get_execution_output fails closed when check_code_for_taint raises."""
        from carpenter.review.code_reviewer import ReviewResult
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="",
        )
        from carpenter.core import code_manager
        code = 'print("HIDDEN_ON_TAINT_ERROR")\n'
        save_result = code_manager.save_code(code, source="test", name="taint_err")
        exec_result = code_manager.execute(save_result["code_file_id"])

        # Make taint check raise
        mock_taint_check.side_effect = ValueError("something broke")

        result = get_handler("get_execution_output")(
            {"execution_id": exec_result["execution_id"]}
        )
        # Output must be withheld (fail-closed)
        assert "HIDDEN_ON_TAINT_ERROR" not in result
        assert "withheld" in result.lower()
