"""Tests for reflection KB entry construction.

Reflection persists via KB only. The per-arc layout writes to
``reflections/by-arc/{arc_id}`` with ``reflected_arc_id`` in
frontmatter; the legacy cadence layout (``reflections/{cadence}/{date}``)
is retained so :func:`reflection_storage.get_reflections`' fallback
stays exercised.

After Phase D PR-D the platform ``kb.write_entry`` handler is generic —
path shape + frontmatter formatting live in the reflection template
package at :mod:`carpenter_template_packages.reflection.kb_entry`.
"""

from __future__ import annotations

import os
import shutil

import pytest

from carpenter.core.engine import (
    handler_registry,
    subscriptions,
    template_manager,
)
from carpenter.core.engine.triggers import registry as trigger_registry


TEMPLATES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "config_seed", "templates",
)


@pytest.fixture(autouse=True)
def _load_reflection_package(tmp_path):
    """Load the reflection template package into ``sys.modules``.

    Template packages are imported under a synthetic
    ``carpenter_template_packages.<pkg>`` namespace by the engine's
    template loader. We call it here so the package is importable for
    the duration of the test. Registries are reset around each test.
    """
    trigger_registry.reset()
    subscriptions.reset()
    handler_registry.clear_registry()

    dest = str(tmp_path / "templates")
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(TEMPLATES_DIR):
        src = os.path.join(TEMPLATES_DIR, f)
        if os.path.isfile(src) and f.endswith((".yaml", ".yml")):
            shutil.copy(src, dest)
        elif os.path.isdir(src) and not f.startswith((".", "_")):
            shutil.copytree(src, os.path.join(dest, f))
    template_manager.load_templates_from_dir(dest)

    yield

    trigger_registry.reset()
    subscriptions.reset()
    handler_registry.clear_registry()


def test_per_arc_entry_writes_by_arc_path():
    from carpenter.kb import get_store
    from carpenter_template_packages.reflection.kb_entry import (
        create_reflection_entry,
    )

    store = get_store()
    path = create_reflection_entry(
        store,
        reflected_arc_id=42,
        content="Observed high tool usage patterns today.",
        model="anthropic:haiku",
        period_start="2025-01-01T00:00:00+00:00",
        period_end="2025-01-01T12:00:00+00:00",
    )
    assert path == "reflections/by-arc/42"

    entry = store.get_entry(path)
    assert entry is not None
    assert "tool usage" in entry["content"].lower()
    assert entry["entry_type"] == "reflection"
    # Frontmatter carries the reflected_arc_id for downstream readers.
    assert "reflected_arc_id: 42" in entry["content"]


def test_returns_none_without_content():
    from carpenter.kb import get_store
    from carpenter_template_packages.reflection.kb_entry import (
        create_reflection_entry,
    )

    store = get_store()
    path = create_reflection_entry(
        store, reflected_arc_id=43, content="",
    )
    assert path is None


def test_includes_proposed_actions():
    from carpenter.kb import get_store
    from carpenter_template_packages.reflection.kb_entry import (
        create_reflection_entry,
    )

    store = get_store()
    path = create_reflection_entry(
        store,
        reflected_arc_id=44,
        content="Weekly review of activity.",
        proposed_actions="Reduce API calls by caching responses.",
        period_start="2025-01-01",
        period_end="2025-01-07",
    )
    assert path is not None

    entry = store.get_entry(path)
    assert "caching responses" in entry["content"].lower()


def test_legacy_cadence_entry():
    """Without reflected_arc_id, writes to the legacy cadence path."""
    from carpenter.kb import get_store
    from carpenter_template_packages.reflection.kb_entry import (
        create_reflection_entry,
    )

    store = get_store()
    path = create_reflection_entry(
        store,
        reflected_arc_id=None,
        content="Daily summary.",
        cadence="daily",
        period_start="2025-01-01",
        period_end="2025-01-01T23:59:59+00:00",
    )
    assert path == "reflections/daily/2025-01-01"


def test_build_reflection_entry_returns_payload():
    """``build_reflection_entry`` produces a serializable payload
    suitable for enqueueing under the platform ``kb.write_entry``
    work-item type — no KB write performed."""
    from carpenter_template_packages.reflection.kb_entry import (
        build_reflection_entry,
    )

    payload = build_reflection_entry(
        reflected_arc_id=77,
        content="Some findings from the arc.",
        model="haiku",
        period_start="2026-04-15",
        period_end="2026-04-15",
    )
    assert payload is not None
    assert payload["kb_path"] == "reflections/by-arc/77"
    assert payload["entry_type"] == "reflection"
    assert "reflected_arc_id: 77" in payload["content"]
    assert "# Reflection on arc #77" in payload["content"]


def test_build_reflection_entry_returns_none_without_content():
    from carpenter_template_packages.reflection.kb_entry import (
        build_reflection_entry,
    )

    assert build_reflection_entry(reflected_arc_id=1, content="") is None
    assert build_reflection_entry(reflected_arc_id=1, content=None) is None
