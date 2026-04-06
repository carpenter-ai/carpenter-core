"""Tests for the circuit breaker and retry logic."""

import time
import threading
from unittest.mock import patch, MagicMock

import httpx
import pytest

from carpenter.agent import circuit_breaker
from carpenter.agent.circuit_breaker import (
    CircuitBreaker, CircuitOpenError, CLOSED, OPEN, HALF_OPEN,
    get_breaker, reset,
)


# Circuit breaker reset is handled by _reset_circuit_breakers in
# tests/conftest.py (autouse for all tests).


# --- CircuitBreaker state machine ---

class TestCircuitBreakerStates:

    @pytest.mark.parametrize(
        "num_failures, threshold, expected_state, expected_allow",
        [
            pytest.param(0, 3, CLOSED, True, id="starts_closed"),
            pytest.param(2, 3, CLOSED, True, id="stays_closed_below_threshold"),
            pytest.param(3, 3, OPEN, False, id="opens_at_threshold"),
        ],
    )
    def test_state_after_failures(
        self, num_failures, threshold, expected_state, expected_allow,
    ):
        cb = CircuitBreaker("test", failure_threshold=threshold)
        for _ in range(num_failures):
            cb.record_failure()
        assert cb.state == expected_state
        assert cb.allow_request() is expected_allow

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.failure_count == 0
        assert cb.state == CLOSED
        # Two more failures shouldn't open it
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CLOSED

    def test_transitions_to_half_open_after_recovery(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.1)
        cb.record_failure()
        assert cb.state == OPEN
        assert cb.allow_request() is False
        # Wait for recovery timeout
        time.sleep(0.15)
        assert cb.allow_request() is True
        assert cb.state == HALF_OPEN

    @pytest.mark.parametrize(
        "action, expected_state, expected_failure_count",
        [
            pytest.param("success", CLOSED, 0, id="success_closes"),
            pytest.param("failure", OPEN, None, id="failure_reopens"),
        ],
    )
    def test_half_open_transition(
        self, action, expected_state, expected_failure_count,
    ):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.1)
        cb.record_failure()
        time.sleep(0.15)
        cb.allow_request()  # transitions to HALF_OPEN
        if action == "success":
            cb.record_success()
        else:
            cb.record_failure()
        assert cb.state == expected_state
        if expected_failure_count is not None:
            assert cb.failure_count == expected_failure_count

    def test_half_open_allows_requests(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.1)
        cb.record_failure()
        time.sleep(0.15)
        assert cb.allow_request() is True  # transitions to HALF_OPEN
        assert cb.allow_request() is True  # still allows in HALF_OPEN


# --- Provider-level breaker management ---

class TestGetBreaker:

    def test_creates_breaker_for_provider(self):
        b = get_breaker("anthropic")
        assert isinstance(b, CircuitBreaker)
        assert b.name == "anthropic"

    def test_returns_same_breaker_for_same_provider(self):
        b1 = get_breaker("anthropic")
        b2 = get_breaker("anthropic")
        assert b1 is b2

    def test_different_providers_get_different_breakers(self):
        b1 = get_breaker("anthropic")
        b2 = get_breaker("ollama")
        assert b1 is not b2

    def test_uses_config_values(self, monkeypatch):
        monkeypatch.setitem(
            __import__("carpenter.config", fromlist=["CONFIG"]).CONFIG,
            "circuit_breaker_threshold", 10)
        monkeypatch.setitem(
            __import__("carpenter.config", fromlist=["CONFIG"]).CONFIG,
            "circuit_breaker_recovery_seconds", 120)
        b = get_breaker("custom")
        assert b.failure_threshold == 10
        assert b.recovery_timeout == 120

    def test_reset_clears_all_breakers(self):
        get_breaker("a")
        get_breaker("b")
        reset()
        # New breakers should be created
        b = get_breaker("a")
        assert b.failure_count == 0


# --- Integration with Claude client ---

