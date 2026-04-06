"""Language model call tool. Tier 1: callback to platform."""
from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True, trusted_output=False,
      param_types={"prompt": "UnstructuredText", "system": "UnstructuredText", "agent_role": "Label"},
      return_types={"content": "UnstructuredText"})
def call(
    prompt: str,
    *,
    model: str | None = None,
    model_role: str | None = None,
    agent_role: str | None = None,
    system: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict:
    """Call a language model through the platform.

    The platform resolves the model, validates it against allowed models,
    and makes the API call. This tool's output is marked as untrusted.

    Args:
        prompt: The user message to send to the model.
        model: Explicit model string (e.g. 'anthropic:claude-sonnet-4-20250514').
        model_role: Named role slot to resolve model from (e.g. 'default_step').
        agent_role: Named agent role for system prompt lookup.
        system: System prompt override (takes precedence over agent_role lookup).
        temperature: Sampling temperature override.
        max_tokens: Max output tokens override.

    Returns:
        Dict with 'content', 'model', 'usage', 'role'.
    """
    params = {"prompt": prompt}
    if model is not None:
        params["model"] = model
    if model_role is not None:
        params["model_role"] = model_role
    if agent_role is not None:
        params["agent_role"] = agent_role
    if system is not None:
        params["system"] = system
    if temperature is not None:
        params["temperature"] = temperature
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    return callback("lm.call", params)
