"""Claude API client using raw httpx with prompt caching support.

Uses httpx directly instead of the anthropic SDK for control over
prompt caching headers and request construction.
"""

import json
import logging
import random
import time

import httpx

from ... import config
from .. import circuit_breaker, rate_limiter

logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4096


def get_api_key() -> str:
    """Get Claude API key from config."""
    return config.CONFIG.get("claude_api_key", "")


def _count_cache_control_blocks(messages: list[dict], system_content: list[dict]) -> int:
    """Count existing cache_control blocks in messages and system.

    Anthropic allows max 4 cache_control blocks total across system + messages.
    """
    count = 0

    # Count in system
    for block in system_content:
        if isinstance(block, dict) and "cache_control" in block:
            count += 1

    # Count in messages
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    count += 1

    return count


def build_messages(system: str, conversation: list[dict], cache_control: bool = True) -> tuple[list[dict], dict]:
    """Build the messages array and system prompt for the API call.

    Adds cache_control breakpoints to:
    1. System prompt — caches the static system instructions
    2. Penultimate user turn — caches prior conversation history

    The API caches everything up to and including each breakpoint.
    With system + tools + conversation history cached, only the last
    user message and new assistant response are uncached.

    NOTE: Anthropic API limits cache_control to 4 blocks total. We count
    existing blocks and skip adding more if we would exceed the limit.

    Args:
        system: System prompt text.
        conversation: List of message dicts with 'role' and 'content'.
        cache_control: Whether to add cache_control markers.

    Returns:
        Tuple of (messages list, system content dict).
    """
    # System prompt with cache_control for prefix caching
    system_content = [{"type": "text", "text": system}]
    if cache_control:
        system_content[0]["cache_control"] = {"type": "ephemeral"}

    messages = []
    for msg in conversation:
        content = msg["content"]

        # Strip any existing cache_control markers from previous API calls
        # We'll add them fresh according to our strategy
        if isinstance(content, list):
            cleaned_content = []
            for block in content:
                if isinstance(block, dict):
                    # Make a copy and remove cache_control if present
                    cleaned_block = {k: v for k, v in block.items() if k != "cache_control"}
                    cleaned_content.append(cleaned_block)
                else:
                    cleaned_content.append(block)
            content = cleaned_content

        messages.append({
            "role": msg["role"],
            "content": content,
        })

    # Add cache breakpoint to penultimate user turn so prior history is cached.
    # We target the second-to-last user message: this caches system + tools +
    # all conversation up to that point. Only the final user message and new
    # assistant response are uncached.
    if cache_control and len(messages) >= 4:
        # Find the penultimate user message (walking backwards)
        user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
        if len(user_indices) >= 2:
            target_idx = user_indices[-2]
            content = messages[target_idx]["content"]

            # Check if adding this cache_control would exceed limit (4 max)
            # Count: system (1) + tools (1, added later in call()) + this message = 3
            # Leave room for 1 more (4 total)
            current_count = _count_cache_control_blocks(messages, system_content)

            # Only add if we have budget (max 4, and tools will add 1)
            if current_count < 3:  # system=1, tools=1 (added later), this=1 → 3 total
                # Wrap string content in a content block to attach cache_control
                if isinstance(content, str):
                    messages[target_idx]["content"] = [
                        {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}},
                    ]
                elif isinstance(content, list):
                    # Structured content (tool_result blocks etc.) — add to last block
                    # But first remove any existing cache_control in this message's blocks
                    # to avoid double-counting
                    for block in content:
                        if isinstance(block, dict) and "cache_control" in block:
                            del block["cache_control"]

                    # Now add cache_control to the last block
                    if content:
                        last_block = content[-1]
                        if isinstance(last_block, dict):
                            last_block["cache_control"] = {"type": "ephemeral"}

    return messages, system_content


