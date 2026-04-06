"""Tests for error classification system."""

import json
from unittest.mock import Mock

import pytest

from carpenter.agent.error_classifier import (
    ErrorInfo,
    classify_error,
    _extract_status_code,
    _extract_retry_after,
    _is_network_error,
    _format_rate_limit_message,
    _format_outage_message,
    _format_network_message,
)


class TestErrorInfo:
    """Tests for ErrorInfo dataclass."""

    def test_error_info_creation(self):
        """Test creating an ErrorInfo instance."""
        error = ErrorInfo(
            type="RateLimitError",
            retry_count=3,
            source_location="test",
            message="Test message",
            status_code=429,
        )
        assert error.type == "RateLimitError"
        assert error.retry_count == 3
        assert error.status_code == 429
        assert error.timestamp is not None  # Auto-generated

    def test_error_info_to_json(self):
        """Test JSON serialization."""
        error = ErrorInfo(
            type="RateLimitError",
            retry_count=2,
            source_location="test",
            message="Rate limit reached",
            status_code=429,
            retry_after=30.0,
        )
        result = error.to_json()

        assert "error_info" in result
        info = result["error_info"]
        assert info["type"] == "RateLimitError"
        assert info["retry_count"] == 2
        assert info["status_code"] == 429
        assert info["retry_after"] == 30.0
        assert "timestamp" in info

    def test_error_info_filters_none_values(self):
        """Test that None values are filtered from JSON."""
        error = ErrorInfo(
            type="NetworkError",
            retry_count=1,
            source_location="test",
            message="Connection failed",
            # status_code, retry_after, model, provider all None
        )
        result = error.to_json()
        info = result["error_info"]

        assert "status_code" not in info
        assert "retry_after" not in info
        assert "model" not in info
        assert "provider" not in info


class TestClassifyError:
    """Tests for classify_error function."""

    def test_classify_429_rate_limit(self):
        """Test classification of 429 rate limit error."""
        # Create mock exception with httpx pattern
        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.headers = {"retry-after": "30"}

        mock_exception = Mock()
        mock_exception.response = mock_response

        error = classify_error(
            mock_exception,
            retry_count=2,
            model="claude-3-5-sonnet",
            provider="anthropic",
        )

        assert error.type == "RateLimitError"
        assert error.retry_count == 2
        assert error.status_code == 429
        assert error.retry_after == 30.0
        assert error.model == "claude-3-5-sonnet"
        assert error.provider == "anthropic"
        assert "rate limit" in error.message.lower()

    def test_classify_500_outage(self):
        """Test classification of 500 server error."""
        mock_response = Mock()
        mock_response.status_code = 500

        mock_exception = Mock()
        mock_exception.response = mock_response

        error = classify_error(mock_exception, retry_count=3)

        assert error.type == "APIOutageError"
        assert error.status_code == 500
        assert "unavailable" in error.message.lower()

    def test_classify_503_outage(self):
        """Test classification of 503 service unavailable."""
        mock_response = Mock()
        mock_response.status_code = 503

        mock_exception = Mock()
        mock_exception.response = mock_response

        error = classify_error(mock_exception, retry_count=4)

        assert error.type == "APIOutageError"
        assert error.status_code == 503

    def test_classify_circuit_open(self):
        """Test classification of circuit breaker error."""
        class CircuitOpenError(Exception):
            """Mock circuit breaker error."""
            pass

        error = classify_error(
            CircuitOpenError("Circuit open"),
            retry_count=3,
        )

        assert error.type == "APIOutageError"
        assert "circuit breaker" in error.message.lower()

    def test_classify_connect_error(self):
        """Test classification of connection error."""
        class ConnectError(Exception):
            """Mock connection error."""
            pass

        error = classify_error(
            ConnectError("Failed to connect"),
            retry_count=2,
        )

        assert error.type == "NetworkError"
        assert "connection" in error.message.lower()

    def test_classify_timeout(self):
        """Test classification of timeout error."""
        class TimeoutException(Exception):
            """Mock timeout error."""
            pass

        error = classify_error(
            TimeoutException("Request timed out"),
            retry_count=2,
        )

        assert error.type == "NetworkError"
        msg_lower = error.message.lower()
        assert "timeout" in msg_lower or "timed out" in msg_lower

    def test_classify_401_auth(self):
        """Test classification of 401 authentication error."""
        mock_response = Mock()
        mock_response.status_code = 401

        mock_exception = Mock()
        mock_exception.response = mock_response

        error = classify_error(mock_exception, retry_count=1)

        assert error.type == "AuthError"
        assert error.status_code == 401
        assert "authentication" in error.message.lower()

    def test_classify_403_forbidden(self):
        """Test classification of 403 forbidden error."""
        mock_response = Mock()
        mock_response.status_code = 403

        mock_exception = Mock()
        mock_exception.response = mock_response

        error = classify_error(mock_exception, retry_count=1)

        assert error.type == "AuthError"
        assert error.status_code == 403

    def test_classify_404_model(self):
        """Test classification of 404 model not found."""
        mock_response = Mock()
        mock_response.status_code = 404

        mock_exception = Mock()
        mock_exception.response = mock_response

        error = classify_error(
            mock_exception,
            retry_count=1,
            model="gpt-4-turbo",
        )

        assert error.type == "ModelError"
        assert error.status_code == 404
        assert "gpt-4-turbo" in error.message

    def test_classify_400_client(self):
        """Test classification of 400 bad request."""
        mock_response = Mock()
        mock_response.status_code = 400

        mock_exception = Mock()
        mock_exception.response = mock_response

        error = classify_error(mock_exception, retry_count=1)

        assert error.type == "ClientError"
        assert error.status_code == 400
        assert "400" in error.message

    def test_classify_422_client(self):
        """Test classification of 422 unprocessable entity."""
        mock_response = Mock()
        mock_response.status_code = 422

        mock_exception = Mock()
        mock_exception.response = mock_response

        error = classify_error(mock_exception, retry_count=1)

        assert error.type == "ClientError"
        assert error.status_code == 422

    def test_classify_unknown_exception(self):
        """Test classification of unknown error type."""
        error = classify_error(
            ValueError("Something went wrong"),
            retry_count=2,
        )

        assert error.type == "UnknownError"
        assert "ValueError" in error.message
        assert error.status_code is None


