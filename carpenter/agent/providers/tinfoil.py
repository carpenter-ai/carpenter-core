"""Tinfoil AI client using the secure enclave inference API.

Wraps the tinfoil Python SDK, which provides TLS attestation verification
on every connection to Tinfoil's secure enclave inference service.

Requires: pip install tinfoil
See: https://docs.tinfoil.sh/sdk/overview
"""

import logging
import os

from ... import config
from .retry import build_openai_messages, retry_with_breaker

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "llama3-3-70b"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.7


def get_model() -> str:
    """Get the Tinfoil model name from config."""
    return config.CONFIG.get("tinfoil_model", DEFAULT_MODEL)


def _get_api_key() -> str:
    """Get the Tinfoil API key.

    Checks TINFOIL_API_KEY environment variable first (standard .env convention),
    then falls back to the tinfoil_api_key config key (set via credential files).
    """
    return os.environ.get("TINFOIL_API_KEY") or config.CONFIG.get("tinfoil_api_key", "")


def call(
    system: str,
    messages: list[dict],
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    tools: list[dict] | None = None,
) -> dict:
    """Make a synchronous call to Tinfoil's secure inference API.

    The tinfoil SDK performs TLS attestation verification on every connection,
    ensuring the request reaches Tinfoil's secure enclave.

    Args:
        system: System prompt text.
        messages: Conversation messages (list of role/content dicts).
        model: Model to use (defaults to config or llama3-3-70b).
        max_tokens: Max tokens to generate.
        temperature: Sampling temperature.
        tools: Optional tool definitions in OpenAI function-calling format.

    Returns:
        Raw API response dict in OpenAI format.

    Raises:
        ImportError: If the 'tinfoil' package is not installed.
        ValueError: If tinfoil_api_key is not configured.
        Exception: On API errors.
    """
    try:
        from tinfoil import TinfoilAI
    except ImportError as exc:
        raise ImportError(
            "The 'tinfoil' package is required for the Tinfoil AI provider. "
            "Install it with: pip install tinfoil"
        ) from exc

    api_key = _get_api_key()
    if not api_key:
        raise ValueError(
            "tinfoil_api_key is not configured. Set it in config.yaml or via "
            "TINFOIL_API_KEY environment variable."
        )

    model = model or get_model()
    max_tokens = max_tokens or config.CONFIG.get("tinfoil_max_tokens", DEFAULT_MAX_TOKENS)

    api_messages = build_openai_messages(system, messages)

    def _do_request() -> dict:
        client = TinfoilAI(api_key=api_key)
        kwargs = {
            "model": model,
            "messages": api_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
        response = client.chat.completions.create(**kwargs)

        # Convert Pydantic response object to plain dict
        result = response.model_dump()

        usage = result.get("usage") or {}
        logger.info(
            "Tinfoil API call: model=%s, prompt_tokens=%d, completion_tokens=%d, "
            "total_tokens=%d",
            model,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            usage.get("total_tokens", 0),
        )
        return result

    return retry_with_breaker("tinfoil", _do_request)


def extract_text(response: dict) -> str:
    """Extract the text content from an OpenAI-format response."""
    choices = response.get("choices", [])
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content", "") or ""


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
