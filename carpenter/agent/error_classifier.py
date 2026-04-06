"""Error classification for AI API failures.

Inspects exceptions from API calls and classifies them into semantic categories
with user-friendly messages and structured metadata for logging and analytics.
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any


@dataclass
class ErrorInfo:
    """Structured error information for API failures.

    Attributes:
        type: Error category (RateLimitError, APIOutageError, NetworkError, etc.)
        retry_count: Number of retry attempts made before failure
        source_location: Where the error occurred (e.g., "invocation._call_with_retries")
        message: User-facing error message (actionable and clear)
        status_code: HTTP status code if applicable
        retry_after: Seconds to wait before retrying (from retry-after header)
        model: Model that was being called (e.g., "claude-3-5-sonnet-20241022")
        provider: Provider name (e.g., "anthropic", "ollama", "chain")
        raw_error: String representation of the original exception
        timestamp: ISO 8601 timestamp when error was classified
    """
    type: str
    retry_count: int
    source_location: str
    message: str
    status_code: int | None = None
    retry_after: float | None = None
    model: str | None = None
    provider: str | None = None
    raw_error: str | None = None
    timestamp: str | None = None

    def __post_init__(self):
        """Set timestamp to current UTC time if not provided."""
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_json(self) -> dict:
        """Convert to JSON-serializable dict for content_json field.

        Returns:
            Dict with error_info key containing all non-None fields.
        """
        # Filter out None values for cleaner JSON
        data = {k: v for k, v in asdict(self).items() if v is not None}
        return {"error_info": data}


def classify_error(
    exception: Exception,
    retry_count: int,
    model: str | None = None,
    provider: str | None = None,
) -> ErrorInfo:
    """Classify an API exception into a semantic error category.

    Inspects the exception type, HTTP status codes, and error messages to
    determine the error category and generate an appropriate user message.

    Classification order (first match wins):
    1. HTTP 429 → RateLimitError (extract retry-after header)
    2. HTTP 5xx or CircuitOpenError → APIOutageError
    3. ConnectError or TimeoutException → NetworkError
    4. HTTP 401/403 → AuthError
    5. HTTP 404 → ModelError
    6. Other HTTP 4xx → ClientError
    7. All others → UnknownError

    Args:
        exception: The caught exception from the API call
        retry_count: Number of attempts made (1-indexed)
        model: Model name that was being called (optional)
        provider: Provider name (optional)

    Returns:
        ErrorInfo with classification and user message
    """
    # Extract status code if present
    status_code = _extract_status_code(exception)
    raw_error = str(exception)

    # 1. Rate limit error (HTTP 429)
    if status_code == 429:
        retry_after = _extract_retry_after(exception)
        return ErrorInfo(
            type="RateLimitError",
            retry_count=retry_count,
            source_location="invocation._call_with_retries",
            message=_format_rate_limit_message(retry_after, retry_count),
            status_code=status_code,
            retry_after=retry_after,
            model=model,
            provider=provider,
            raw_error=raw_error,
        )

    # 2. API outage (5xx or circuit breaker)
    if status_code and 500 <= status_code < 600:
        return ErrorInfo(
            type="APIOutageError",
            retry_count=retry_count,
            source_location="invocation._call_with_retries",
            message=_format_outage_message(retry_count, is_circuit_open=False),
            status_code=status_code,
            model=model,
            provider=provider,
            raw_error=raw_error,
        )

    # Check for circuit breaker by exception type name
    exc_type_name = type(exception).__name__
    if "CircuitOpenError" in exc_type_name or "CircuitBreakerError" in exc_type_name:
        return ErrorInfo(
            type="APIOutageError",
            retry_count=retry_count,
            source_location="invocation._call_with_retries",
            message=_format_outage_message(retry_count, is_circuit_open=True),
            model=model,
            provider=provider,
            raw_error=raw_error,
        )

    # 3. Network errors (connection/timeout)
    if _is_network_error(exception):
        return ErrorInfo(
            type="NetworkError",
            retry_count=retry_count,
            source_location="invocation._call_with_retries",
            message=_format_network_message(exception),
            model=model,
            provider=provider,
            raw_error=raw_error,
        )

    # 4. Authentication errors (401/403)
    if status_code in (401, 403):
        return ErrorInfo(
            type="AuthError",
            retry_count=retry_count,
            source_location="invocation._call_with_retries",
            message="Authentication failed. Please check your API credentials.",
            status_code=status_code,
            model=model,
            provider=provider,
            raw_error=raw_error,
        )

    # 5. Model not found (404)
    if status_code == 404:
        model_name = model or "requested model"
        return ErrorInfo(
            type="ModelError",
            retry_count=retry_count,
            source_location="invocation._call_with_retries",
            message=f"Model '{model_name}' not available. Please try a different model.",
            status_code=status_code,
            model=model,
            provider=provider,
            raw_error=raw_error,
        )

    # 6. Other client errors (4xx)
    if status_code and 400 <= status_code < 500:
        return ErrorInfo(
            type="ClientError",
            retry_count=retry_count,
            source_location="invocation._call_with_retries",
            message=f"API request error (HTTP {status_code}). This may indicate a configuration issue.",
            status_code=status_code,
            model=model,
            provider=provider,
            raw_error=raw_error,
        )

    # 7. Unknown error (fallback)
    return ErrorInfo(
        type="UnknownError",
        retry_count=retry_count,
        source_location="invocation._call_with_retries",
        message=f"Unexpected error: {exc_type_name}. Please contact support if this persists.",
        model=model,
        provider=provider,
        raw_error=raw_error,
    )


def _extract_status_code(exception: Exception) -> int | None:
    """Extract HTTP status code from exception if available.

    Handles multiple common patterns:
    - httpx.HTTPStatusError: exception.response.status_code
    - Generic: exception.status_code attribute
    - Response object: exception.response.status_code

    Args:
        exception: The exception to inspect

    Returns:
        Status code or None if not found
    """
    # Check for direct status_code attribute first (avoid Mock issues)
    if hasattr(exception, 'status_code'):
        status_code = getattr(exception, 'status_code')
        # Make sure it's actually an int, not a Mock or other object
        if isinstance(status_code, int):
            return status_code

    # Check for httpx HTTPStatusError pattern
    if hasattr(exception, 'response'):
        response = getattr(exception, 'response')
        if hasattr(response, 'status_code'):
            status_code = getattr(response, 'status_code')
            if isinstance(status_code, int):
                return status_code

    return None


def _extract_retry_after(exception: Exception) -> float:
    """Extract retry-after header value from 429 response.

    Args:
        exception: The rate limit exception

    Returns:
        Seconds to wait (defaults to 5.0 if header not found)
    """
    if hasattr(exception, 'response'):
        response = getattr(exception, 'response')
        if hasattr(response, 'headers'):
            headers = getattr(response, 'headers')
            # Headers might be dict-like or have .get() method
            if hasattr(headers, 'get'):
                retry_after_str = headers.get('retry-after') or headers.get('Retry-After')
                if retry_after_str:
                    try:
                        return float(retry_after_str)
                    except (ValueError, TypeError):
                        pass

    return 5.0  # Default fallback


def _is_network_error(exception: Exception) -> bool:
    """Check if exception is a network-related error.

    Identifies connection failures, timeouts, and DNS errors.

    Args:
        exception: The exception to check

    Returns:
        True if network error
    """
    exc_type_name = type(exception).__name__
    exc_str = str(exception).lower()

    # Common network error patterns (type names)
    type_indicators = [
        'ConnectError',
        'ConnectionError',
        'TimeoutError',
        'TimeoutException',
        'ConnectTimeout',
        'ReadTimeout',
    ]

    # Common network error patterns (message text)
    message_indicators = [
        'timeout',
        'timed out',
        'connection refused',
        'network unreachable',
        'name or service not known',  # DNS error
        'temporary failure in name resolution',
    ]

    # Check type name
    if any(indicator in exc_type_name for indicator in type_indicators):
        return True

    # Check message text
    if any(indicator in exc_str for indicator in message_indicators):
        return True

    return False


def _format_rate_limit_message(retry_after: float, retry_count: int) -> str:
    """Format user-friendly rate limit message.

    Args:
        retry_after: Seconds to wait
        retry_count: Number of attempts made

    Returns:
        Message like "API rate limit reached. Retrying in 30 seconds..." or
        "API rate limit reached after 4 attempts. Please wait before retrying."
    """
    if retry_count == 1:
        return f"API rate limit reached. Retrying in {int(retry_after)} seconds..."
    else:
        return f"API rate limit reached after {retry_count} attempts. Please wait before retrying."


def _format_outage_message(retry_count: int, is_circuit_open: bool) -> str:
    """Format user-friendly outage message.

    Args:
        retry_count: Number of attempts made
        is_circuit_open: Whether error is from circuit breaker

    Returns:
        Message distinguishing circuit breaker vs server error
    """
    if is_circuit_open:
        return (
            f"AI service circuit breaker activated after {retry_count} attempts. "
            "Service may be experiencing issues. Please try again later."
        )
    else:
        return (
            f"AI service temporarily unavailable (retried {retry_count} times). "
            "The service may be experiencing issues. Please try again later."
        )


def _format_network_message(exception: Exception) -> str:
    """Format user-friendly network error message.

    Args:
        exception: The network exception

    Returns:
        Message distinguishing timeout vs connection failure
    """
    exc_str = str(exception).lower()

    if 'timeout' in exc_str or 'timed out' in exc_str:
        return "Request timed out. Please check your network connection and try again."
    elif 'refused' in exc_str:
        return "Connection refused. The service may be down or unreachable."
    elif 'unreachable' in exc_str or 'resolution' in exc_str:
        return "Network unreachable. Please check your internet connection."
    else:
        return "Network connection error. Please check your connection and try again."