def call(
    system: str,
    messages: list[dict],
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.7,
    api_key: str | None = None,
    tools: list[dict] | None = None,
    tool_choice: dict | None = None,
) -> dict:
    """Make a synchronous call to the Claude API.

    Args:
        system: System prompt text.
        messages: Conversation messages.
        model: Model to use (defaults to config or claude-sonnet-4-20250514).
        max_tokens: Max tokens to generate.
        temperature: Sampling temperature.
        api_key: API key override.
        tools: Optional list of tool definitions for tool_use.

    Returns:
        Raw API response dict.

    Raises:
        httpx.HTTPStatusError: On API errors.
    """
    key = api_key or get_api_key()
    model = model or config.CONFIG.get("claude_model", DEFAULT_MODEL)
    max_tokens = max_tokens or config.CONFIG.get("claude_max_tokens", DEFAULT_MAX_TOKENS)

    msgs, system_content = build_messages(system, messages)

    headers = {
        "x-api-key": key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system_content,
        "messages": msgs,
    }

    if tool_choice:
        body["tool_choice"] = tool_choice

    if tools:
        # Deep-copy the last tool to add cache_control without mutating the
        # shared tool definitions list.
        cached_tools = list(tools)
        cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}
        body["tools"] = cached_tools

        # Validate total cache_control blocks don't exceed 4
        # Count: system + tools + messages
        total_cache_blocks = _count_cache_control_blocks(msgs, system_content) + 1  # +1 for tools
        if total_cache_blocks > 4:
            logger.warning(
                "Too many cache_control blocks (%d > 4), removing from penultimate message",
                total_cache_blocks
            )
            # Remove cache_control from penultimate user message to stay under limit
            user_indices = [i for i, m in enumerate(msgs) if m["role"] == "user"]
            if len(user_indices) >= 2:
                target_idx = user_indices[-2]
                content = msgs[target_idx]["content"]
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and "cache_control" in block:
                            del block["cache_control"]

    breaker = circuit_breaker.get_breaker("anthropic")
    max_attempts = config.CONFIG.get("retry_max_attempts", 3)
    base_delay = config.CONFIG.get("retry_base_delay", 1.0)

    last_error = None
    for attempt in range(max_attempts):
        if not breaker.allow_request():
            raise circuit_breaker.CircuitOpenError(
                f"Circuit breaker open for anthropic after {breaker.failure_count} failures")

        # Proactive rate limiting — block until a slot is available
        if not rate_limiter.acquire(model=model):
            raise RuntimeError("Rate limiter timed out waiting for API slot")

        try:
            # Sanitize surrogates that may have leaked from upstream backends
            body_json = json.dumps(body, ensure_ascii=False)
            body_bytes = body_json.encode("utf-8", errors="replace")
            response = httpx.post(
                API_URL,
                headers={**headers, "Content-Type": "application/json"},
                content=body_bytes,
                timeout=120.0,
            )
            response.raise_for_status()

            result = response.json()

            # Update rate limiter from API response headers
            rate_limiter.update_from_headers(response.headers, model=model)

            # Log usage for cost tracking
            usage = result.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            logger.info(
                "Claude API call: model=%s, input_tokens=%d, output_tokens=%d, "
                "cache_read=%d, cache_creation=%d",
                model,
                input_tokens,
                usage.get("output_tokens", 0),
                usage.get("cache_read_input_tokens", 0),
                usage.get("cache_creation_input_tokens", 0),
            )

            # Feed actual token count back to rate limiter for ITPM tracking
            rate_limiter.record(input_tokens, model=model)

            breaker.record_success()
            return result

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                rate_limiter.record_429(model=model)
                last_error = e
                logger.warning(
                    "Claude API 429 rate limited (attempt %d/%d)",
                    attempt + 1, max_attempts)
            elif e.response.status_code >= 500:
                breaker.record_failure()
                last_error = e
                logger.warning(
                    "Claude API %d server error (attempt %d/%d)",
                    e.response.status_code, attempt + 1, max_attempts)
            else:
                # 4xx errors (except 429) - log the error body for debugging
                try:
                    error_body = e.response.json()
                    logger.error("Claude API 4xx error: status=%d, body=%s",
                                e.response.status_code, error_body)
                except (ValueError, KeyError) as _exc:
                    logger.error("Claude API 4xx error: status=%d, body=%s",
                                e.response.status_code, e.response.text)
                raise  # 4xx (except 429) are not retryable

        except (httpx.ConnectError, httpx.TimeoutException) as e:
            breaker.record_failure()
            last_error = e
            logger.warning(
                "Claude API connection error (attempt %d/%d): %s",
                attempt + 1, max_attempts, e)

        if attempt < max_attempts - 1:
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            time.sleep(delay)

    raise last_error


def extract_text(response: dict) -> str:
    """Extract the text content from a Claude API response."""
    content = response.get("content", [])
    texts = [block["text"] for block in content if block.get("type") == "text"]
    return "\n".join(texts)


def extract_code(response: dict) -> str | None:
    """Extract Python code from a Claude API response.

    Looks for code blocks marked with ```python ... ```.
    Returns the code text, or None if no code block found.
    """
    text = extract_text(response)
    return extract_code_from_text(text)


def extract_code_from_text(text: str) -> str | None:
    """Extract Python code from a text string.

    Delegates to the shared implementation in api_standard.
    """
    from .. import api_standard
    return api_standard.extract_code_from_text(text)
