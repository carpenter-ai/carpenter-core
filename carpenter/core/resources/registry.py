"""Resource template registry.

Loads ``config_seed/resource_templates.yaml`` lazily on first access and
caches the parsed entries in a module-level dict.  Templates describe
how an untrusted Resource (e.g. a raw HTML ingest) can be transformed,
via a sandboxed template arc, into a derived Resource with a declared
``produces_content_type``.  The JUDGE verdict on that template arc is
what promotes the derived Resource's trust.

This module is a pure lookup layer — it does NOT instantiate arcs, run
templates, or validate that a given template name is referenced
elsewhere.  Callers (PR3's ``fetch_web_content`` rewrite) use
``get_template_for(content_type)`` to pick a template when ingesting
new content, and consult the returned dict for ``reviewer_profile`` /
``judge_profile`` / ``model_policy`` / ``goal_template`` when wiring
the resulting arcs.
"""

from __future__ import annotations

import logging
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is a hard dep in carpenter
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# Module-level cache.  Keyed by template name.
_templates: dict[str, dict] | None = None


def _seed_path() -> Path:
    """Return the config_seed/resource_templates.yaml path.

    Same resolution pattern as ``carpenter.db._config_seed_dir``: walk up
    from this file to the package root and into ``config_seed``.
    """
    # carpenter/core/resources/registry.py -> carpenter/core/resources -> carpenter/core -> carpenter -> repo
    return Path(__file__).resolve().parents[3] / "config_seed" / "resource_templates.yaml"


def _load() -> dict[str, dict]:
    """Parse the YAML file into a {name: template_dict} mapping."""
    if yaml is None:
        logger.warning("PyYAML not available; resource template registry empty")
        return {}

    path = _seed_path()
    if not path.exists():
        logger.warning("resource_templates.yaml not found at %s", path)
        return {}

    with open(path, "r") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        logger.warning("resource_templates.yaml: top-level is not a mapping")
        return {}

    raw = data.get("templates", [])
    if not isinstance(raw, list):
        logger.warning("resource_templates.yaml: 'templates' is not a list")
        return {}

    result: dict[str, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            logger.warning("resource_templates.yaml: skipping entry without name")
            continue
        result[name] = dict(entry)
    return result


def _ensure_loaded() -> dict[str, dict]:
    global _templates
    if _templates is None:
        _templates = _load()
    return _templates


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_template_for(content_type: str) -> dict | None:
    """Return the first template whose ``consumes_content_type`` matches.

    Lookup is by the raw-ingest content type (e.g. ``'html'``).  Returns
    the template dict or ``None`` if no template handles that type.
    """
    if not content_type:
        return None
    for entry in _ensure_loaded().values():
        if entry.get("consumes_content_type") == content_type:
            return dict(entry)
    return None


def get_template_by_name(name: str) -> dict | None:
    """Return a template dict by name, or ``None`` if unknown."""
    if not name:
        return None
    entry = _ensure_loaded().get(name)
    return dict(entry) if entry is not None else None


def list_templates() -> list[dict]:
    """Return a list of all loaded template dicts (copy)."""
    return [dict(v) for v in _ensure_loaded().values()]


def reload_templates() -> None:
    """Clear the cache so the next access re-reads the YAML file.

    Primarily useful for tests that want to assert load behaviour or
    reload after writing a test YAML.
    """
    global _templates
    _templates = None
