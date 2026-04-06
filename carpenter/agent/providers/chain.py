"""Inference chain client with failover.

Provides a configurable failover chain of AI backends. When ``ai_provider``
is set to ``"chain"``, the ``inference_chain`` config list is iterated in
order. Each backend is tried; on ConnectError, TimeoutException, or 5xx
the next backend is attempted. 4xx errors propagate immediately (client
error, not transient).

Each chain entry has its own circuit breaker keyed by ``entry.name``.

Example config::

    ai_provider: chain
    inference_chain:
      - name: desktop-ollama
        provider: ollama
        url: "http://192.168.2.243:11434"
        model: "qwen3.5:9b"
        context_window: 16384
        timeout: 300
      - name: claude-haiku
        provider: anthropic
        model: "claude-haiku-4-5-20251001"
        context_window: 200000
        timeout: 120
"""

import json
import logging
from dataclasses import dataclass

import httpx

from ... import config
from .. import api_standard, circuit_breaker
from . import anthropic as claude_client

logger = logging.getLogger(__name__)


@dataclass
class ChainEntry:
    """A single backend in the inference chain."""
    name: str
    provider: str       # ollama | anthropic | tinfoil | local
    model: str
    url: str            # base URL (empty for anthropic)
    context_window: int
    timeout: float


def load_chain() -> list[ChainEntry]:
    """Parse the ``inference_chain`` config into ChainEntry objects.

    Returns:
        List of ChainEntry instances.

    Raises:
        ValueError: If the chain is empty or entries are malformed.
    """
    raw = config.CONFIG.get("inference_chain", [])
    if not raw:
        raise ValueError(
            "inference_chain config is empty. Set ai_provider to a single "
            "provider or populate the inference_chain list."
        )

    entries: list[ChainEntry] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"inference_chain[{i}]: expected dict, got {type(entry).__name__}")

        name = entry.get("name", f"chain-{i}")
        provider = entry.get("provider", "")
        if not provider:
            raise ValueError(f"inference_chain[{i}] ({name}): 'provider' is required")

        model = entry.get("model", "")
        if not model:
            raise ValueError(f"inference_chain[{i}] ({name}): 'model' is required")

        entries.append(ChainEntry(
            name=str(name),
            provider=str(provider),
            model=str(model),
            url=str(entry.get("url", "")),
            context_window=int(entry.get("context_window", 16384)),
            timeout=float(entry.get("timeout", 300)),
        ))

    return entries


def _call_single_backend(
    entry: ChainEntry,
    system: str,
    messages: list[dict],
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.7,
    tools: list[dict] | None = None,
) -> dict:
    """Call a single backend in the chain.

    For anthropic entries, delegates to claude_client.call().
    For openai-compat entries (ollama/local/tinfoil), builds the HTTP
    request inline to avoid fighting each client's own retry/breaker layer.

    Messages arrive in canonical (Anthropic) format and are converted
    per-backend as needed.

    Returns:
        Raw API response dict.

    Raises:
        httpx.HTTPStatusError: On API errors.
        httpx.ConnectError: If the backend is unreachable.
        httpx.TimeoutException: On timeout.
    """
    use_model = model or entry.model

    if entry.provider == "anthropic":
        # Messages are already in Anthropic format, pass through
        return claude_client.call(
            system, messages,
            model=use_model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
        )

    # OpenAI-compatible backend (ollama, local, tinfoil)
    # Convert messages from Anthropic to OpenAI format
    from .. import api_standard as _api_std
    standard = _api_std.get_api_standard(entry.provider)

    # Need parallel message ID list for conversion (dummy list since we don't track IDs here)
    dummy_ids = [None] * len(messages)
    from .. import invocation as _inv
    converted_messages, _ = _inv._convert_history_to_standard(messages, standard, dummy_ids)

    base_url = entry.url or "http://localhost:11434"

    api_messages = [{"role": "system", "content": system}]
    for msg in converted_messages:
        role = msg["role"]
        content = msg.get("content")
        api_entry = {"role": role}
        if role == "assistant" and "tool_calls" in msg:
            api_entry["content"] = content
            api_entry["tool_calls"] = msg["tool_calls"]
        elif role == "tool":
            api_entry["tool_call_id"] = msg.get("tool_call_id", "")
            api_entry["content"] = content if isinstance(content, str) else str(content)
        else:
            api_entry["content"] = content
        api_messages.append(api_entry)

    body: dict = {
        "model": use_model,
        "messages": api_messages,
        "max_tokens": max_tokens or 4096,
        "temperature": temperature,
    }
    if tools:
        body["tools"] = tools

    url = f"{base_url}/v1/chat/completions"
    # Sanitize surrogate characters that upstream backends may introduce.
    body_json = json.dumps(body, ensure_ascii=False)
    body_bytes = body_json.encode("utf-8", errors="replace")
    response = httpx.post(
        url, content=body_bytes,
        headers={"Content-Type": "application/json"},
        timeout=entry.timeout,
    )
    response.raise_for_status()
    return response.json()


