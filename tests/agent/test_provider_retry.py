"""Tests for carpenter.agent.providers.retry shared module."""

import pytest
from unittest.mock import MagicMock, patch

import httpx

from carpenter.agent import circuit_breaker
from carpenter.agent.circuit_breaker import CircuitOpenError
from carpenter.agent.providers.retry import (
    build_openai_messages,
    default_is_retryable,
    retry_with_breaker,
)


# -- build_openai_messages --

class TestBuildOpenaiMessages:

    def test_prepends_system_prompt(self):
        """System prompt is the first message."""
        result = build_openai_messages("You are helpful.", [
            {"role": "user", "content": "Hello"},
        ])
        assert result[0] == {"role": "system", "content": "You are helpful."}
        assert result[1] == {"role": "user", "content": "Hello"}

    def test_preserves_tool_calls_on_assistant(self):
        """Assistant messages with tool_calls pass through."""
        tool_calls = [{"id": "tc1", "function": {"name": "foo", "arguments": "{}"}}]
        result = build_openai_messages("sys", [
            {"role": "assistant", "content": None, "tool_calls": tool_calls},
        ])
        assert result[1]["tool_calls"] == tool_calls
        assert result[1]["content"] is None

    def test_tool_role_gets_tool_call_id(self):
        """Tool-result messages get tool_call_id and string content."""
        result = build_openai_messages("sys", [
            {"role": "tool", "content": {"result": 42}, "tool_call_id": "tc1"},
        ])
        assert result[1]["tool_call_id"] == "tc1"
        assert result[1]["content"] == "{'result': 42}"

    def test_tool_role_missing_tool_call_id(self):
        """Tool-result messages default to empty tool_call_id."""
        result = build_openai_messages("sys", [
            {"role": "tool", "content": "ok"},
        ])
        assert result[1]["tool_call_id"] == ""

    def test_user_message_passthrough(self):
        """Plain user messages pass through unchanged."""
        result = build_openai_messages("sys", [
            {"role": "user", "content": "Hello"},
        ])
        assert result[1] == {"role": "user", "content": "Hello"}

    def test_multiple_messages(self):
        """Multiple messages are all included in order."""
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "How are you?"},
        ]
        result = build_openai_messages("sys", msgs)
        assert len(result) == 4  # system + 3 messages
        assert result[1]["content"] == "Hi"
        assert result[2]["content"] == "Hello!"
        assert result[3]["content"] == "How are you?"


# -- default_is_retryable --

class TestDefaultIsRetryable:

    def test_500_is_retryable(self):
        """Server errors (5xx) are retryable."""
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 500
        exc = httpx.HTTPStatusError("500", request=MagicMock(), response=resp)
        assert default_is_retryable(exc) is True

    def test_503_is_retryable(self):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 503
        exc = httpx.HTTPStatusError("503", request=MagicMock(), response=resp)
        assert default_is_retryable(exc) is True

    def test_400_not_retryable(self):
        """Client errors (4xx) are not retryable."""
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 400
        exc = httpx.HTTPStatusError("400", request=MagicMock(), response=resp)
        assert default_is_retryable(exc) is False

    def test_401_not_retryable(self):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 401
        exc = httpx.HTTPStatusError("401", request=MagicMock(), response=resp)
        assert default_is_retryable(exc) is False

    def test_connect_error_is_retryable(self):
        assert default_is_retryable(httpx.ConnectError("refused")) is True

    def test_timeout_is_retryable(self):
        assert default_is_retryable(httpx.TimeoutException("timeout")) is True

    def test_import_error_not_retryable(self):
        assert default_is_retryable(ImportError("no module")) is False

    def test_sdk_4xx_status_code_not_retryable(self):
        """Exceptions with a status_code attr in 4xx range are not retryable."""
        exc = Exception("Unauthorized")
        exc.status_code = 401
        assert default_is_retryable(exc) is False

    def test_generic_exception_is_retryable(self):
        """Unknown exceptions are assumed transient."""
        assert default_is_retryable(RuntimeError("something")) is True


# -- retry_with_breaker --

