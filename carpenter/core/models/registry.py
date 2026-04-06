"""Model registry — loads model metadata from YAML for selector scoring.

Provides a unified view of all configured models with their quality tiers,
cost information, context windows, and capabilities. The seed file ships
as ``config_seed/model-registry.yaml`` and is synced to
``{base_dir}/config/model_registry.yaml`` on first run (same pattern as
credential_registry.yaml).

Falls back to building entries from ``config.CONFIG["models"]`` when the
YAML file is missing.
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
    provider: str  # "anthropic", "ollama", "local", "tinfoil"
    model_id: str  # "claude-opus-4-6"
    quality_tier: int  # 1-5
    cost_per_mtok_in: float
    cost_per_mtok_out: float
    cached_cost_per_mtok_in: float
    context_window: int
    capabilities: list[str] = field(default_factory=list)
    description: str = ""
    measured_speed: Optional[float] = None  # seconds per ktok output
    # Download metadata for local GGUF models (used by install tooling)
    hf_repo: str = ""  # HuggingFace repo, e.g. "Qwen/Qwen2.5-1.5B-Instruct-GGUF"
    gguf_filename: str = ""  # GGUF file name, e.g. "qwen2.5-1.5b-instruct-q4_k_m.gguf"
    download_size_mb: int = 0  # Approximate download size in MB


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
                hf_repo=str(entry.get("hf_repo", "")),
                gguf_filename=str(entry.get("gguf_filename", "")),
                download_size_mb=int(entry.get("download_size_mb", 0)),
            )
        return result
    except (OSError, ValueError, TypeError, KeyError) as _exc:
        logger.exception("Failed to load model registry from %s", yaml_path)
        return {}
    except yaml.YAMLError as _exc:
        logger.exception("Failed to parse model registry YAML from %s", yaml_path)
        return {}


def _load_from_config() -> dict[str, ModelEntry]:
    """Build ModelEntry objects from config.CONFIG['models'] as fallback."""
    from ... import config

    # Cost tier → quality_tier mapping
    tier_map = {"low": 2, "medium": 4, "high": 5}
    # Cost tier → approximate pricing (in/out/cached per Mtok)
    pricing_map = {
        "low": (0.8, 4.0, 0.08),
        "medium": (3.0, 15.0, 0.3),
        "high": (15.0, 75.0, 1.5),
    }

    models = config.CONFIG.get("models", {})
    result = {}
    for key, entry in models.items():
        if not isinstance(entry, dict):
            continue
        cost_tier = entry.get("cost_tier", "medium")
        pricing = pricing_map.get(cost_tier, (3.0, 15.0, 0.3))
        result[key] = ModelEntry(
            key=key,
            provider=entry.get("provider", ""),
            model_id=entry.get("model_id", ""),
            quality_tier=tier_map.get(cost_tier, 3),
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


def get_entry(key: str) -> ModelEntry | None:
    """Look up a model entry by its short key (e.g., 'opus')."""
    reg = get_registry()
    return reg.get(key)


def get_entry_by_model_id(model_id: str) -> ModelEntry | None:
    """Look up a model entry by its provider model_id string.

    Args:
        model_id: Exact model ID (e.g., "claude-opus-4-6") or
                  "provider:model_id" format (e.g., "anthropic:claude-opus-4-6").

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


def get_local_downloadable_models() -> dict[str, dict]:
    """Return local models that have download metadata (hf_repo + gguf_filename).

    This replaces the former MODEL_CATALOG dict that was hardcoded in
    ``carpenter.agent.providers.local``.  Install tooling can call this to
    discover available local GGUF models and their download coordinates.

    Returns:
        Dict mapping model key to a dict with keys:
        ``repo``, ``filename``, ``size_mb``, ``label``.
    """
    reg = get_registry()
    result = {}
    for key, entry in reg.items():
        if entry.hf_repo and entry.gguf_filename:
            result[key] = {
                "repo": entry.hf_repo,
                "filename": entry.gguf_filename,
                "size_mb": entry.download_size_mb,
                "label": entry.description,
            }
    return result
