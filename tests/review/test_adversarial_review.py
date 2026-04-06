"""Tests for adversarial review mode.

Adversarial mode requires the reviewer to find at least N findings.
Zero findings = insufficient review, not clean code.
"""

from unittest.mock import patch

import pytest

from carpenter.review.code_reviewer import (
    review_code_adversarial,
    review_code,
    Finding,
    ReviewResult,
    _extract_findings_from_tool_call,
    format_findings_for_human,
)
from carpenter import config


# --- _extract_findings_from_tool_call ---


class TestExtractFindings:
    def _tool_response(self, status, reasoning, findings):
        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_123",
                    "name": "submit_verdict",
                    "input": {
                        "status": status,
                        "reasoning": reasoning,
                        "findings": findings,
                    },
                }
            ],
        }

    def test_extracts_findings(self):
        resp = self._tool_response("APPROVE", "ok", [
            {
                "location": "line 3",
                "severity": "note",
                "description": "Implicit assumption about input type",
                "remediation": "Add type check",
            },
        ])
        findings = _extract_findings_from_tool_call(resp)
        assert len(findings) == 1
        assert findings[0].location == "line 3"
        assert findings[0].severity == "note"

    def test_empty_findings(self):
        resp = self._tool_response("APPROVE", "ok", [])
        findings = _extract_findings_from_tool_call(resp)
        assert findings == []

    def test_no_findings_key(self):
        resp = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_123",
                    "name": "submit_verdict",
                    "input": {"status": "APPROVE", "reasoning": "ok"},
                }
            ],
        }
        findings = _extract_findings_from_tool_call(resp)
        assert findings == []

    def test_multiple_findings(self):
        resp = self._tool_response("MINOR", "issues", [
            {
                "location": "line 1",
                "severity": "warning",
                "description": "Missing error handling",
                "remediation": "Add try/except",
            },
            {
                "location": "line 5",
                "severity": "critical",
                "description": "Unauthorized network call",
                "remediation": "Remove network call",
            },
        ])
        findings = _extract_findings_from_tool_call(resp)
        assert len(findings) == 2
        assert findings[0].severity == "warning"
        assert findings[1].severity == "critical"

    def test_non_tool_use_block_ignored(self):
        resp = {
            "content": [
                {"type": "text", "text": "Some review text"},
            ],
        }
        findings = _extract_findings_from_tool_call(resp)
        assert findings == []

    def test_wrong_tool_name_ignored(self):
        resp = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_456",
                    "name": "wrong_tool",
                    "input": {
                        "findings": [
                            {"location": "x", "severity": "note",
                             "description": "d", "remediation": "r"},
                        ],
                    },
                }
            ],
        }
        findings = _extract_findings_from_tool_call(resp)
        assert findings == []

    def test_malformed_finding_entry_handled(self):
        """Non-dict entries in findings array are skipped."""
        resp = self._tool_response("APPROVE", "ok", [
            "not a dict",
            {"location": "line 1", "severity": "note",
             "description": "ok", "remediation": "none"},
        ])
        findings = _extract_findings_from_tool_call(resp)
        assert len(findings) == 1
        assert findings[0].location == "line 1"


# --- format_findings_for_human ---


class TestFormatFindings:
    def test_empty_findings(self):
        result = format_findings_for_human([])
        assert result == "(No findings)"

    def test_single_finding(self):
        findings = [
            Finding("line 5", "warning", "Missing error check", "Add try/except"),
        ]
        result = format_findings_for_human(findings)
        assert "Finding 1 [WARNING]" in result
        assert "line 5" in result
        assert "Missing error check" in result
        assert "Add try/except" in result

    def test_multiple_findings_formatted(self):
        findings = [
            Finding("line 1", "critical", "Security issue", "Fix it"),
            Finding("line 10", "note", "Style issue", "Consider changing"),
        ]
        result = format_findings_for_human(findings)
        assert "Finding 1 [CRITICAL]" in result
        assert "Finding 2 [NOTE]" in result



# --- review_code_adversarial (mocked AI) ---