class TestRetryWithBreaker:

    def test_success_first_attempt(self):
        """Returns result on first successful attempt."""
        result = retry_with_breaker(
            "test-provider",
            lambda: {"ok": True},
            max_attempts=3,
            base_delay=0.0,
        )
        assert result == {"ok": True}

    def test_retries_on_transient_error(self):
        """Retries on transient errors and succeeds."""
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise httpx.ConnectError("refused")
            return {"ok": True}

        result = retry_with_breaker(
            "test-retry",
            flaky,
            max_attempts=3,
            base_delay=0.0,
        )
        assert result == {"ok": True}
        assert call_count == 3

    def test_raises_after_max_attempts(self):
        """Raises last error after exhausting all attempts."""
        result = retry_with_breaker
        with pytest.raises(httpx.ConnectError):
            retry_with_breaker(
                "test-exhaust",
                lambda: (_ for _ in ()).throw(httpx.ConnectError("refused")),
                max_attempts=2,
                base_delay=0.0,
            )

    def test_non_retryable_raises_immediately(self):
        """Non-retryable errors raise immediately without retry."""
        call_count = 0

        def bad_request():
            nonlocal call_count
            call_count += 1
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 400
            raise httpx.HTTPStatusError("400", request=MagicMock(), response=resp)

        with pytest.raises(httpx.HTTPStatusError):
            retry_with_breaker(
                "test-no-retry",
                bad_request,
                max_attempts=3,
                base_delay=0.0,
            )
        assert call_count == 1

    def test_circuit_breaker_open_raises(self):
        """Raises CircuitOpenError when breaker is open."""
        breaker = circuit_breaker.get_breaker("test-open")
        # Trip the breaker
        for _ in range(10):
            breaker.record_failure()

        with pytest.raises(CircuitOpenError, match="test-open"):
            retry_with_breaker(
                "test-open",
                lambda: {"ok": True},
                max_attempts=3,
                base_delay=0.0,
            )

    def test_records_success_on_breaker(self):
        """Successful call records success on the breaker."""
        breaker = circuit_breaker.get_breaker("test-success-record")
        # Add a failure first
        breaker.record_failure()
        assert breaker.failure_count == 1

        retry_with_breaker(
            "test-success-record",
            lambda: {"ok": True},
            max_attempts=1,
            base_delay=0.0,
        )
        assert breaker.failure_count == 0

    def test_records_failure_on_breaker(self):
        """Failed retryable call records failure on the breaker."""
        breaker = circuit_breaker.get_breaker("test-fail-record")
        assert breaker.failure_count == 0

        with pytest.raises(httpx.ConnectError):
            retry_with_breaker(
                "test-fail-record",
                lambda: (_ for _ in ()).throw(httpx.ConnectError("refused")),
                max_attempts=1,
                base_delay=0.0,
            )
        assert breaker.failure_count == 1

    def test_custom_is_retryable(self):
        """Custom is_retryable predicate is honored."""
        call_count = 0

        def always_fail():
            nonlocal call_count
            call_count += 1
            raise ValueError("custom")

        # With default: ValueError is retryable (generic exception)
        with pytest.raises(ValueError):
            retry_with_breaker(
                "test-custom-pred",
                always_fail,
                max_attempts=3,
                base_delay=0.0,
                is_retryable=lambda e: False,  # treat everything as non-retryable
            )
        assert call_count == 1  # No retry

    @patch("carpenter.agent.providers.retry.time.sleep")
    def test_backoff_delay(self, mock_sleep):
        """Backoff delays are applied between retries."""
        call_count = 0

        def fail_twice():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise httpx.ConnectError("refused")
            return {"ok": True}

        retry_with_breaker(
            "test-backoff",
            fail_twice,
            max_attempts=3,
            base_delay=1.0,
        )

        # Should have slept twice (after attempt 1 and 2)
        assert mock_sleep.call_count == 2
        # First delay: 1.0 * 2^0 + jitter = ~1.0-2.0
        first_delay = mock_sleep.call_args_list[0][0][0]
        assert 1.0 <= first_delay <= 2.0
        # Second delay: 1.0 * 2^1 + jitter = ~2.0-3.0
        second_delay = mock_sleep.call_args_list[1][0][0]
        assert 2.0 <= second_delay <= 3.0

    def test_uses_config_defaults(self, monkeypatch):
        """Uses retry_max_attempts and retry_base_delay from config."""
        from carpenter import config as cfg

        monkeypatch.setitem(cfg.CONFIG, "retry_max_attempts", 1)
        monkeypatch.setitem(cfg.CONFIG, "retry_base_delay", 0.0)

        call_count = 0

        def always_fail():
            nonlocal call_count
            call_count += 1
            raise httpx.ConnectError("refused")

        with pytest.raises(httpx.ConnectError):
            retry_with_breaker("test-config-defaults", always_fail)

        assert call_count == 1  # Only 1 attempt from config
