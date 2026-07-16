"""Tests for the per-root-arc-completion reflection trigger.

Replaces the old cadence-based trigger suite. Covers:

- The template's ``triggers:`` section is picked up at load time and
  registered as a subscription.
- The subscription's filter correctly gates on ``is_root=True`` +
  ``new_status=completed`` + ``template_name`` not in the reflection
  pipeline's own meta-templates (reflection, skill-kb-review).
- ``$ne`` / ``$nin`` filter semantics (incl. absent-key) in
  ``filter_matches``.
- ``handle_subscription_create_arc`` threads through ``priority`` and
  ``initial_arc_state`` (including ``{event.payload.X}`` substitution).
- ``arc.status_changed`` event payload now includes ``template_name``
  when the arc was created from a template.
- ``activity_gatherer.gather_from_arc`` returns a markdown block
  mentioning the reflected arc's id, goal, and children.
- KB entries land at ``reflections/by-arc/{arc_id}``.
- Legacy ``reflections/daily/*.md`` entries remain readable via
  :func:`get_reflections`.
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
from carpenter.core.engine._utils import filter_matches
from carpenter.core.engine.triggers import registry as trigger_registry


TEMPLATES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "config_seed", "templates",
)


def _copy_seed(tmp_path):
    dest = str(tmp_path / "templates")
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(TEMPLATES_DIR):
        src = os.path.join(TEMPLATES_DIR, f)
        if os.path.isfile(src) and f.endswith((".yaml", ".yml")):
            shutil.copy(src, dest)
        elif os.path.isdir(src) and not f.startswith((".", "_")):
            target = os.path.join(dest, f)
            # Idempotent: the autouse fixture seeds the dir before the
            # test body runs, so individual tests calling _copy_seed
            # again should be a no-op for already-present subdirs.
            if not os.path.exists(target):
                shutil.copytree(src, target)
    return dest


@pytest.fixture(autouse=True)
def _reset(tmp_path):
    """Reset engine registries and load the reflection template package.

    Loading the template package here (rather than relying on a sibling
    test in this file to do it first) makes every test in the module
    self-contained. Without this, xdist load-balancing can dispatch
    individual tests to a worker that hasn't yet executed any sibling
    that imports / loads the synthetic
    ``carpenter_template_packages.reflection`` namespace, causing
    ``ModuleNotFoundError`` on direct imports inside the test body.
    """
    trigger_registry.reset()
    subscriptions.reset()
    handler_registry.clear_registry()

    # Load templates so ``carpenter_template_packages.reflection`` is
    # registered in ``sys.modules`` for the duration of the test.
    dest = _copy_seed(tmp_path)
    template_manager.load_templates_from_dir(dest)

    yield
    trigger_registry.reset()
    subscriptions.reset()
    handler_registry.clear_registry()


# ── Filter semantics ────────────────────────────────────────────────


class TestFilterNotEquals:
    def test_ne_matches_when_value_differs(self):
        assert filter_matches({"x": {"$ne": "a"}}, {"x": "b"})

    def test_ne_rejects_when_value_equals(self):
        assert not filter_matches({"x": {"$ne": "a"}}, {"x": "a"})

    def test_ne_matches_when_key_absent(self):
        # Non-template arcs have no ``template_name`` — they must still
        # pass ``template_name != reflection``.
        assert filter_matches({"x": {"$ne": "a"}}, {})

    def test_ne_combined_with_eq(self):
        flt = {"is_root": True, "template_name": {"$ne": "reflection"}}
        assert filter_matches(flt, {"is_root": True, "template_name": "other"})
        assert filter_matches(flt, {"is_root": True})  # absent
        assert not filter_matches(flt, {"is_root": True, "template_name": "reflection"})
        assert not filter_matches(flt, {"is_root": False})


class TestFilterNotIn:
    def test_nin_matches_when_value_absent_from_list(self):
        assert filter_matches({"x": {"$nin": ["a", "b"]}}, {"x": "c"})

    def test_nin_rejects_when_value_in_list(self):
        assert not filter_matches({"x": {"$nin": ["a", "b"]}}, {"x": "a"})
        assert not filter_matches({"x": {"$nin": ["a", "b"]}}, {"x": "b"})

    def test_nin_matches_when_key_absent(self):
        # Non-template arcs have no ``template_name`` — they must still
        # pass a ``$nin`` exclusion of meta-templates.
        assert filter_matches({"x": {"$nin": ["a", "b"]}}, {})


# ── Reflection is cadence-driven, NOT per-arc-completion ────────────


def test_reflection_declares_no_arc_completion_trigger(tmp_path):
    """Reflection must NOT register an ``arc.status_changed`` subscription.

    The per-arc-completion trigger could form an unbounded feedback loop
    (reflection → skills/ write → skill-kb-review root arc → its completion
    re-triggers reflection → ...). Reflection is now driven by a daily
    cadence (see ``daily_tick.py`` + the cron registered in ``__init__.py``)
    over a bounded batch, which cannot loop.
    """
    dest = _copy_seed(tmp_path)
    template_manager.load_templates_from_dir(dest)
    template_manager.load_template_triggers()

    refl_subs = [
        s for s in subscriptions.get_subscriptions()
        if s.event_type == "arc.status_changed"
    ]
    assert refl_subs == [], (
        "reflection must not subscribe to arc.status_changed — that was the "
        "per-arc trigger whose loop burned credits"
    )


# ── arc.status_changed payload includes template_name ───────────────


def test_arc_status_changed_includes_template_name(tmp_path):
    """Root arcs created from a template surface ``template_name`` in
    the emitted ``arc.status_changed`` payload."""
    from carpenter.core.arcs import manager as arc_manager
    from carpenter.db import get_db
    import json as _json

    dest = _copy_seed(tmp_path)
    template_manager.load_templates_from_dir(dest)
    tmpl = template_manager.get_template_by_name("reflection")
    assert tmpl is not None

    parent_id = arc_manager.create_arc(
        name="reflection",
        goal="test",
        template_id=tmpl["id"],
    )
    arc_manager.update_status(parent_id, "active")
    arc_manager.update_status(parent_id, "completed")

    db = get_db()
    try:
        rows = db.execute(
            "SELECT payload_json FROM events WHERE event_type = ? "
            "ORDER BY id DESC LIMIT 20",
            ("arc.status_changed",),
        ).fetchall()
    finally:
        db.close()

    payloads = [_json.loads(r["payload_json"]) for r in rows]
    hits = [
        p for p in payloads
        if p.get("arc_id") == parent_id and p.get("new_status") == "completed"
    ]
    assert hits, "expected a completion event for the reflection-template arc"
    assert hits[0].get("template_name") == "reflection"

    # Also verify a non-template arc emits without template_name.
    plain_id = arc_manager.create_arc(name="user-goal", goal="do stuff")
    arc_manager.update_status(plain_id, "active")
    arc_manager.update_status(plain_id, "completed")

    db = get_db()
    try:
        rows = db.execute(
            "SELECT payload_json FROM events WHERE event_type = ? "
            "ORDER BY id DESC LIMIT 20",
            ("arc.status_changed",),
        ).fetchall()
    finally:
        db.close()
    plain_hits = [
        _json.loads(r["payload_json"]) for r in rows
        if _json.loads(r["payload_json"]).get("arc_id") == plain_id
        and _json.loads(r["payload_json"]).get("new_status") == "completed"
    ]
    assert plain_hits
    assert "template_name" not in plain_hits[0]


# ── handle_subscription_create_arc threads priority + initial state ─


def test_subscription_create_arc_sets_priority_and_state(tmp_path):
    """Subscription action fields ``priority`` and ``initial_arc_state``
    land on the created arc, with ``{event.payload.X}`` substitution."""
    from carpenter.core.arcs import manager as arc_manager
    from carpenter.core.engine import subscriptions as _subs
    from carpenter.core.workflows._arc_state import get_arc_state

    dest = _copy_seed(tmp_path)
    template_manager.load_templates_from_dir(dest)
    tmpl = template_manager.get_template_by_name("reflection")

    parent_id = _subs.handle_subscription_create_arc({
        "template_id": tmpl["id"],
        "template_name": "reflection",
        "arc_name": "reflection",
        "priority": 1000,
        "initial_arc_state": {
            "reflected_arc_id": "{event.payload.arc_id}",
        },
        "_event_payload": {"arc_id": 99},
        "_subscription": "reflection:on-root-arc-completed",
    })
    assert parent_id is not None

    arc = arc_manager.get_arc(parent_id)
    assert arc["priority"] == 1000
    assert get_arc_state(parent_id, "reflected_arc_id") == 99


# ── gather_from_arc output shape ────────────────────────────────────


def test_gather_from_arc_produces_expected_shape(tmp_path):
    """The gatherer emits markdown with the reflected arc's id, goal, and
    child steps."""
    # Load the template package so the synthetic
    # ``carpenter_template_packages.reflection`` namespace is wired up.
    dest = _copy_seed(tmp_path)
    template_manager.load_templates_from_dir(dest)
    import importlib
    activity_gatherer = importlib.import_module(
        "carpenter_template_packages.reflection.activity_gatherer",
    )

    from carpenter.core.arcs import manager as arc_manager

    root_id = arc_manager.create_arc(
        name="user-goal",
        goal="Refactor the widget module.",
    )
    arc_manager.update_status(root_id, "active")
    child_id = arc_manager.add_child(
        root_id, name="plan", goal="Outline the refactor.",
    )
    arc_manager.update_status(child_id, "active")
    arc_manager.update_status(child_id, "completed")
    arc_manager.update_status(root_id, "completed")

    md = activity_gatherer.gather_from_arc(root_id)
    assert f"#{root_id}" in md
    assert "Refactor the widget module" in md
    assert "plan" in md
    assert "Period Stats" in md


# The v2 pipeline no longer writes diary KB entries. The two tests that
# used to live here (``test_kb_entry_lands_under_by_arc``,
# ``test_get_reflections_reads_legacy_cadence_layout``) exercised the
# now-dead ``create_reflection_entry`` path. Content-hash dedupe is
# covered by ``tests/kb/test_store.py`` and no-diary-write is covered
# by ``tests/core/test_reflection_v2_triage.py``.
