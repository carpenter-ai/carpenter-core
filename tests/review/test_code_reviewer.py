"""Tests for code_reviewer — AI reviewer prompt construction and response parsing."""

from unittest.mock import patch

import pytest

from carpenter.review.code_reviewer import (
    review_code,
    extract_conversation_text,
    get_reviewer_model,
    _parse_review_response,
    _extract_verdict_from_tool_call,
    ReviewResult,
)
from carpenter import config


# --- extract_conversation_text ---


class TestExtractConversationText:
    def test_user_and_assistant_messages(self):
        messages = [
            {"role": "user", "content": "Hello", "content_json": None},
            {"role": "assistant", "content": "Hi there", "content_json": None},
        ]
        result = extract_conversation_text(messages)
        assert "**User:** Hello" in result
        assert "**Assistant:** Hi there" in result

    def test_system_messages_excluded(self):
        messages = [
            {"role": "system", "content": "You are helpful", "content_json": None},
            {"role": "user", "content": "Hello", "content_json": None},
        ]
        result = extract_conversation_text(messages)
        assert "helpful" not in result
        assert "Hello" in result

    def test_tool_result_excluded(self):
        messages = [
            {"role": "user", "content": "Read my files", "content_json": None},
            {
                "role": "assistant",
                "content": "tool_use: read_file",
                "content_json": '[{"type": "tool_use"}]',
            },
            {
                "role": "tool_result",
                "content": "SECRET DATA FROM FILE",
                "content_json": '[{"type": "tool_result"}]',
            },
        ]
        result = extract_conversation_text(messages)
        assert "Read my files" in result
        assert "SECRET DATA" not in result
        assert "tool_use" not in result

    def test_structured_assistant_messages_excluded(self):
        messages = [
            {
                "role": "assistant",
                "content": "Using tool",
                "content_json": '[{"type": "tool_use", "name": "read_file"}]',
            },
        ]
        result = extract_conversation_text(messages)
        assert "Using tool" not in result

    def test_empty_messages(self):
        result = extract_conversation_text([])
        assert result == ""

    def test_empty_content_skipped(self):
        messages = [
            {"role": "user", "content": "", "content_json": None},
            {"role": "user", "content": "Real message", "content_json": None},
        ]
        result = extract_conversation_text(messages)
        assert "Real message" in result


# --- get_reviewer_model ---


class TestGetReviewerModel:
    def test_configured_model(self, test_db, monkeypatch):
        current = config.CONFIG.copy()
        current["model_roles"] = {**current.get("model_roles", {}), "code_review": "anthropic:claude-opus-4-6"}
        monkeypatch.setattr(config, "CONFIG", current)

        assert get_reviewer_model() == "anthropic:claude-opus-4-6"

    def test_falls_back_to_default_role(self, test_db, monkeypatch):
        current = config.CONFIG.copy()
        current["model_roles"] = {**current.get("model_roles", {}), "code_review": "", "default": "anthropic:claude-sonnet-4-20250514"}
        monkeypatch.setattr(config, "CONFIG", current)

        result = get_reviewer_model()
        assert result == "anthropic:claude-sonnet-4-20250514"

    def test_falls_back_to_auto_detect(self, test_db, monkeypatch):
        current = config.CONFIG.copy()
        current["model_roles"] = {"code_review": "", "default": ""}
        current["ai_provider"] = "anthropic"
        monkeypatch.setattr(config, "CONFIG", current)

        result = get_reviewer_model()
        assert result == "anthropic:claude-sonnet-4-5-20250929"


# --- _parse_review_response ---