class TestReviewCodeAdversarial:
    @pytest.fixture(autouse=True)
    def setup_config(self, test_db, monkeypatch):
        current = config.CONFIG.copy()
        current["review"] = {
            "reviewer_model": "anthropic:claude-sonnet-4-20250514",
            "adversarial_mode": True,
            "adversarial_min_findings": 1,
        }
        current["claude_api_key"] = "test-key"
        monkeypatch.setattr(config, "CONFIG", current)

    def _make_tool_response(self, status, reasoning, findings=None):
        """Create a mock response with submit_verdict tool call."""
        inp = {"status": status, "reasoning": reasoning}
        if findings is not None:
            inp["findings"] = findings
        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_abc",
                    "name": "submit_verdict",
                    "input": inp,
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

    def test_pass1_with_findings_accepted(self, mock_reviewer_ai):
        """First pass finds issues — accepted immediately."""
        mock_reviewer_ai.return_value = self._make_tool_response(
            "APPROVE", "Code matches intent", [
                {"location": "line 3", "severity": "note",
                 "description": "Implicit assumption about input", "remediation": "Document"},
            ],
        )

        messages = [{"role": "user", "content": "Write x=1", "content_json": None}]
        result = review_code_adversarial("a = S1", messages, [])

        assert result.status == "approve"
        assert len(result.findings) == 1
        assert result.review_pass == 1
        assert result.adversarial_escalated is False
        # Should only be called once (no escalation)
        assert mock_reviewer_ai.call_count == 1

    def test_pass1_zero_findings_triggers_pass2(self, mock_reviewer_ai):
        """First pass finds nothing — triggers second pass with escalation prompt."""
        # First call: no findings. Second call: has findings.
        mock_reviewer_ai.side_effect = [
            self._make_tool_response("APPROVE", "Looks good", []),
            self._make_tool_response("APPROVE", "Found edge case", [
                {"location": "line 1", "severity": "note",
                 "description": "Edge case with empty input", "remediation": "Add guard"},
            ]),
        ]

        messages = [{"role": "user", "content": "Write x=1", "content_json": None}]
        result = review_code_adversarial("a = S1", messages, [])

        assert result.status == "approve"
        assert len(result.findings) == 1
        assert result.review_pass == 2
        assert result.adversarial_escalated is True
        assert mock_reviewer_ai.call_count == 2

        # Verify second call includes escalation prompt
        second_call_args = mock_reviewer_ai.call_args_list[1]
        second_user_msg = second_call_args[0][1][0]["content"]
        assert "ESCALATED REVIEW" in second_user_msg

    def test_both_passes_zero_findings_returns_major(self, mock_reviewer_ai):
        """Both passes find nothing, no escalation model — returns MAJOR."""
        mock_reviewer_ai.return_value = self._make_tool_response("APPROVE", "Clean", [])

        messages = [{"role": "user", "content": "Write x=1", "content_json": None}]
        result = review_code_adversarial("a = S1", messages, [])

        assert result.status == "major"
        assert "no findings" in result.reason.lower()
        assert result.adversarial_escalated is True
        # 2 passes (no escalation model available)
        assert mock_reviewer_ai.call_count == 2

    @patch("carpenter.agent.model_resolver.get_next_model")
    def test_model_escalation_on_double_zero(self, mock_next, mock_reviewer_ai):
        """Both passes find nothing — escalates to stronger model."""
        mock_next.return_value = "anthropic:claude-opus-4-6-20250514"
        mock_reviewer_ai.side_effect = [
            # Pass 1: no findings
            self._make_tool_response("APPROVE", "Clean", []),
            # Pass 2: no findings
            self._make_tool_response("APPROVE", "Clean", []),
            # Pass 3 (escalated model): has findings
            self._make_tool_response("MINOR", "Found issue", [
                {"location": "global scope", "severity": "warning",
                 "description": "No error handling", "remediation": "Add try/except"},
            ]),
        ]

        messages = [{"role": "user", "content": "Write x=1", "content_json": None}]
        result = review_code_adversarial("a = S1", messages, [])

        assert result.status == "minor"
        assert len(result.findings) == 1
        assert result.adversarial_escalated is True
        assert mock_reviewer_ai.call_count == 3

    @patch("carpenter.agent.model_resolver.get_next_model")
    def test_escalated_model_also_zero_returns_major(self, mock_next, mock_reviewer_ai):
        """All three passes find nothing — returns MAJOR for human review."""
        mock_next.return_value = "anthropic:claude-opus-4-6-20250514"
        mock_reviewer_ai.return_value = self._make_tool_response("APPROVE", "Clean", [])

        messages = [{"role": "user", "content": "Write x=1", "content_json": None}]
        result = review_code_adversarial("a = S1", messages, [])

        assert result.status == "major"
        assert "escalated model" in result.reason.lower()
        assert result.adversarial_escalated is True
        assert mock_reviewer_ai.call_count == 3

    def test_adversarial_uses_larger_max_tokens(self, mock_reviewer_ai):
        """Adversarial mode uses larger max_tokens for structured findings."""
        mock_reviewer_ai.return_value = self._make_tool_response(
            "APPROVE", "ok", [
                {"location": "line 1", "severity": "note",
                 "description": "Noted", "remediation": "None needed"},
            ],
        )

        messages = [{"role": "user", "content": "Write x=1", "content_json": None}]
        review_code_adversarial("a = S1", messages, [])

        call_kwargs = mock_reviewer_ai.call_args[1]
        assert call_kwargs["max_tokens"] == 1000

    def test_adversarial_system_prompt_appended(self, mock_reviewer_ai):
        """Adversarial mode appends instructions to system prompt."""
        mock_reviewer_ai.return_value = self._make_tool_response(
            "APPROVE", "ok", [
                {"location": "line 1", "severity": "note",
                 "description": "ok", "remediation": "none"},
            ],
        )

        messages = [{"role": "user", "content": "Write x=1", "content_json": None}]
        review_code_adversarial("a = S1", messages, [])

        system_prompt = mock_reviewer_ai.call_args[0][0]
        assert "ADVERSARIAL MODE" in system_prompt
        assert "at least one finding" in system_prompt

    def test_adversarial_uses_adversarial_tool_schema(self, mock_reviewer_ai):
        """Adversarial mode uses the tool schema that requires findings."""
        mock_reviewer_ai.return_value = self._make_tool_response(
            "APPROVE", "ok", [
                {"location": "line 1", "severity": "note",
                 "description": "ok", "remediation": "none"},
            ],
        )

        messages = [{"role": "user", "content": "Write x=1", "content_json": None}]
        review_code_adversarial("a = S1", messages, [])

        call_kwargs = mock_reviewer_ai.call_args[1]
        tool = call_kwargs["tools"][0]
        assert "findings" in tool["input_schema"]["properties"]

    def test_major_verdict_with_findings_pass1(self, mock_reviewer_ai):
        """MAJOR verdict with findings on first pass — no escalation needed."""
        mock_reviewer_ai.return_value = self._make_tool_response(
            "MAJOR", "Unauthorized network call", [
                {"location": "line 2", "severity": "critical",
                 "description": "Sends data to external URL",
                 "remediation": "Remove network call"},
            ],
        )

        messages = [{"role": "user", "content": "Summarize notes", "content_json": None}]
        result = review_code_adversarial("web.post(S1, data=S2)", messages, [])

        assert result.status == "major"
        assert len(result.findings) == 1
        assert result.findings[0].severity == "critical"
        assert result.review_pass == 1
        assert mock_reviewer_ai.call_count == 1


