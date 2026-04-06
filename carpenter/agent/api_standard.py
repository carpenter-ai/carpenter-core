"""API standard normalization layer.

Translates between Anthropic and OpenAI API formats at the call boundary.
The rest of the codebase works with one canonical format (Anthropic-like):

- content: list of blocks [{type: "text", text: ...}, {type: "tool_use", ...}]
- stop_reason: "end_turn" | "tool_use" | "max_tokens"
- usage: {input_tokens, output_tokens}

Two standards: "anthropic" (native) and "openai" (Ollama, llama.cpp, Tinfoil).
Providers declare their standard via the ``api_standards`` config dict.
"""

import json
import logging
import re

from .. import config

logger = logging.getLogger(__name__)

# Default mapping: provider name → API standard
_DEFAULT_STANDARDS = {
    "anthropic": "anthropic",
    "ollama": "openai",
    "local": "openai",
    "tinfoil": "openai",
    "chain": "anthropic",
}


def get_api_standard(provider: str) -> str:
    """Resolve the API standard for a provider.

    Checks the ``api_standards`` config dict first, then falls back
    to built-in defaults. Unknown providers default to ``"openai"``.

    Args:
        provider: Provider name (e.g., "anthropic", "ollama").

    Returns:
        ``"anthropic"`` or ``"openai"``.
    """
    standards = config.CONFIG.get("api_standards", {})
    return standards.get(provider) or _DEFAULT_STANDARDS.get(provider, "openai")


def convert_tools_for_provider(
    tools: list[dict] | None, standard: str
) -> list[dict] | None:
    """Convert tool definitions to the provider's expected format.

    Anthropic format (canonical)::

        {"name": "...", "description": "...", "input_schema": {...}}

    OpenAI format::

        {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}

    Args:
        tools: Tool definitions in Anthropic format, or None.
        standard: ``"anthropic"`` or ``"openai"``.

    Returns:
        Converted tool definitions, or None if tools is None.
    """
    if tools is None:
        return None

    if standard == "anthropic":
        return tools

    # Convert to OpenAI function-calling format
    converted = []
    for tool in tools:
        converted.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        })
    return converted


def normalize_response(raw: dict, standard: str) -> dict:
    """Normalize an API response to canonical (Anthropic-like) format.

    Passthrough for ``"anthropic"``. For ``"openai"``, converts:

    - ``choices[0].message.content`` → ``content: [{type: "text", text: ...}]``
    - ``choices[0].message.tool_calls`` → ``content: [{type: "tool_use", ...}]``
    - ``finish_reason`` → ``stop_reason`` mapping
    - ``usage.prompt_tokens/completion_tokens`` → ``usage.input_tokens/output_tokens``

    Args:
        raw: Raw API response dict.
        standard: ``"anthropic"`` or ``"openai"``.

    Returns:
        Normalized response dict in canonical format.
    """
    if standard == "anthropic":
        return raw

    # --- OpenAI → Anthropic normalization ---
    choices = raw.get("choices", [])
    if not choices:
        return {
            "content": [],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "model": raw.get("model", ""),
        }

    choice = choices[0]
    message = choice.get("message", {})

    # Build content blocks
    content_blocks = []

    # Text content
    text = message.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})

    # Tool calls
    tool_calls = message.get("tool_calls", [])
    for tc in tool_calls:
        func = tc.get("function", {})
        # Parse arguments — may be a JSON string or already a dict
        args = func.get("arguments", "{}")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Malformed tool call JSON: %s", args[:200])
                args = {"_parse_error": args[:200]}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": func.get("name", ""),
            "input": args,
        })

    # Map finish_reason → stop_reason
    finish_reason = choice.get("finish_reason", "stop")
    stop_reason_map = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
    }
    stop_reason = stop_reason_map.get(finish_reason, "end_turn")

    # Normalize usage
    raw_usage = raw.get("usage") or {}
    usage = {
        "input_tokens": raw_usage.get("prompt_tokens", 0),
        "output_tokens": raw_usage.get("completion_tokens", 0),
    }

    return {
        "content": content_blocks,
        "stop_reason": stop_reason,
        "usage": usage,
        "model": raw.get("model", ""),
    }


def format_tool_results_for_api(
    results: list[dict], standard: str
) -> list[dict]:
    """Format tool result blocks for the provider's API.

    Args:
        results: Tool results in canonical format::

            [{"type": "tool_result", "tool_use_id": "...", "content": "..."}]

        standard: ``"anthropic"`` or ``"openai"``.

    Returns:
        For anthropic: same list (caller wraps in user message).
        For openai: list of ``{"role": "tool", "tool_call_id": ..., "content": ...}``
            messages (caller appends directly to message list).
    """
    if standard == "anthropic":
        return results

    # OpenAI: each tool result is a separate message
    messages = []
    for r in results:
        messages.append({
            "role": "tool",
            "tool_call_id": r["tool_use_id"],
            "content": r.get("content", ""),
        })
    return messages


def format_assistant_tool_message(
    content_blocks: list[dict], standard: str
) -> dict:
    """Format an assistant message containing tool_use blocks for the API.

    Args:
        content_blocks: Canonical content blocks (text + tool_use).
        standard: ``"anthropic"`` or ``"openai"``.

    Returns:
        A single message dict in the provider's format.
    """
    if standard == "anthropic":
        return {"role": "assistant", "content": content_blocks}

    # OpenAI format: text in content field, tool_calls as separate array
    text_parts = []
    tool_calls = []
    for block in content_blocks:
        if block.get("type") == "text" and block.get("text"):
            text_parts.append(block["text"])
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {})),
                },
            })

    msg = {
        "role": "assistant",
        "content": "\n".join(text_parts) if text_parts else None,
    }
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def extract_code_from_text(text: str) -> str | None:
    """Extract Python code from a text string.

    Looks for \\`\\`\\`python ... \\`\\`\\` code blocks.
    Returns the last code block (most likely the final version),
    or None if no code block found.
    """
    pattern = r'```python\s*\n(.*?)```'
    matches = re.findall(pattern, text, re.DOTALL)
    if matches:
        return matches[-1].strip() + "\n"
    return None
