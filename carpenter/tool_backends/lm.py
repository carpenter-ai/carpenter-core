"""Language model call backend — handles lm.call callbacks from executors."""
import logging

from .. import config
from ..agent import api_standard
from ..agent.model_resolver import (
    get_model_for_role, create_client_for_model, parse_model_string,
)

logger = logging.getLogger(__name__)


def _get_allowed_models() -> set[str]:
    """Build the set of allowed model strings from model_roles config."""
    model_roles = config.CONFIG.get("model_roles", {})
    allowed = set()
    for slot, model_str in model_roles.items():
        if model_str:
            allowed.add(model_str)
    return allowed


def handle_call(params: dict) -> dict:
    """Handle an lm.call request from executor code.

    Resolves model from: explicit model > model_role > arc config > default_step.
    Validates model is in the allowed set (model_roles values).

    Returns:
        Dict with 'content', 'model', 'usage', 'role'.
    """
    prompt = params.get("prompt")
    if not prompt:
        return {"error": "prompt is required"}

    # Resolve model
    explicit_model = params.get("model")
    model_role = params.get("model_role")

    if explicit_model:
        model_str = explicit_model
    elif model_role:
        model_str = get_model_for_role(model_role)
    else:
        model_str = get_model_for_role("default_step")

    # Validate model is allowed
    allowed = _get_allowed_models()
    if allowed and model_str not in allowed:
        # Also allow if it matches the auto-detected default
        auto_default = get_model_for_role("default")
        if model_str != auto_default:
            return {
                "error": f"Model '{model_str}' is not in the allowed model_roles set. "
                         f"Allowed: {sorted(allowed) if allowed else '(auto-detect only)'}"
            }

    # Resolve system prompt
    system_prompt = params.get("system", "")
    if not system_prompt:
        agent_role_name = params.get("agent_role")
        if agent_role_name:
            agent_roles = config.CONFIG.get("agent_roles", {})
            role_config = agent_roles.get(agent_role_name, {})
            system_prompt = role_config.get("system_prompt", "")

    if not system_prompt:
        system_prompt = "You are a helpful assistant."

    # Build messages
    messages = [{"role": "user", "content": prompt}]

    # Get client and call
    try:
        client = create_client_for_model(model_str)
        _, bare_model = parse_model_string(model_str)

        kwargs = {"model": bare_model}
        if params.get("temperature") is not None:
            kwargs["temperature"] = params["temperature"]
        if params.get("max_tokens") is not None:
            kwargs["max_tokens"] = params["max_tokens"]

        raw = client.call(system_prompt, messages, **kwargs)

        # Normalize response to canonical format
        provider, _ = parse_model_string(model_str)
        standard = api_standard.get_api_standard(provider)
        response = api_standard.normalize_response(raw, standard)

        # Extract content from normalized response
        text_parts = []
        for block in response.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        content = "\n".join(text_parts)

        usage = response.get("usage", {})

        return {
            "content": content,
            "model": model_str,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            },
            "role": "assistant",
        }
    except Exception as e:  # broad catch: AI provider client may raise anything
        logger.exception("lm.call failed for model %s", model_str)
        return {"error": str(e)}