class TestClaudeClientRetry:

    def _mock_response(self, status_code, json_data=None, headers=None):
        """Create a mock httpx.Response."""
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status_code
        resp.headers = headers or {}
        resp.json.return_value = json_data or {}
        if status_code >= 400:
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                f"HTTP {status_code}", request=MagicMock(), response=resp)
        else:
            resp.raise_for_status.return_value = None
        return resp

    @patch("carpenter.agent.providers.anthropic.rate_limiter")
    @patch("httpx.post")
    def test_successful_call_no_retry(self, mock_post, mock_rl):
        mock_rl.acquire.return_value = True
        mock_post.return_value = self._mock_response(200, {
            "content": [{"type": "text", "text": "hello"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

        from carpenter.agent.providers import anthropic as claude_client
        result = claude_client.call("system", [{"role": "user", "content": "hi"}],
                                     api_key="test-key")
        assert result["content"][0]["text"] == "hello"
        assert mock_post.call_count == 1

    @patch("carpenter.agent.providers.anthropic.rate_limiter")
    @patch("httpx.post")
    def test_retries_on_500(self, mock_post, mock_rl):
        mock_rl.acquire.return_value = True
        mock_post.side_effect = [
            self._mock_response(500),
            self._mock_response(200, {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }),
        ]

        from carpenter.agent.providers import anthropic as claude_client
        result = claude_client.call("system", [{"role": "user", "content": "hi"}],
                                     api_key="test-key")
        assert result["content"][0]["text"] == "ok"
        assert mock_post.call_count == 2

    @patch("carpenter.agent.providers.anthropic.rate_limiter")
    @patch("httpx.post")
    def test_retries_on_429(self, mock_post, mock_rl):
        mock_rl.acquire.return_value = True
        mock_post.side_effect = [
            self._mock_response(429),
            self._mock_response(200, {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }),
        ]

        from carpenter.agent.providers import anthropic as claude_client
        result = claude_client.call("system", [{"role": "user", "content": "hi"}],
                                     api_key="test-key")
        assert result["content"][0]["text"] == "ok"
        mock_rl.record_429.assert_called_once()

    @patch("carpenter.agent.providers.anthropic.rate_limiter")
    @patch("httpx.post")
    def test_retries_on_connection_error(self, mock_post, mock_rl):
        mock_rl.acquire.return_value = True
        mock_post.side_effect = [
            httpx.ConnectError("refused"),
            self._mock_response(200, {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }),
        ]

        from carpenter.agent.providers import anthropic as claude_client
        result = claude_client.call("system", [{"role": "user", "content": "hi"}],
                                     api_key="test-key")
        assert result["content"][0]["text"] == "ok"
        assert mock_post.call_count == 2

    @patch("carpenter.agent.providers.anthropic.rate_limiter")
    @patch("httpx.post")
    def test_raises_after_max_retries(self, mock_post, mock_rl):
        mock_rl.acquire.return_value = True
        mock_post.side_effect = httpx.ConnectError("refused")

        from carpenter.agent.providers import anthropic as claude_client
        with pytest.raises(httpx.ConnectError):
            claude_client.call("system", [{"role": "user", "content": "hi"}],
                               api_key="test-key")
        assert mock_post.call_count == 3  # default retry_max_attempts

    @patch("carpenter.agent.providers.anthropic.rate_limiter")
    @patch("httpx.post")
    def test_does_not_retry_400(self, mock_post, mock_rl):
        mock_rl.acquire.return_value = True
        mock_post.return_value = self._mock_response(400)

        from carpenter.agent.providers import anthropic as claude_client
        with pytest.raises(httpx.HTTPStatusError):
            claude_client.call("system", [{"role": "user", "content": "hi"}],
                               api_key="test-key")
        assert mock_post.call_count == 1

    @patch("carpenter.agent.providers.anthropic.rate_limiter")
    @patch("httpx.post")
    def test_circuit_breaker_opens_after_failures(self, mock_post, mock_rl):
        mock_rl.acquire.return_value = True
        mock_post.side_effect = httpx.ConnectError("refused")

        from carpenter.agent.providers import anthropic as claude_client

        # Exhaust retries multiple times to trip the breaker
        # With threshold=5 and max_attempts=3, need 2 full call cycles
        # (each cycle records 3 failures, second cycle opens at failure 5)
        for _ in range(2):
            try:
                claude_client.call("system", [{"role": "user", "content": "hi"}],
                                   api_key="test-key")
            except (httpx.ConnectError, CircuitOpenError):
                pass

        # Now the breaker should be open
        breaker = get_breaker("anthropic")
        assert breaker.state == OPEN

        with pytest.raises(CircuitOpenError):
            claude_client.call("system", [{"role": "user", "content": "hi"}],
                               api_key="test-key")


# --- Integration with Ollama client ---

class TestOllamaClientRetry:

    def _mock_response(self, status_code, json_data=None):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status_code
        resp.json.return_value = json_data or {}
        if status_code >= 400:
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                f"HTTP {status_code}", request=MagicMock(), response=resp)
        else:
            resp.raise_for_status.return_value = None
        return resp

    @patch("httpx.post")
    def test_successful_call(self, mock_post):
        mock_post.return_value = self._mock_response(200, {
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

        from carpenter.agent.providers import ollama as ollama_client
        result = ollama_client.call("system", [{"role": "user", "content": "hi"}])
        assert mock_post.call_count == 1

    @patch("httpx.post")
    def test_retries_on_500(self, mock_post):
        mock_post.side_effect = [
            self._mock_response(503),
            self._mock_response(200, {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }),
        ]

        from carpenter.agent.providers import ollama as ollama_client
        result = ollama_client.call("system", [{"role": "user", "content": "hi"}])
        assert mock_post.call_count == 2

    @patch("httpx.post")
    def test_retries_on_timeout(self, mock_post):
        mock_post.side_effect = [
            httpx.ReadTimeout("timeout"),
            self._mock_response(200, {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }),
        ]

        from carpenter.agent.providers import ollama as ollama_client
        result = ollama_client.call("system", [{"role": "user", "content": "hi"}])
        assert mock_post.call_count == 2

    @patch("httpx.post")
    def test_does_not_retry_400(self, mock_post):
        mock_post.return_value = self._mock_response(400)

        from carpenter.agent.providers import ollama as ollama_client
        with pytest.raises(httpx.HTTPStatusError):
            ollama_client.call("system", [{"role": "user", "content": "hi"}])
        assert mock_post.call_count == 1


# --- Thread safety ---

class TestCircuitBreakerThreadSafety:

    def test_concurrent_failures(self):
        """Multiple threads recording failures should be safe."""
        cb = CircuitBreaker("test", failure_threshold=100)
        errors = []

        def record_many():
            try:
                for _ in range(50):
                    cb.record_failure()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record_many) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert cb.failure_count == 200
        assert cb.state == OPEN

    def test_concurrent_success_and_failure(self):
        """Mixed success/failure from multiple threads should not crash."""
        cb = CircuitBreaker("test", failure_threshold=1000)
        errors = []

        def mixed_ops():
            try:
                for i in range(100):
                    cb.allow_request()
                    if i % 2 == 0:
                        cb.record_success()
                    else:
                        cb.record_failure()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=mixed_ops) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
