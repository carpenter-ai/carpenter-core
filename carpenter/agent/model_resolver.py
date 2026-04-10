"""Model string parsing and escalation stack resolution.

Provides utilities for:
- Parsing provider:model strings (e.g., "anthropic:claude-haiku-4.5")
- Looking up escalation stacks for task types
- Finding the next model in an escalation chain
- Creating client modules for specific models
- Estimating cost multipliers between models
"""

from .. import config

# Ordered cost tiers (lowest to highest). Used for tier comparisons.
COST_TIER_ORDER = ["low", "medium", "high"]


def resolve_model_identifier(identifier: str) -> str:
    """Resolve a short model identifier (e.g., "opus") to a "provider:model_id" string.

    Looks up the identifier in the ``models`` config section.

    Args:
        identifier: Short model key (e.g., "opus", "sonnet", "haiku").

    Returns:
        Model string in "provider:model_id" format.

    Raises:
        ValueError: If identifier is not found in the models config.
    """
    models = config.CONFIG.get("models", {})
    entry = models.get(identifier)
    if entry is None:
        raise ValueError(
            f"Unknown model identifier {identifier!r}. "
            f"Available: {sorted(models.keys())}"
        )
    return f"{entry['provider']}:{entry['model_id']}"


def get_model_manifest() -> dict:
    """Return the full model manifest from config.

    Returns:
        Dict mapping identifier keys to model entry dicts.
    """
    return dict(config.CONFIG.get("models", {}))


def get_cost_tier(identifier: str) -> str:
    """Get the cost_tier for a model identifier.

    Args:
        identifier: Short model key (e.g., "opus").

    Returns:
        Cost tier string ("low", "medium", or "high").

    Raises:
        ValueError: If identifier is not found in the models config.
    """
    models = config.CONFIG.get("models", {})
    entry = models.get(identifier)
    if entry is None:
        raise ValueError(
            f"Unknown model identifier {identifier!r}. "
            f"Available: {sorted(models.keys())}"
        )
    return entry.get("cost_tier", "medium")


def compare_cost_tiers(tier_a: str, tier_b: str) -> int:
    """Compare two cost tiers.

    Returns:
        Negative if tier_a < tier_b, 0 if equal, positive if tier_a > tier_b.

    Raises:
        ValueError: If either tier is not recognized.
    """
    try:
        idx_a = COST_TIER_ORDER.index(tier_a)
    except ValueError:
        raise ValueError(f"Unknown cost tier: {tier_a!r}")
    try:
        idx_b = COST_TIER_ORDER.index(tier_b)
    except ValueError:
        raise ValueError(f"Unknown cost tier: {tier_b!r}")
    return idx_a - idx_b


def get_model_for_role(slot: str) -> str:
    """Resolve a model string for a named role slot.

    Resolution chain:
    1. model_roles[slot] if non-empty
    2. model_roles["default"] if non-empty
    3. Auto-detect from ai_provider + provider-specific model

    Returns:
        Model string in "provider:model" format.
    """
    model_roles = config.CONFIG.get("model_roles", {})

    # 1. Check specific slot
    model = model_roles.get(slot, "")
    if model:
        return model

    # 2. Check default
    model = model_roles.get("default", "")
    if model:
        return model

    # 3. Auto-detect from provider
    provider = config.CONFIG.get("ai_provider", "anthropic")
    if provider == "anthropic":
        anthropic_model = config.CONFIG.get("anthropic_model", "claude-sonnet-4-6")
        return f"anthropic:{anthropic_model}"
    elif provider == "ollama":
        ollama_model = config.CONFIG.get("ollama_model", "llama3.1")
        return f"ollama:{ollama_model}"
    elif provider == "tinfoil":
        tinfoil_model = config.CONFIG.get("tinfoil_model", "llama3-3-70b")
        return f"tinfoil:{tinfoil_model}"
    elif provider == "chain":
        from .providers import chain as chain_client
        model = chain_client.get_model()
        return f"chain:{model}" if model else "chain:default"
    elif provider == "local":
        import os
        model_path = config.CONFIG.get("local_model_path", "")
        if model_path:
            basename = os.path.splitext(os.path.basename(model_path))[0]
            return f"local:{basename}"
        return "local:default"
    else:
        anthropic_model = config.CONFIG.get("anthropic_model", "claude-sonnet-4-6")
        return f"anthropic:{anthropic_model}"


def parse_model_string(model_str: str) -> tuple[str, str]:
    """Parse a model string into (provider, model_name).

    Args:
        model_str: Format "provider:model" (e.g., "anthropic:claude-haiku-4.5")
                   or "provider:model:variant" for Ollama (e.g., "ollama:qwen2.5-coder:32b")

    Returns:
        Tuple of (provider, model_name).
        For Ollama with variant, model_name includes the variant (e.g., "qwen2.5-coder:32b").

    Raises:
        ValueError: If model_str is malformed.
    """
    if ":" not in model_str:
        # Infer provider from ai_provider config for bare model names
        provider = config.CONFIG.get("ai_provider", "anthropic")
        return provider, model_str

    parts = model_str.split(":", 1)
    provider = parts[0]
    model_name = parts[1]

    if not provider or not model_name:
        raise ValueError(f"Invalid model string: {model_str}")

    return provider, model_name


