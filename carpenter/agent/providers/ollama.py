"""Ollama API client using the OpenAI-compatible endpoint.

Calls Ollama's /v1/chat/completions endpoint via httpx.
No API key needed since Ollama runs as a local service.
"""

import json
import logging

import httpx

from ... import config
from .retry import build_openai_messages, retry_with_breaker

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.1"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 300.0


def get_api_url() -> str:
    """Get the Ollama API base URL from config."""
    return config.CONFIG.get("ollama_url", DEFAULT_URL)


def get_model() -> str:
    """Get the Ollama model name from config."""
    return config.CONFIG.get("ollama_model", DEFAULT_MODEL)


def call(
    system: str,
    messages: list[dict],
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.7,
    tools: list[dict] | None = None,
) -> dict:
    """Make a synchronous call to the Ollama OpenAI-compatible API.

    Args:
        system: System prompt text.
        messages: Conversation messages (list of role/content dicts).
        model: Model to use (defaults to config or llama3.1).
        max_tokens: Max tokens to generate.
        temperature: Sampling temperature.
        tools: Optional tool definitions in OpenAI function-calling format.

    Returns:
        Raw API response dict in OpenAI format.

    Raises:
        httpx.HTTPStatusError: On API errors.
        httpx.TimeoutException: On timeout.
    """
    base_url = get_api_url()
    model = model or get_model()
    max_tokens = max_tokens or config.CONFIG.get("ollama_max_tokens", DEFAULT_MAX_TOKENS)

    api_messages = build_openai_messages(system, messages)

    body = {
        "model": model,
        "messages": api_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    if tools:
        body["tools"] = tools

    url = f"{base_url}/v1/chat/completions"

    def _do_request() -> dict:
        # Serialize to JSON and sanitize any surrogate characters
        # that may have been introduced by upstream LLM backends.
        body_json = json.dumps(body, ensure_ascii=False)
        body_bytes = body_json.encode("utf-8", errors="replace")
        response = httpx.post(
            url,
            content=body_bytes,
            headers={"Content-Type": "application/json"},
            timeout=DEFAULT_TIMEOUT,
        )
        response.raise_for_status()

        result = response.json()

        # Log usage for tracking
        usage = result.get("usage", {})
        logger.info(
            "Ollama API call: model=%s, prompt_tokens=%d, completion_tokens=%d, "
            "total_tokens=%d",
            model,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            usage.get("total_tokens", 0),
        )
        return result

    return retry_with_breaker("ollama", _do_request)


def extract_text(response: dict) -> str:
    """Extract the text content from an OpenAI-format response."""
    choices = response.get("choices", [])
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content", "")


def extract_code(response: dict) -> str | None:
    """Extract Python code from an OpenAI-format response.

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