# --- Config option tests ---


class TestAdversarialConfig:
    def test_min_findings_respected(self, test_db, monkeypatch, mock_reviewer_ai):
        """Custom min_findings threshold is used."""
        current = config.CONFIG.copy()
        current["review"] = {
            "reviewer_model": "anthropic:claude-sonnet-4-20250514",
            "adversarial_mode": True,
            "adversarial_min_findings": 3,
        }
        current["claude_api_key"] = "test-key"
        monkeypatch.setattr(config, "CONFIG", current)

        # Return 2 findings (below threshold of 3)
        mock_reviewer_ai.return_value = {
            "content": [{
                "type": "tool_use",
                "id": "toolu_x",
                "name": "submit_verdict",
                "input": {
                    "status": "APPROVE",
                    "reasoning": "ok",
                    "findings": [
                        {"location": "l1", "severity": "note",
                         "description": "d1", "remediation": "r1"},
                        {"location": "l2", "severity": "note",
                         "description": "d2", "remediation": "r2"},
                    ],
                },
            }],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

        messages = [{"role": "user", "content": "test", "content_json": None}]
        result = review_code_adversarial("a = 1", messages, [])

        # 2 findings < min_findings of 3 → should escalate
        assert mock_reviewer_ai.call_count >= 2


# --- Standard mode backward compatibility ---


class TestStandardModeUnchanged:
    @pytest.fixture(autouse=True)
    def setup_config(self, test_db, monkeypatch):
        current = config.CONFIG.copy()
        current["review"] = {
            "reviewer_model": "anthropic:claude-sonnet-4-20250514",
            "adversarial_mode": False,
        }
        current["claude_api_key"] = "test-key"
        monkeypatch.setattr(config, "CONFIG", current)

    def test_standard_approve_no_findings_ok(self, mock_reviewer_ai):
        """In standard mode, zero findings with APPROVE is perfectly fine."""
        mock_reviewer_ai.return_value = {
            "content": [{
                "type": "tool_use",
                "id": "toolu_abc",
                "name": "submit_verdict",
                "input": {"status": "APPROVE", "reasoning": "Code matches intent"},
            }],
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }

        messages = [{"role": "user", "content": "Write x=1", "content_json": None}]
        result = review_code("a = S1", messages, [])

        assert result.status == "approve"
        assert result.findings == []  # No findings required in standard mode
        assert mock_reviewer_ai.call_count == 1  # No re-review


# --- Pipeline integration with adversarial mode ---


class TestPipelineAdversarial:
    @pytest.fixture(autouse=True)
    def setup_config(self, test_db, monkeypatch):
        from carpenter.review.pipeline import clear_cache
        clear_cache()  # Clear approval cache between tests

        current = config.CONFIG.copy()
        current["review"] = {
            "reviewer_model": "anthropic:claude-sonnet-4-20250514",
            "adversarial_mode": True,
            "adversarial_min_findings": 1,
        }
        current["claude_api_key"] = "test-key"
        monkeypatch.setattr(config, "CONFIG", current)

    def test_pipeline_uses_adversarial_when_configured(self, mock_reviewer_ai):
        """Pipeline dispatches to adversarial review when config is set."""
        from carpenter.review.pipeline import run_review_pipeline
        from carpenter.agent import conversation

        # Create a conversation
        conv_id = conversation.create_conversation()
        conversation.add_message(conv_id, "user", "Write x=1")

        mock_reviewer_ai.return_value = {
            "content": [{
                "type": "tool_use",
                "id": "toolu_abc",
                "name": "submit_verdict",
                "input": {
                    "status": "APPROVE",
                    "reasoning": "ok",
                    "findings": [
                        {"location": "line 1", "severity": "note",
                         "description": "Simple assignment", "remediation": "None needed"},
                    ],
                },
            }],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

        result = run_review_pipeline("x = 1\n", conv_id)

        assert result.outcome is not None
        # Verify adversarial tool was used (has findings in schema)
        call_kwargs = mock_reviewer_ai.call_args[1]
        tool = call_kwargs["tools"][0]
        assert "findings" in tool["input_schema"]["properties"]
        # Verify review_result is populated
        assert result.review_result is not None
        assert len(result.review_result.findings) == 1

    def test_pipeline_standard_when_not_configured(self, mock_reviewer_ai, monkeypatch):
        """Pipeline uses standard review when adversarial_mode is False."""
        current = config.CONFIG.copy()
        current["review"] = {
            "reviewer_model": "anthropic:claude-sonnet-4-20250514",
            "adversarial_mode": False,
        }
        current["claude_api_key"] = "test-key"
        monkeypatch.setattr(config, "CONFIG", current)

        from carpenter.review.pipeline import run_review_pipeline
        from carpenter.agent import conversation

        conv_id = conversation.create_conversation()
        conversation.add_message(conv_id, "user", "Write x=1")

        mock_reviewer_ai.return_value = {
            "content": [{
                "type": "tool_use",
                "id": "toolu_abc",
                "name": "submit_verdict",
                "input": {"status": "APPROVE", "reasoning": "ok"},
            }],
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }

        result = run_review_pipeline("x = 1\n", conv_id)

        assert result.status == "approved"
        # Standard tool schema (no findings property)
        call_kwargs = mock_reviewer_ai.call_args[1]
        tool = call_kwargs["tools"][0]
        assert "findings" not in tool["input_schema"]["properties"]