def get_escalation_stack(task_type: str) -> list[str]:
    """Get the escalation stack for a given task type.

    Args:
        task_type: Task type ("coding", "writing", "general").

    Returns:
        List of model strings in escalation order (weakest to strongest).
        Falls back to "general" stack if task_type not found.
    """
    escalation_config = config.CONFIG.get("escalation", {})
    stacks = escalation_config.get("stacks", {})

    # Try task-specific stack first, fall back to general
    stack = stacks.get(task_type) or stacks.get("general", [])

    return stack


def get_next_model(current_model: str, task_type: str) -> str | None:
    """Find the next model in the escalation stack.

    Args:
        current_model: Current model string (e.g., "anthropic:claude-haiku-4.5").
        task_type: Task type for stack selection.

    Returns:
        Next model in the stack, or None if already at the top.
    """
    stack = get_escalation_stack(task_type)

    if not stack:
        return None

    try:
        current_idx = stack.index(current_model)
    except ValueError:
        # Current model not in stack — return first model as a fallback
        return stack[0] if stack else None

    # If we're at the last position, no escalation available
    if current_idx >= len(stack) - 1:
        return None

    return stack[current_idx + 1]


def create_client_for_model(model_str: str):
    """Return the appropriate AI client module for a model string.

    Args:
        model_str: Model string in "provider:model" format.

    Returns:
        The provider module (e.g. providers.anthropic, providers.ollama).

    Raises:
        ValueError: If provider is unknown.

    Note for tests:
        This function returns the actual client module object via a local
        import, so patching a *name binding* in another module's namespace
        (e.g. ``@patch("carpenter.agent.invocation.ollama_client")``)
        does NOT intercept calls routed through this function — it only
        affects code that accesses ``invocation.ollama_client`` by name
        (such as ``_get_client()``).

        To mock client calls in tests that go through this function, patch
        the ``call`` attribute on the actual module instead::

            @patch("carpenter.agent.providers.ollama.call")

        This works regardless of which code path obtained the client.
        The canonical path is ``carpenter.agent.providers.ollama.call``.
    """
    provider, _ = parse_model_string(model_str)

    if provider == "anthropic":
        from .providers import anthropic as claude_client
        return claude_client
    elif provider == "ollama":
        from .providers import ollama as ollama_client
        return ollama_client
    elif provider == "tinfoil":
        from .providers import tinfoil as tinfoil_client
        return tinfoil_client
    elif provider == "chain":
        from .providers import chain as chain_client
        return chain_client
    elif provider == "local":
        from .providers import local as local_client
        return local_client
    else:
        raise ValueError(f"Unknown AI provider: {provider}")


def estimate_cost_multiplier(current_model: str, target_model: str) -> str:
    """Estimate the cost increase from current to target model.

    Tries model registry first for precise cost data, falls back to
    escalation.pricing config for legacy compatibility.

    Args:
        current_model: Current model string.
        target_model: Target model string.

    Returns:
        Human-readable cost estimate (e.g., "~3x", "~10x", "free").
    """
    # Try model registry for precise cost data
    try:
        from ..core.models.registry import get_entry_by_model_id
        current_entry = get_entry_by_model_id(current_model)
        target_entry = get_entry_by_model_id(target_model)

        if current_entry and target_entry:
            current_avg = (current_entry.cost_per_mtok_in + current_entry.cost_per_mtok_out) / 2
            target_avg = (target_entry.cost_per_mtok_in + target_entry.cost_per_mtok_out) / 2

            if current_avg == 0 and target_avg == 0:
                return "free"
            if current_avg == 0:
                return "paid" if target_avg > 0 else "free"

            multiplier = target_avg / current_avg if current_avg > 0 else 1.0
            return _format_multiplier(multiplier)
    except (ImportError, KeyError, ValueError, ZeroDivisionError) as _exc:
        pass  # Fall through to legacy pricing

    # Legacy fallback: escalation.pricing config
    escalation_config = config.CONFIG.get("escalation", {})
    pricing = escalation_config.get("pricing", {})

    current_pricing = pricing.get(current_model)
    target_pricing = pricing.get(target_model)

    # If either model is not in pricing, check if they're free (Ollama/local)
    if current_pricing is None or target_pricing is None:
        # Ollama and local models are free
        if target_model.startswith("ollama:") or target_model.startswith("local:"):
            return "free"
        # Unknown pricing
        return "unknown cost"

    # Extract average cost (mean of input and output)
    current_avg = sum(current_pricing) / len(current_pricing) if current_pricing else 0
    target_avg = sum(target_pricing) / len(target_pricing) if target_pricing else 0

    if current_avg == 0 and target_avg == 0:
        return "free"
    if current_avg == 0:
        return "paid" if target_avg > 0 else "free"

    multiplier = target_avg / current_avg if current_avg > 0 else 1.0
    return _format_multiplier(multiplier)


def _format_multiplier(multiplier: float) -> str:
    """Format a cost multiplier to a human-readable string."""
    if multiplier < 1.5:
        return "similar cost"
    elif multiplier < 2.5:
        return "~2x"
    elif multiplier < 4:
        return "~3x"
    elif multiplier < 7:
        return "~5x"
    elif multiplier < 12:
        return "~10x"
    else:
        return f"~{int(multiplier)}x"
