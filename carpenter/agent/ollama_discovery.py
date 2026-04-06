"""Ollama model discovery.

Queries an Ollama server's /api/tags endpoint to discover available models.
Uses constrained extraction: only name (str) and size (int) are kept,
unexpected fields are discarded.
"""

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://localhost:11434"
_MAX_NAME_LEN = 200
_MAX_MODELS = 500


@dataclass(frozen=True)
class OllamaModel:
    """A discovered Ollama model."""
    name: str
    size: int


def discover_models(url: str | None = None, timeout: float = 10.0) -> list[OllamaModel]:
    """Discover models available on an Ollama server.

    Calls GET /api/tags and extracts model entries with constrained parsing:
    only ``name`` (str, truncated to 200 chars) and ``size`` (int) are kept.
    Non-string names and non-int sizes are skipped. At most 500 models returned.

    Args:
        url: Ollama server base URL (defaults to http://localhost:11434).
        timeout: HTTP request timeout in seconds.

    Returns:
        List of OllamaModel instances.

    Raises:
        httpx.ConnectError: If the server is unreachable.
        httpx.TimeoutException: If the request times out.
        httpx.HTTPStatusError: On non-200 responses.
    """
    base_url = url or _DEFAULT_URL
    response = httpx.get(f"{base_url}/api/tags", timeout=timeout)
    response.raise_for_status()

    data = response.json()
    raw_models = data.get("models", [])
    if not isinstance(raw_models, list):
        return []

    result: list[OllamaModel] = []
    for entry in raw_models:
        if not isinstance(entry, dict):
            continue

        name = entry.get("name")
        if not isinstance(name, str):
            continue
        name = name[:_MAX_NAME_LEN]

        size = entry.get("size", 0)
        if not isinstance(size, int):
            continue

        result.append(OllamaModel(name=name, size=size))
        if len(result) >= _MAX_MODELS:
            break

    return result


def check_health(url: str | None = None, timeout: float = 5.0) -> bool:
    """Check if an Ollama server is reachable.

    Args:
        url: Ollama server base URL.
        timeout: HTTP request timeout in seconds.

    Returns:
        True if the server responds with HTTP 200 to /api/tags.
    """
    base_url = url or _DEFAULT_URL
    try:
        response = httpx.get(f"{base_url}/api/tags", timeout=timeout)
        return response.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException, OSError):
        return False


def find_model(
    model_name: str, url: str | None = None, timeout: float = 10.0
) -> OllamaModel | None:
    """Find a specific model on an Ollama server.

    Args:
        model_name: Model name to search for (exact match).
        url: Ollama server base URL.
        timeout: HTTP request timeout in seconds.

    Returns:
        OllamaModel if found, None otherwise.
    """
    models = discover_models(url=url, timeout=timeout)
    for m in models:
        if m.name == model_name:
            return m
    return None