class TestParseReviewResponse:
    def test_approve(self):
        result = _parse_review_response("APPROVE", "code")
        assert result.status == "approve"
        assert result.reason == ""

    def test_minor(self):
        result = _parse_review_response("MINOR: Extra file read detected", "code")
        assert result.status == "minor"
        assert result.reason == "Extra file read detected"

    def test_major(self):
        result = _parse_review_response("MAJOR: Unauthorized network call", "code")
        assert result.status == "major"
        assert result.reason == "Unauthorized network call"

    def test_malformed_defaults_to_major(self):
        result = _parse_review_response("I think the code looks fine", "code")
        assert result.status == "major"
        assert "unparseable response" in result.reason

    def test_whitespace_handling(self):
        result = _parse_review_response("  APPROVE  ", "code")
        assert result.status == "approve"

    def test_approve_with_trailing_text_on_same_line_is_malformed(self):
        result = _parse_review_response("APPROVE but with concerns", "code")
        assert result.status == "major"  # Malformed, not exact "APPROVE" on first line

    def test_approve_with_explanation_on_next_lines(self):
        result = _parse_review_response(
            "APPROVE\n\nThe code correctly implements the user's request.",
            "code",
        )
        assert result.status == "approve"  # First line is "APPROVE"


# --- review_code (integration with mocked AI) ---


class TestReviewCode:
    @pytest.fixture(autouse=True)
    def setup_config(self, test_db, monkeypatch):
        current = config.CONFIG.copy()
        current["review"] = {"reviewer_model": "anthropic:claude-sonnet-4-20250514"}
        current["claude_api_key"] = "test-key"
        monkeypatch.setattr(config, "CONFIG", current)

    def _mock_claude_response(self, text):
        """Create a mock response dict matching claude_client.call() output."""
        return {
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }

    def test_approve_flow(self, mock_reviewer_ai):
        mock_reviewer_ai.return_value = self._mock_claude_response("APPROVE")

        messages = [
            {"role": "user", "content": "Write hello to a file", "content_json": None},
        ]
        result = review_code("a = S1\nfiles.write_file(S2, a)", messages, [])

        assert result.status == "approve"
        assert mock_reviewer_ai.called
        # Check that the reviewer was called with the sanitized code
        call_args = mock_reviewer_ai.call_args
        user_msg = call_args[0][1][0]["content"]  # messages[0]["content"]
        assert "Sanitized Code" in user_msg
        assert "files.write_file" in user_msg

    def test_major_alert_flow(self, mock_reviewer_ai):
        mock_reviewer_ai.return_value = self._mock_claude_response(
            "MAJOR: Code sends data to external URL not requested by user"
        )

        messages = [
            {"role": "user", "content": "Summarize my notes", "content_json": None},
        ]
        result = review_code("a = S1\nweb.post(S2, data=a)", messages, [])

        assert result.status == "major"
        assert "external URL" in result.reason

    def test_advisory_flags_passed_to_reviewer(self, mock_reviewer_ai):
        mock_reviewer_ai.return_value = self._mock_claude_response("APPROVE")

        flags = ["[high] Instruction override attempt (in comments)"]
        result = review_code("a = 1", [], flags)

        call_args = mock_reviewer_ai.call_args
        user_msg = call_args[0][1][0]["content"]
        assert "Advisory Flags" in user_msg
        assert "Instruction override" in user_msg

    def test_conversation_context_excludes_data(self, mock_reviewer_ai):
        mock_reviewer_ai.return_value = self._mock_claude_response("APPROVE")

        messages = [
            {"role": "user", "content": "Read my passwords file", "content_json": None},
            {"role": "assistant", "content": "I'll read that for you", "content_json": None},
            {
                "role": "assistant",
                "content": "tool call",
                "content_json": '[{"type": "tool_use"}]',
            },
            {
                "role": "tool_result",
                "content": "password123\nadmin:secret",
                "content_json": '[{"type": "tool_result"}]',
            },
            {"role": "assistant", "content": "Here are the contents", "content_json": None},
        ]
        result = review_code("a = 1", messages, [])

        call_args = mock_reviewer_ai.call_args
        user_msg = call_args[0][1][0]["content"]
        assert "Read my passwords file" in user_msg
        assert "password123" not in user_msg
        assert "admin:secret" not in user_msg


# --- _extract_verdict_from_tool_call ---


