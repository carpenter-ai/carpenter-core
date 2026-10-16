"""Model registry — loads model metadata from YAML for selector scoring.

Provides a unified view of all configured models with their quality tiers,
cost information, context windows, and capabilities. The seed file ships
as ``config_seed/model-registry.yaml`` and is synced to
``{base_dir}/config/model_registry.yaml`` on first run (same pattern as
credential_registry.yaml).

Falls back to building entries from ``config.CONFIG["models"]`` when the
YAML file is missing. Prices are data, never code: that fallback still takes
each model's price from the registry YAML (the instance copy, else the
bundled seed), so the seed file is the single place list prices live.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:
    yaml = None


@dataclass
class ModelEntry:
    """Metadata for a single model in the registry."""

    key: str  # "opus", "sonnet", etc.
    provider: str  # "anthropic", "ollama", "tinfoil"
    model_id: str  # "claude-opus-4-7"
    quality_tier: int  # 1-5
    cost_per_mtok_in: float
    cost_per_mtok_out: float
    cached_cost_per_mtok_in: float
    context_window: int
    capabilities: list[str] = field(default_factory=list)
    description: str = ""
    measured_speed: Optional[float] = None  # seconds per ktok output


# Module-level cache
_registry: dict[str, ModelEntry] = {}
_registry_loaded: bool = False


def _yaml_path() -> str | None:
    """Resolve the model registry YAML path from config."""
    from ... import config
    base_dir = config.CONFIG.get("base_dir", "")
    if base_dir:
        p = Path(base_dir) / "config" / "model_registry.yaml"
        if p.exists():
            return str(p)
    return None


def _load_from_yaml(yaml_path: str) -> dict[str, ModelEntry]:
    """Parse the YAML registry file into ModelEntry objects."""
    if yaml is None:
        return {}
    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return {}
        models = data.get("models", {})
        if not isinstance(models, dict):
            return {}
        result = {}
        for key, entry in models.items():
            if not isinstance(entry, dict):
                continue
            result[key] = ModelEntry(
                key=key,
                provider=entry.get("provider", ""),
                model_id=entry.get("model_id", ""),
                quality_tier=int(entry.get("quality_tier", 1)),
                cost_per_mtok_in=float(entry.get("cost_per_mtok_in", 0)),
                cost_per_mtok_out=float(entry.get("cost_per_mtok_out", 0)),
                cached_cost_per_mtok_in=float(entry.get("cached_cost_per_mtok_in", 0)),
                context_window=int(entry.get("context_window", 0)),
                capabilities=list(entry.get("capabilities", [])),
                description=str(entry.get("description", "")),
                measured_speed=entry.get("measured_speed"),
            )
        return result
    except (OSError, ValueError, TypeError, KeyError) as _exc:
        logger.exception("Failed to load model registry from %s", yaml_path)
        return {}
    except yaml.YAMLError as _exc:
        logger.exception("Failed to parse model registry YAML from %s", yaml_path)
        return {}


# Cost tier -> quality tier, used only when a config-defined model is not
# found in the registry YAML.
_TIER_QUALITY = {"low": 2, "medium": 4, "high": 5}

# Last-resort price (in/out/cached USD per Mtok), used only when no registry
# YAML can be read at all -- not even the bundled seed, which means a broken
# install. It is deliberately high rather than current: costing a model too
# high trips the budget breaker early, costing it at zero disables it.
_UNKNOWN_PRICE = (15.0, 75.0, 1.5)


def _seed_yaml_path() -> Path:
    """Return the bundled ``config_seed/model-registry.yaml`` path."""
    from ...db import _config_seed_dir
    return _config_seed_dir() / "model-registry.yaml"


def _price_of(entry: ModelEntry) -> tuple[float, float, float]:
    return (
        entry.cost_per_mtok_in,
        entry.cost_per_mtok_out,
        entry.cached_cost_per_mtok_in,
    )


def _reference_prices() -> dict[str, ModelEntry]:
    """Load the registry YAML that supplies prices for config-defined models.

    Prefers the instance copy (``{base_dir}/config/model_registry.yaml``) and
    falls back to the bundled seed, so prices always come from data rather
    than from code.
    """
    for path in (_yaml_path(), str(_seed_yaml_path())):
        if path and Path(path).exists():
            entries = _load_from_yaml(path)
            if entries:
                return entries
    return {}


def _tier_prices(
    reference: dict[str, ModelEntry],
) -> dict[str, tuple[float, float, float]]:
    """Derive a price per cost tier from the reference registry.

    Each tier takes the most expensive registry model at that tier's quality
    level, so an unknown model is never under-costed relative to its peers.
    """
    result = {}
    for tier, quality in _TIER_QUALITY.items():
        peers = [e for e in reference.values() if e.quality_tier == quality]
        if peers:
            result[tier] = _price_of(max(peers, key=lambda e: e.cost_per_mtok_out))
    return result


def _ceiling_price(reference: dict[str, ModelEntry]) -> tuple[float, float, float]:
    """The highest price in the reference registry.

    Used for a model whose price cannot be determined any other way. Charging
    it at the most expensive known rate keeps the budget breaker
    conservative: an unknown model can trip the breaker early, never late.
    """
    if not reference:
        return _UNKNOWN_PRICE
    return _price_of(max(reference.values(), key=lambda e: e.cost_per_mtok_out))


def _load_from_config() -> dict[str, ModelEntry]:
    """Build ModelEntry objects from config.CONFIG['models'] as fallback.

    ``config.CONFIG['models']`` carries no prices of its own. Each model's
    price is resolved, in order, from:

    1. explicit ``cost_per_mtok_in`` / ``cost_per_mtok_out`` /
       ``cached_cost_per_mtok_in`` fields on the config entry;
    2. the registry YAML entry with the same ``model_id``;
    3. the registry's most expensive model at the entry's ``cost_tier``;
    4. the registry's most expensive model overall;
    5. ``_UNKNOWN_PRICE``, only if no registry YAML is readable at all.
    """
    from ... import config

    reference = _reference_prices()
    by_model_id = {e.model_id: e for e in reference.values() if e.model_id}
    tier_prices = _tier_prices(reference)
    ceiling = _ceiling_price(reference)
    if not reference:
        logger.warning(
            "No model registry YAML could be read; costing config-defined "
            "models at the conservative last-resort price %s",
            _UNKNOWN_PRICE,
        )

    models = config.CONFIG.get("models", {})
    result = {}
    for key, entry in models.items():
        if not isinstance(entry, dict):
            continue
        cost_tier = entry.get("cost_tier", "medium")
        model_id = entry.get("model_id", "")
        known = by_model_id.get(model_id)
        if known is not None:
            pricing = _price_of(known)
        else:
            pricing = tier_prices.get(cost_tier, ceiling)
        if any(k in entry for k in (
            "cost_per_mtok_in", "cost_per_mtok_out", "cached_cost_per_mtok_in",
        )):
            pricing = (
                float(entry.get("cost_per_mtok_in", pricing[0])),
                float(entry.get("cost_per_mtok_out", pricing[1])),
                float(entry.get("cached_cost_per_mtok_in", pricing[2])),
            )
        result[key] = ModelEntry(
            key=key,
            provider=entry.get("provider", ""),
            model_id=model_id,
            quality_tier=_TIER_QUALITY.get(cost_tier, 3),
            cost_per_mtok_in=pricing[0],
            cost_per_mtok_out=pricing[1],
            cached_cost_per_mtok_in=pricing[2],
            context_window=int(entry.get("context_window", 200000)),
            capabilities=list(entry.get("roles", [])),
            description=str(entry.get("description", "")),
        )
    return result


def load_registry(yaml_path: str | None = None) -> dict[str, ModelEntry]:
    """Load the model registry from YAML, falling back to config.

    Args:
        yaml_path: Optional explicit path to the YAML file.

    Returns:
        Dict mapping model key to ModelEntry.
    """
    global _registry, _registry_loaded

    if yaml_path is None:
        yaml_path = _yaml_path()

    if yaml_path:
        entries = _load_from_yaml(yaml_path)
        if entries:
            _registry = entries
            _registry_loaded = True
            return dict(_registry)

    # Fallback to config
    entries = _load_from_config()
    _registry = entries
    _registry_loaded = True
    return dict(_registry)


def get_registry() -> dict[str, ModelEntry]:
    """Get the cached registry, loading if needed."""
    if not _registry_loaded:
        load_registry()
    return dict(_registry)


def unknown_model_price() -> tuple[float, float, float]:
    """Price (in/out/cached USD per Mtok) to charge a model with no entry.

    The most expensive model in the loaded registry, or ``_UNKNOWN_PRICE``
    if the registry is empty.  Used by cost accounting so that a model
    missing from the registry is charged conservatively rather than at $0.
    """
    return _ceiling_price(get_registry())


def get_entry(key: str) -> ModelEntry | None:
    """Look up a model entry by its short key (e.g., 'opus')."""
    reg = get_registry()
    return reg.get(key)


def get_entry_by_model_id(model_id: str) -> ModelEntry | None:
    """Look up a model entry by its provider model_id string.

    Args:
        model_id: Exact model ID (e.g., "claude-opus-4-7") or
                  "provider:model_id" format (e.g., "anthropic:claude-opus-4-7").

    Returns:
        ModelEntry or None.
    """
    # Strip provider prefix if present
    if ":" in model_id:
        _, model_id = model_id.split(":", 1)

    reg = get_registry()
    for entry in reg.values():
        if entry.model_id == model_id:
            return entry
    return None


def reload_registry() -> None:
    """Force-reload the registry from disk."""
    global _registry, _registry_loaded
    _registry = {}
    _registry_loaded = False
    load_registry()


def update_measured_speed(key: str, speed: float) -> None:
    """Update the measured_speed for a model in the in-memory registry.

    Also persists to the YAML file if available.

    Args:
        key: Model key (e.g., "opus").
        speed: Measured speed in seconds per ktok output.
    """
    reg = get_registry()
    entry = reg.get(key)
    if entry is None:
        logger.warning("Cannot update speed for unknown model key: %s", key)
        return

    entry.measured_speed = speed
    _registry[key] = entry

    # Persist to YAML
    yaml_path = _yaml_path()
    if yaml_path and yaml is not None:
        try:
            with open(yaml_path) as f:
                data = yaml.safe_load(f) or {}
            models = data.get("models", {})
            if key in models:
                models[key]["measured_speed"] = round(speed, 3)
                data["models"] = models
                with open(yaml_path, "w") as f:
                    yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        except (OSError, ValueError, TypeError) as _exc:
            logger.exception("Failed to persist measured_speed to %s", yaml_path)