class TestExtractStatusCode:
    """Tests for _extract_status_code helper."""

    def test_extract_from_httpx_pattern(self):
        """Test extracting status code from httpx exception."""
        mock_response = Mock()
        mock_response.status_code = 429

        mock_exception = Mock()
        mock_exception.response = mock_response

        assert _extract_status_code(mock_exception) == 429

    def test_extract_from_direct_attribute(self):
        """Test extracting status code from direct attribute."""
        mock_exception = Mock()
        mock_exception.status_code = 503

        assert _extract_status_code(mock_exception) == 503

    def test_extract_none_when_missing(self):
        """Test returns None when no status code available."""
        mock_exception = Exception("Generic error")
        assert _extract_status_code(mock_exception) is None


class TestExtractRetryAfter:
    """Tests for _extract_retry_after helper."""

    def test_extract_retry_after_header(self):
        """Test extracting retry-after from response headers."""
        mock_response = Mock()
        mock_response.headers = {"retry-after": "60"}

        mock_exception = Mock()
        mock_exception.response = mock_response

        assert _extract_retry_after(mock_exception) == 60.0

    def test_extract_retry_after_uppercase(self):
        """Test extracting Retry-After header (uppercase)."""
        mock_response = Mock()
        mock_response.headers = {"Retry-After": "120"}

        mock_exception = Mock()
        mock_exception.response = mock_response

        assert _extract_retry_after(mock_exception) == 120.0

    def test_default_when_missing(self):
        """Test default value when header missing."""
        mock_response = Mock()
        mock_response.headers = {}

        mock_exception = Mock()
        mock_exception.response = mock_response

        assert _extract_retry_after(mock_exception) == 5.0

    def test_default_when_invalid(self):
        """Test default value when header is invalid."""
        mock_response = Mock()
        mock_response.headers = {"retry-after": "invalid"}

        mock_exception = Mock()
        mock_exception.response = mock_response

        assert _extract_retry_after(mock_exception) == 5.0

    def test_default_when_no_response(self):
        """Test default value when no response object."""
        mock_exception = Exception("No response")
        assert _extract_retry_after(mock_exception) == 5.0


class TestIsNetworkError:
    """Tests for _is_network_error helper."""

    def test_detect_connect_error(self):
        """Test detection of ConnectError."""
        class ConnectError(Exception):
            pass
        assert _is_network_error(ConnectError("Failed"))

    def test_detect_timeout_error(self):
        """Test detection of TimeoutError."""
        class TimeoutException(Exception):
            pass
        assert _is_network_error(TimeoutException("Timeout"))

    def test_detect_connection_refused(self):
        """Test detection from error message."""
        exc = Exception("connection refused")
        assert _is_network_error(exc)

    def test_detect_timeout_message(self):
        """Test detection of timeout in message."""
        exc = Exception("Request timed out after 30s")
        assert _is_network_error(exc)

    def test_detect_dns_failure(self):
        """Test detection of DNS resolution error."""
        exc = Exception("temporary failure in name resolution")
        assert _is_network_error(exc)

    def test_not_network_error(self):
        """Test non-network error returns False."""
        exc = ValueError("Invalid input")
        assert not _is_network_error(exc)


