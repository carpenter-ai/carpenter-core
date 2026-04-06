"""Shared retry and circuit-breaker logic for provider modules.

Encapsulates the retry loop, exponential backoff with jitter, and
circuit-breaker integration that was previously duplicated across the
ollama, local, and tinfoil provider modules.

The anthropic provider has additional rate-limiter integration and
429-specific handling, so it keeps its own retry loop but could be
migrated in the future.
"""

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

import httpx

from ... import config
from .. import circuit_breaker

logger = logging.getLogger(__name__)

T = TypeVar("T")


def build_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """Build an OpenAI-format messages array with a system prompt.

    Converts the canonical message list into the format expected by
    OpenAI-compatible APIs (Ollama, llama.cpp, Tinfoil, etc.),
    preserving tool-call threading.

    Args:
        system: System prompt text.
        messages: Conversation messages (list of role/content dicts).

    Returns:
        List of message dicts with system prompt prepended.
    """
    api_messages = [{"role": "system", "content": system}]
    for msg in messages:
        role = msg["role"]
        content = msg.get("content")
        entry: dict = {"role": role}
        # Pass through structured content for tool-call message threading
        if role == "assistant" and "tool_calls" in msg:
            entry["content"] = content
            entry["tool_calls"] = msg["tool_calls"]
        elif role == "tool":
            entry["tool_call_id"] = msg.get("tool_call_id", "")
            entry["content"] = content if isinstance(content, str) else str(content)
        else:
            entry["content"] = content
        api_messages.append(entry)
    return api_messages


def retry_with_breaker(
    provider_name: str,
    fn: Callable[[], T],
    *,
    max_attempts: int | None = None,
    base_delay: float | None = None,
    is_retryable: Callable[[Exception], bool] | None = None,
) -> T:
    """Execute *fn* with retry, exponential backoff, and circuit-breaker.

    This is the shared retry loop used by the ollama, local, and tinfoil
    providers. On each attempt it:

    1. Checks the circuit breaker -- raises CircuitOpenError if open.
    2. Calls *fn()*.
    3. On success, records success on the breaker and returns the result.
    4. On failure, classifies the error:
       - If *is_retryable* returns False, re-raises immediately.
       - Otherwise records a breaker failure and retries after backoff.
    5. After exhausting all attempts, re-raises the last error.

    Args:
        provider_name: Key for the circuit breaker (e.g. "ollama").
        fn: Zero-argument callable that performs the actual API call.
        max_attempts: Override for retry_max_attempts config (default 3).
        base_delay: Override for retry_base_delay config (default 1.0).
        is_retryable: Predicate that returns True if the exception is
            transient and should be retried. Defaults to
            :func:`default_is_retryable`.

    Returns:
        The return value of *fn* on success.

    Raises:
        circuit_breaker.CircuitOpenError: If the breaker is open.
        Exception: The last error after all retries are exhausted, or
            a non-retryable error on the first occurrence.
    """
    if max_attempts is None:
        max_attempts = config.CONFIG.get("retry_max_attempts", 3)
    if base_delay is None:
        base_delay = config.CONFIG.get("retry_base_delay", 1.0)
    if is_retryable is None:
        is_retryable = default_is_retryable

    breaker = circuit_breaker.get_breaker(provider_name)

    last_error: Exception | None = None
    for attempt in range(max_attempts):
        if not breaker.allow_request():
            raise circuit_breaker.CircuitOpenError(
                f"Circuit breaker open for {provider_name} "
                f"after {breaker.failure_count} failures"
            )

        try:
            result = fn()
            breaker.record_success()
            return result

        except Exception as exc:
            if not is_retryable(exc):
                raise

            breaker.record_failure()
            last_error = exc
            logger.warning(
                "%s API error (attempt %d/%d): %s",
                provider_name.capitalize(),
                attempt + 1,
                max_attempts,
                exc,
            )

        if attempt < max_attempts - 1:
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            time.sleep(delay)

    raise last_error  # type: ignore[misc]


def default_is_retryable(exc: Exception) -> bool:
    """Classify whether an exception is transient (retryable).

    - httpx.HTTPStatusError with status >= 500: retryable (server error).
    - httpx.HTTPStatusError with 4xx: NOT retryable (client error).
    - httpx.ConnectError, httpx.TimeoutException: retryable.
    - ImportError: NOT retryable.
    - Any other exception with a ``status_code`` attribute in the
      400-499 range: NOT retryable (covers SDK-raised errors like
      those from the tinfoil client).
    - All other exceptions: retryable (assume transient).
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500

    if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
        return True

    if isinstance(exc, ImportError):
        return False

    # SDK-raised errors may carry a status_code attribute
    status = getattr(exc, "status_code", None)
    if status is not None and 400 <= status < 500:
        return False

    return True