class TestExtractVerdictFromToolCall:
    def _tool_response(self, status, reasoning="reason"):
        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_123",
                    "name": "submit_verdict",
                    "input": {"status": status, "reasoning": reasoning},
                }
            ],
        }

    def test_approve(self):
        result = _extract_verdict_from_tool_call(
            self._tool_response("APPROVE", "Matches intent"), "code",
        )
        assert result is not None
        assert result.status == "approve"
        assert result.reason == ""

    def test_minor(self):
        result = _extract_verdict_from_tool_call(
            self._tool_response("MINOR", "Extra file read"), "code",
        )
        assert result.status == "minor"
        assert result.reason == "Extra file read"

    def test_major(self):
        result = _extract_verdict_from_tool_call(
            self._tool_response("MAJOR", "Unauthorized network call"), "code",
        )
        assert result.status == "major"
        assert result.reason == "Unauthorized network call"

    def test_invalid_status_defaults_to_major(self):
        result = _extract_verdict_from_tool_call(
            self._tool_response("UNKNOWN"), "code",
        )
        assert result.status == "major"
        assert "unclear verdict" in result.reason

    def test_wrong_tool_name_returns_none(self):
        response = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_456",
                    "name": "wrong_tool",
                    "input": {"status": "APPROVE", "reasoning": "ok"},
                }
            ],
        }
        result = _extract_verdict_from_tool_call(response, "code")
        assert result is None

    def test_no_tool_call_returns_none(self):
        response = {
            "content": [{"type": "text", "text": "APPROVE"}],
        }
        result = _extract_verdict_from_tool_call(response, "code")
        assert result is None

    def test_empty_content_returns_none(self):
        result = _extract_verdict_from_tool_call({"content": []}, "code")
        assert result is None

    def test_mixed_content_extracts_tool(self):
        """Tool_use block found even alongside text blocks."""
        response = {
            "content": [
                {"type": "text", "text": "Let me review this..."},
                {
                    "type": "tool_use",
                    "id": "toolu_789",
                    "name": "submit_verdict",
                    "input": {"status": "APPROVE", "reasoning": "Looks good"},
                },
            ],
        }
        result = _extract_verdict_from_tool_call(response, "code")
        assert result is not None
        assert result.status == "approve"


# --- review_code with tool_use response ---


class TestReviewCodeToolUse:
    @pytest.fixture(autouse=True)
    def setup_config(self, test_db, monkeypatch):
        current = config.CONFIG.copy()
        current["review"] = {"reviewer_model": "anthropic:claude-sonnet-4-20250514"}
        current["claude_api_key"] = "test-key"
        monkeypatch.setattr(config, "CONFIG", current)

    def test_tool_use_approve(self, mock_reviewer_ai):
        """Reviewer uses submit_verdict tool — extracted directly."""
        mock_reviewer_ai.return_value = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_abc",
                    "name": "submit_verdict",
                    "input": {"status": "APPROVE", "reasoning": "Code matches intent"},
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
        messages = [
            {"role": "user", "content": "Write hello", "content_json": None},
        ]
        result = review_code("a = S1", messages, [])
        assert result.status == "approve"

        # Verify tools were passed in the call
        call_kwargs = mock_reviewer_ai.call_args[1]
        assert "tools" in call_kwargs
        assert call_kwargs["tools"][0]["name"] == "submit_verdict"

    def test_text_fallback_when_no_tool_used(self, mock_reviewer_ai):
        """Anthropic model doesn't use tool — falls back to text parsing."""
        mock_reviewer_ai.return_value = {
            "content": [{"type": "text", "text": "APPROVE"}],
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
        messages = [
            {"role": "user", "content": "Write hello", "content_json": None},
        ]
        result = review_code("a = S1", messages, [])
        assert result.status == "approve"

    def test_tool_use_major(self, mock_reviewer_ai):
        mock_reviewer_ai.return_value = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_xyz",
                    "name": "submit_verdict",
                    "input": {"status": "MAJOR", "reasoning": "Sends data externally"},
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
        messages = [
            {"role": "user", "content": "Summarize notes", "content_json": None},
        ]
        result = review_code("web.post(S1, data=S2)", messages, [])
        assert result.status == "major"
        assert "Sends data externally" in result.reason