class TestFormatMessages:
    """Tests for message formatting helpers."""

    def test_rate_limit_message_first_attempt(self):
        """Test rate limit message on first attempt."""
        msg = _format_rate_limit_message(retry_after=30.0, retry_count=1)
        assert "30 seconds" in msg
        assert "retrying" in msg.lower()

    def test_rate_limit_message_exhausted(self):
        """Test rate limit message after multiple attempts."""
        msg = _format_rate_limit_message(retry_after=30.0, retry_count=4)
        assert "4 attempts" in msg
        assert "wait" in msg.lower()

    def test_outage_message_circuit(self):
        """Test outage message for circuit breaker."""
        msg = _format_outage_message(retry_count=3, is_circuit_open=True)
        assert "circuit breaker" in msg.lower()
        assert "3 attempts" in msg

    def test_outage_message_server_error(self):
        """Test outage message for server error."""
        msg = _format_outage_message(retry_count=4, is_circuit_open=False)
        assert "unavailable" in msg.lower()
        assert "4 times" in msg

    def test_network_timeout_message(self):
        """Test network message for timeout."""
        exc = Exception("Request timed out")
        msg = _format_network_message(exc)
        msg_lower = msg.lower()
        assert "timeout" in msg_lower or "timed out" in msg_lower
        assert "network" in msg_lower or "connection" in msg_lower

    def test_network_refused_message(self):
        """Test network message for connection refused."""
        exc = Exception("Connection refused")
        msg = _format_network_message(exc)
        assert "refused" in msg.lower()

    def test_network_unreachable_message(self):
        """Test network message for unreachable."""
        exc = Exception("Network unreachable")
        msg = _format_network_message(exc)
        assert "unreachable" in msg.lower()

    def test_network_generic_message(self):
        """Test generic network error message."""
        exc = Exception("Connection error")
        msg = _format_network_message(exc)
        assert "connection" in msg.lower()


class TestErrorClassificationPriority:
    """Tests for error classification priority order."""

    def test_429_takes_priority_over_4xx(self):
        """Test that 429 is classified as RateLimitError, not ClientError."""
        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.headers = {}

        mock_exception = Mock()
        mock_exception.response = mock_response

        error = classify_error(mock_exception, retry_count=1)
        assert error.type == "RateLimitError"

    def test_401_takes_priority_over_4xx(self):
        """Test that 401 is classified as AuthError, not ClientError."""
        mock_response = Mock()
        mock_response.status_code = 401

        mock_exception = Mock()
        mock_exception.response = mock_response

        error = classify_error(mock_exception, retry_count=1)
        assert error.type == "AuthError"

    def test_404_takes_priority_over_4xx(self):
        """Test that 404 is classified as ModelError, not ClientError."""
        mock_response = Mock()
        mock_response.status_code = 404

        mock_exception = Mock()
        mock_exception.response = mock_response

        error = classify_error(mock_exception, retry_count=1)
        assert error.type == "ModelError"


class TestEdgeCases:
    """Tests for edge cases and unusual scenarios."""

    def test_retry_count_zero(self):
        """Test handling of zero retry count."""
        error = classify_error(
            Exception("Error"),
            retry_count=0,
        )
        assert error.retry_count == 0

    def test_very_large_retry_after(self):
        """Test handling of very large retry-after value."""
        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.headers = {"retry-after": "3600"}

        mock_exception = Mock()
        mock_exception.response = mock_response

        error = classify_error(mock_exception, retry_count=1)
        assert error.retry_after == 3600.0

    def test_empty_model_name(self):
        """Test handling of empty model name."""
        mock_response = Mock()
        mock_response.status_code = 404

        mock_exception = Mock()
        mock_exception.response = mock_response

        error = classify_error(
            mock_exception,
            retry_count=1,
            model="",
        )
        assert error.type == "ModelError"
        assert "requested model" in error.message

    def test_long_exception_message(self):
        """Test handling of very long exception messages."""
        long_message = "x" * 10000
        error = classify_error(
            Exception(long_message),
            retry_count=1,
        )
        assert error.raw_error == long_message
        assert error.type == "UnknownError"