def call(
    system: str,
    messages: list[dict],
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.7,
    tools: list[dict] | None = None,
) -> dict:
    """Call backends in chain order with failover.

    Iterates chain entries. Skips entries whose circuit breaker is open.
    On ConnectError, TimeoutException, or 5xx, tries the next entry.
    4xx errors propagate immediately. If all backends fail, re-raises
    the last error.

    Tools arrive in canonical (Anthropic) format. Before calling each
    backend, tools are converted to the entry's provider format.

    The response dict gets an ``_api_standard`` key injected so the
    caller can normalize correctly.

    Returns:
        Raw API response dict with ``_api_standard`` tag.

    Raises:
        The last exception if all backends fail.
        ValueError: If inference_chain config is empty.
    """
    chain = load_chain()

    # Reorder chain to honor model_roles.chat preference
    preferred_model = config.CONFIG.get("model_roles", {}).get("chat", "")
    if preferred_model:
        # Strip provider prefix if present (e.g., "anthropic:claude-haiku..." → "claude-haiku...")
        if ":" in preferred_model:
            _, preferred_model = preferred_model.split(":", 1)

        # Find matching entry and move it to front
        preferred_idx = None
        for i, entry in enumerate(chain):
            # Match by exact model name OR by provider if model belongs to that provider
            if entry.model == preferred_model:
                preferred_idx = i
                break
            # Check if it's a Claude model and this is an Anthropic entry
            elif preferred_model.startswith("claude-") and entry.provider == "anthropic":
                preferred_idx = i
                break

        if preferred_idx is not None and preferred_idx > 0:
            # Move preferred entry to front
            chain = [chain[preferred_idx]] + chain[:preferred_idx] + chain[preferred_idx+1:]
            logger.info(
                "Chain: reordered to prefer %s (model_roles.chat=%s)",
                chain[0].name, preferred_model
            )

    last_error: Exception | None = None
    for entry in chain:
        breaker = circuit_breaker.get_breaker(entry.name)

        if not breaker.allow_request():
            logger.info(
                "Chain: skipping %s (circuit breaker open)", entry.name
            )
            continue

        # Convert tools to provider format
        standard = api_standard.get_api_standard(entry.provider)
        provider_tools = api_standard.convert_tools_for_provider(tools, standard)

        # Resolve model: explicit caller model > preferred model > entry default
        use_model = model
        if use_model is None and preferred_model and entry.provider == "anthropic":
            if preferred_model.startswith("claude-"):
                use_model = preferred_model

        try:
            logger.info("Chain: trying backend %s (%s)", entry.name, entry.provider)
            result = _call_single_backend(
                entry, system, messages,
                model=use_model,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=provider_tools,
            )
            breaker.record_success()

            # Tag the response so _call_with_retries can normalize correctly
            result["_api_standard"] = standard
            logger.info("Chain: backend %s succeeded", entry.name)
            return result

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if 400 <= status < 500:
                # Client error — don't failover, propagate immediately
                logger.warning(
                    "Chain: backend %s returned %d (client error), not failing over",
                    entry.name, status,
                )
                raise

            # 5xx — failover
            breaker.record_failure()
            last_error = e
            logger.warning(
                "Chain: backend %s returned %d, failing over",
                entry.name, status,
            )

        except (httpx.ConnectError, httpx.TimeoutException) as e:
            breaker.record_failure()
            last_error = e
            logger.warning(
                "Chain: backend %s connection/timeout error: %s, failing over",
                entry.name, e,
            )

    if last_error is not None:
        raise last_error
    raise RuntimeError("No backends available in inference chain (all circuit breakers open)")


def get_model() -> str:
    """Return the model from the first available backend."""
    try:
        chain = load_chain()
    except ValueError:
        return ""
    for entry in chain:
        breaker = circuit_breaker.get_breaker(entry.name)
        if breaker.allow_request():
            return entry.model
    return chain[0].model if chain else ""


def get_api_url() -> str:
    """Return the URL from the first available backend."""
    try:
        chain = load_chain()
    except ValueError:
        return ""
    for entry in chain:
        breaker = circuit_breaker.get_breaker(entry.name)
        if breaker.allow_request():
            return entry.url
    return chain[0].url if chain else ""


def extract_text(response: dict) -> str:
    """Extract text from a response, detecting format automatically.

    OpenAI format: choices[0].message.content
    Anthropic format: content[0].text (where type == "text")
    """
    # Try OpenAI format first
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        return choices[0].get("message", {}).get("content", "") or ""

    # Try Anthropic format
    content = response.get("content")
    if isinstance(content, list):
        texts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(texts)

    return ""


def extract_code(response: dict) -> str | None:
    """Extract Python code from a response."""
    text = extract_text(response)
    if not text:
        return None
    from .. import api_standard as _as
    return _as.extract_code_from_text(text)
