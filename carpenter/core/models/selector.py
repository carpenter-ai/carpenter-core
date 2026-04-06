"""Model selector — constraint filtering + preference-weighted scoring.

Given a ModelPolicy (constraints + preference vector), selects the best
available model from the registry. Integrates with model_health for
circuit breaker filtering and accounts for cache-loss penalty when
switching providers.

Usage::

    from carpenter.core.models.selector import select_model, ModelPolicy, PolicyConstraints

    policy = ModelPolicy(
        constraints=PolicyConstraints(min_quality=4),
        preference=(0.1, 0.6, 0.3),  # (cost, quality, speed)
    )
    result = select_model(policy, current_model="anthropic:claude-sonnet-4-5-20250929")
    if result:
        print(result.model_id, result.score, result.reason)
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from .registry import ModelEntry, get_registry

logger = logging.getLogger(__name__)


@dataclass
class PolicyConstraints:
    """Hard constraints that filter eligible models."""

    min_quality: int = 1
    max_quality: Optional[int] = None
    max_cost_per_mtok_out: Optional[float] = None
    max_latency_s_per_ktok: Optional[float] = None
    required_capabilities: Optional[list[str]] = None


@dataclass
class ModelPolicy:
    """Model selection policy — constraints + preference vector.

    Attributes:
        id: Database row ID (None for in-memory policies).
        name: Human-readable policy name.
        model: Hard-pinned model string (bypasses selector when set).
        temperature: Sampling temperature override.
        max_tokens: Max tokens override.
        constraints: Hard constraint filters.
        preference: Weighted preference vector (cost, quality, speed).
                    Values should sum to ~1.0. Higher weight = more important.
    """

    id: Optional[int] = None
    name: str = ""
    model: Optional[str] = None  # Hard pin (bypass selector)
    agent_role: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    constraints: Optional[PolicyConstraints] = None
    preference: tuple[float, float, float] = (0.3, 0.4, 0.3)  # (cost, quality, speed)

    @classmethod
    def from_db_row(cls, row: dict) -> "ModelPolicy":
        """Construct from a model_policies DB row."""
        policy_json = row.get("policy_json")
        constraints = None
        preference = (0.3, 0.4, 0.3)

        if policy_json:
            try:
                data = json.loads(policy_json)
                if "constraints" in data:
                    c = data["constraints"]
                    constraints = PolicyConstraints(
                        min_quality=c.get("min_quality", 1),
                        max_quality=c.get("max_quality"),
                        max_cost_per_mtok_out=c.get("max_cost_per_mtok_out"),
                        max_latency_s_per_ktok=c.get("max_latency_s_per_ktok"),
                        required_capabilities=c.get("required_capabilities"),
                    )
                if "preference" in data:
                    p = data["preference"]
                    if isinstance(p, (list, tuple)) and len(p) == 3:
                        preference = tuple(p)
            except (json.JSONDecodeError, TypeError, KeyError):
                logger.warning("Failed to parse policy_json: %s", policy_json)

        return cls(
            id=row.get("id"),
            name=row.get("name", ""),
            model=row.get("model"),
            agent_role=row.get("agent_role"),
            temperature=row.get("temperature"),
            max_tokens=row.get("max_tokens"),
            constraints=constraints,
            preference=preference,
        )

    def to_policy_json(self) -> str | None:
        """Serialize constraints and preference to JSON for DB storage."""
        if self.constraints is None and self.preference == (0.3, 0.4, 0.3):
            return None
        data = {}
        if self.constraints is not None:
            c = self.constraints
            cd = {"min_quality": c.min_quality}
            if c.max_quality is not None:
                cd["max_quality"] = c.max_quality
            if c.max_cost_per_mtok_out is not None:
                cd["max_cost_per_mtok_out"] = c.max_cost_per_mtok_out
            if c.max_latency_s_per_ktok is not None:
                cd["max_latency_s_per_ktok"] = c.max_latency_s_per_ktok
            if c.required_capabilities is not None:
                cd["required_capabilities"] = c.required_capabilities
            data["constraints"] = cd
        data["preference"] = list(self.preference)
        return json.dumps(data)


@dataclass
class SelectionResult:
    """Result of model selection."""

    model_key: str  # "opus", "sonnet", etc.
    model_id: str  # "provider:model_id" format
    score: float
    reason: str


def _passes_constraints(entry: ModelEntry, constraints: PolicyConstraints) -> bool:
    """Check if a model entry passes all hard constraints."""
    if entry.quality_tier < constraints.min_quality:
        return False
    if constraints.max_quality is not None and entry.quality_tier > constraints.max_quality:
        return False
    if constraints.max_cost_per_mtok_out is not None:
        if entry.cost_per_mtok_out > constraints.max_cost_per_mtok_out:
            return False
    if constraints.max_latency_s_per_ktok is not None:
        if entry.measured_speed is not None:
            if entry.measured_speed > constraints.max_latency_s_per_ktok:
                return False
    if constraints.required_capabilities:
        entry_caps = set(entry.capabilities)
        for cap in constraints.required_capabilities:
            if cap not in entry_caps:
                return False
    return True


def _filter_by_health(eligible: list[ModelEntry]) -> list[ModelEntry]:
    """Filter models by health state (exclude CIRCUIT_OPEN models and providers).
    
    Returns the filtered list, or the original list if ALL models/providers are unhealthy
    (graceful degradation).
    
    Args:
        eligible: List of models that passed constraint filtering.
        
    Returns:
        Filtered list of healthy models, or original list if all are unhealthy.
    """
    try:
        from . import health

        # Filter out individual CIRCUIT_OPEN models
        healthy = []
        for entry in eligible:
            model_id = f"{entry.provider}:{entry.model_id}"
            if not health.should_circuit_break(model_id):
                healthy.append(entry)
        if healthy:
            eligible = healthy
        # If ALL are circuit-broken, keep all eligible (degrade gracefully)

        # Filter out models from providers that are entirely CIRCUIT_OPEN
        provider_filtered = []
        for entry in eligible:
            prov_health = health.get_provider_health(entry.provider)
            if prov_health.health != health.ModelHealth.CIRCUIT_OPEN:
                provider_filtered.append(entry)
        if provider_filtered:
            eligible = provider_filtered
        # If ALL providers are down, keep all eligible (degrade gracefully)
    except (ImportError, AttributeError, KeyError, ValueError):
        logger.debug("Health module not available, skipping health filtering", exc_info=True)

    return eligible


def _score_models(
    eligible: list[ModelEntry],
    policy: ModelPolicy,
    current_model: str | None,
    cached_tokens: int,
) -> list[SelectionResult]:
    """Score and rank all eligible models with cache-loss penalty.
    
    Scoring algorithm:
    - Normalize cost, quality, speed to [0,1]
    - Apply weighted sum via policy preference vector
    - Apply cache-loss penalty for provider switches
    
    Args:
        eligible: List of models that passed filtering.
        policy: Model selection policy with preference weights.
        current_model: Currently active model string (for cache-loss calculation).
        cached_tokens: Number of cached input tokens with current provider.
        
    Returns:
        List of SelectionResult sorted by score descending.
    """
    cost_w, quality_w, speed_w = policy.preference

    # Find max values for normalization
    max_cost = max(e.cost_per_mtok_out for e in eligible) or 1.0
    speeds = [e.measured_speed for e in eligible if e.measured_speed is not None]
    max_speed = max(speeds) if speeds else 1.0
    median_speed = sorted(speeds)[len(speeds) // 2] if speeds else max_speed * 0.5

    # Parse current model provider for cache-loss calculation
    current_provider = None
    if current_model and ":" in current_model:
        current_provider = current_model.split(":", 1)[0]

    results: list[SelectionResult] = []

    for entry in eligible:
        # Cost score: cheaper = higher (free models get perfect score)
        if max_cost > 0:
            cost_score = 1.0 - (entry.cost_per_mtok_out / max_cost)
        else:
            cost_score = 1.0

        # Quality score: higher tier = higher
        quality_score = entry.quality_tier / 5.0

        # Speed score: faster = higher (unknown treated as median)
        effective_speed = entry.measured_speed if entry.measured_speed is not None else median_speed
        if max_speed > 0:
            speed_score = 1.0 - (effective_speed / max_speed) if max_speed > effective_speed else 0.0
        else:
            speed_score = 1.0

        # Weighted sum
        score = (cost_w * cost_score) + (quality_w * quality_score) + (speed_w * speed_score)

        # Cache-loss penalty
        if cached_tokens > 0 and current_provider and entry.provider != current_provider:
            # Cost of re-sending cached tokens at full price instead of cached price
            price_delta = entry.cost_per_mtok_in - entry.cached_cost_per_mtok_in
            if price_delta > 0:
                switch_cost_usd = cached_tokens * price_delta / 1_000_000
                # Scale penalty: $0.01 → ~0.05 score, $0.10 → ~0.5 score
                penalty = min(switch_cost_usd * 5.0, 0.5)
                score -= penalty

        reason_parts = [f"cost={cost_score:.2f}", f"quality={quality_score:.2f}", f"speed={speed_score:.2f}"]
        reason = f"scored {score:.3f} ({', '.join(reason_parts)})"

        model_id = f"{entry.provider}:{entry.model_id}"
        results.append(SelectionResult(
            model_key=entry.key,
            model_id=model_id,
            score=score,
            reason=reason,
        ))

    # Sort by score descending (highest first)
    results.sort(key=lambda r: r.score, reverse=True)
    return results


def select_models(
    policy: ModelPolicy,
    current_model: str | None = None,
    cached_tokens: int = 0,
) -> list[SelectionResult]:
    """Select and rank all eligible models given policy, health state, and cache context.

    Steps:
    1. Load registry
    2. Filter by constraints (quality bounds, cost cap, latency cap, capabilities)
    3. Filter by health (exclude CIRCUIT_OPEN)
    4. Score: normalize each dimension to [0,1], weighted sum via preference vector
       - cost_score = 1 - (cost / max_cost)  [cheaper = higher score]
       - quality_score = quality_tier / 5
       - speed_score = 1 - (speed / max_speed)  [faster = higher score]
         (unknown speed treated as median)
    5. Cache-loss penalty for non-current provider:
       switch_penalty = cached_tokens * (full_price - cached_price) / 1M
       Converted to score deduction proportional to penalty magnitude.
    6. Return all eligible models sorted by score (descending)

    Args:
        policy: Model selection policy.
        current_model: Currently active model string (for cache-loss calculation).
        cached_tokens: Number of cached input tokens with current provider.

    Returns:
        List of SelectionResult sorted by score descending. Empty if no eligible models.
        Hard-pinned policies return a single-element list.
    """
    # Hard pin — bypass selection entirely
    if policy.model:
        return [SelectionResult(
            model_key=policy.model,
            model_id=policy.model,
            score=1.0,
            reason="hard-pinned",
        )]

    registry = get_registry()
    if not registry:
        return []

    constraints = policy.constraints or PolicyConstraints()

    # Filter by constraints
    eligible = []
    for key, entry in registry.items():
        if not _passes_constraints(entry, constraints):
            continue
        eligible.append(entry)

    if not eligible:
        return []

    # Filter by health (exclude CIRCUIT_OPEN models and providers)
    eligible = _filter_by_health(eligible)

    if not eligible:
        return []

    # Score and rank models
    return _score_models(eligible, policy, current_model, cached_tokens)


def select_model(
    policy: ModelPolicy,
    current_model: str | None = None,
    cached_tokens: int = 0,
) -> SelectionResult | None:
    """Select best model given policy, health state, and cache context.

    Convenience wrapper around select_models() that returns only the top result.

    Args:
        policy: Model selection policy.
        current_model: Currently active model string (for cache-loss calculation).
        cached_tokens: Number of cached input tokens with current provider.

    Returns:
        SelectionResult or None if no eligible models.
    """
    results = select_models(policy, current_model, cached_tokens)
    return results[0] if results else None


# ── Named presets ─────────────────────────────────────────────────

_DEFAULT_PRESETS: dict[str, ModelPolicy] = {
    "fast-chat": ModelPolicy(
        name="fast-chat",
        constraints=PolicyConstraints(min_quality=2),
        preference=(0.3, 0.2, 0.5),
    ),
    "careful-coding": ModelPolicy(
        name="careful-coding",
        constraints=PolicyConstraints(min_quality=4),
        preference=(0.1, 0.6, 0.3),
    ),
    "background-batch": ModelPolicy(
        name="background-batch",
        constraints=PolicyConstraints(max_cost_per_mtok_out=5.0),
        preference=(0.6, 0.2, 0.2),
    ),
    "caretaker": ModelPolicy(
        name="caretaker",
        constraints=PolicyConstraints(max_quality=2),
        preference=(0.5, 0.3, 0.2),
    ),
}


def _build_preset(name: str, overrides: dict) -> ModelPolicy:
    """Build a ModelPolicy from a config override dict, using defaults as base."""
    base = _DEFAULT_PRESETS.get(name)
    # Start from base if it exists, otherwise build from scratch
    constraints_data = overrides.get("constraints", {})
    preference = overrides.get("preference")

    if base and not constraints_data and preference is None:
        # No meaningful overrides, return the default
        return base

    # Build constraints
    if constraints_data:
        constraints = PolicyConstraints(
            min_quality=constraints_data.get("min_quality", 1),
            max_quality=constraints_data.get("max_quality"),
            max_cost_per_mtok_out=constraints_data.get("max_cost_per_mtok_out"),
            max_latency_s_per_ktok=constraints_data.get("max_latency_s_per_ktok"),
            required_capabilities=constraints_data.get("required_capabilities"),
        )
    elif base:
        constraints = base.constraints
    else:
        constraints = PolicyConstraints()

    # Build preference tuple
    if preference is not None and isinstance(preference, (list, tuple)) and len(preference) == 3:
        pref = tuple(preference)
    elif base:
        pref = base.preference
    else:
        pref = (0.3, 0.4, 0.3)

    return ModelPolicy(
        name=name,
        model=overrides.get("model") or (base.model if base else None),
        constraints=constraints,
        preference=pref,
    )


def get_presets() -> dict[str, ModelPolicy]:
    """Return model presets, merging config overrides with hardcoded defaults.

    Config key ``model_presets`` is a dict mapping preset names to override dicts.
    Each override dict may contain ``constraints``, ``preference``, and ``model`` keys.
    Unspecified fields fall back to the hardcoded default for that preset.
    New presets not in the defaults are also supported.
    """
    from ...config import get_config
    user_overrides = get_config("model_presets", {})

    if not user_overrides:
        return dict(_DEFAULT_PRESETS)

    merged = dict(_DEFAULT_PRESETS)
    for name, overrides in user_overrides.items():
        if isinstance(overrides, dict):
            merged[name] = _build_preset(name, overrides)

    return merged


# Module-level alias for backward compatibility — consumers that import PRESETS
# directly get the merged result.  This is recomputed on each access via property
# but since it's a module-level dict, we compute it once at import time and
# provide get_presets() for dynamic access after config reload.
PRESETS: dict[str, ModelPolicy] = get_presets()
