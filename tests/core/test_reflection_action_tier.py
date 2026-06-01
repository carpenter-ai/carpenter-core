"""Reflection action-routing tier gating (PR 4 platform-integrity).

Covers the broadened ``_is_reflection_restricted`` predicate that
replaces ``_is_reflected_arc_tainted``.  The predicate still uses
taint as its primary signal — the path-tier / change-category arms
are wired but unreachable until PR 5 extends
``parse_proposed_actions`` to surface a structured ``target_path``.

These tests pin:

1. The clean-trusted case still routes to the auto variant.
2. A tainted reflection still routes to the gated variant
   (existing behavior preserved through the rename).
3. The broadened predicate, when invoked directly with a
   proposed-action target inside the platform tree, returns
   restricted=True — confirming the path/category arms work even
   though the production call site does not yet supply them.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import shutil

import pytest

from carpenter import config as carpenter_config
from carpenter.core.engine import (
    handler_registry,
    subscriptions,
    template_manager,
)
from carpenter.core.engine.triggers import registry as trigger_registry
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.workflows._arc_state import set_arc_state, get_arc_state


TEMPLATES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "config_seed", "templates",
)


@pytest.fixture(autouse=True)
def _reset():
    trigger_registry.reset()
    subscriptions.reset()
    handler_registry.clear_registry()
    yield
    trigger_registry.reset()
    subscriptions.reset()
    handler_registry.clear_registry()


def _copy_seed(tmp_path):
    dest = str(tmp_path / "templates")
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(TEMPLATES_DIR):
        src = os.path.join(TEMPLATES_DIR, f)
        if os.path.isfile(src) and f.endswith((".yaml", ".yml")):
            shutil.copy(src, dest)
        elif os.path.isdir(src) and not f.startswith((".", "_")):
            shutil.copytree(src, os.path.join(dest, f))
    return dest


def _load_package_handlers(tmp_path):
    dest = _copy_seed(tmp_path)
    template_manager.load_templates_from_dir(dest)
    step_handlers = importlib.import_module(
        "carpenter_template_packages.reflection.step_handlers",
    )
    importlib.reload(step_handlers)
    handler_registry.register_step_handler(
        "reflection", "dispatch-actions", step_handlers.handle_dispatch_actions,
    )
    return step_handlers


def _make_reflection_arc_tree(reflect_response):
    tmpl = template_manager.get_template_by_name("reflection")
    assert tmpl is not None
    root_id = arc_manager.create_arc(
        name="reflection", goal="reflection", template_id=tmpl["id"],
    )
    arc_manager.update_status(root_id, "active")
    template_manager.instantiate_template(tmpl["id"], root_id)
    from carpenter.db import db_connection
    with db_connection() as db:
        rows = db.execute(
            "SELECT id, name FROM arcs WHERE parent_id = ? ORDER BY step_order",
            (root_id,),
        ).fetchall()
    by_name = {row["name"]: row["id"] for row in rows}

    reflect_id = by_name["reflect"]
    dispatch_id = by_name["dispatch-actions"]

    arc_manager.update_status(reflect_id, "active")
    if reflect_response is not None:
        set_arc_state(reflect_id, "_agent_response", reflect_response)
    arc_manager.update_status(reflect_id, "completed")
    arc_manager.update_status(by_name["gather-activity"], "active")
    arc_manager.update_status(by_name["gather-activity"], "completed")
    arc_manager.update_status(by_name["save-reflection"], "active")
    arc_manager.update_status(by_name["save-reflection"], "completed")
    arc_manager.update_status(dispatch_id, "active")
    return root_id, reflect_id, dispatch_id


def _run(handler, arc_id):
    arc_info = arc_manager.get_arc(arc_id)
    asyncio.run(handler(arc_id, arc_info))


# ── End-to-end: clean trusted reflection routes to auto variant ─────────


def test_clean_trusted_reflection_uses_auto_variant(tmp_path):
    """Regression guard for the rename: a clean reflection with no
    proposed-action target paths still uses the auto-variant template.

    Mirrors ``test_clean_reflection_spawns_auto_template`` in
    ``test_reflection_dispatch_actions.py`` but lives here so it
    runs alongside the new tier-specific tests and the suite breaks
    fast if the rename regresses the happy path.
    """
    step_handlers = _load_package_handlers(tmp_path)
    trusted_id = arc_manager.create_arc(
        name="user-goal-trusted", goal="clean reflection source",
    )
    reflect_output = (
        "- create kb entry on error budgets\n"
        "- implement code fix for the login bug\n"
    )
    root_id, _, dispatch_id = _make_reflection_arc_tree(reflect_output)
    set_arc_state(root_id, "reflected_arc_id", trusted_id)

    _run(step_handlers.handle_dispatch_actions, dispatch_id)

    resp = get_arc_state(dispatch_id, "_agent_response")
    assert resp["tainted"] is False
    kb_auto = template_manager.get_template_by_name("reflection-kb-action")
    code_auto = template_manager.get_template_by_name("reflection-code-action")
    for spawned_id in resp["spawned_arcs"]:
        arc = arc_manager.get_arc(spawned_id)
        assert arc["template_id"] in (kb_auto["id"], code_auto["id"])
        assert get_arc_state(spawned_id, "_review_mode") is None


# ── End-to-end: tainted reflection still uses gated variant ─────────────


def test_tainted_reflection_uses_gated_variant(tmp_path):
    """The taint arm of the broadened predicate still fires —
    existing behavior preserved through the rename."""
    step_handlers = _load_package_handlers(tmp_path)
    tainted_id = arc_manager._insert_arc(
        name="user-goal-tainted", goal="something tainted",
        integrity_level="untrusted",
    )
    reflect_output = "- create kb entry on the issue\n"
    root_id, _, dispatch_id = _make_reflection_arc_tree(reflect_output)
    set_arc_state(root_id, "reflected_arc_id", tainted_id)

    _run(step_handlers.handle_dispatch_actions, dispatch_id)

    resp = get_arc_state(dispatch_id, "_agent_response")
    assert resp["tainted"] is True
    kb_gated = template_manager.get_template_by_name(
        "reflection-kb-action-gated",
    )
    for spawned_id in resp["spawned_arcs"]:
        arc = arc_manager.get_arc(spawned_id)
        assert arc["template_id"] == kb_gated["id"]
        assert get_arc_state(spawned_id, "_review_mode") == "human"


# ── Direct predicate: path/category arms ────────────────────────────────


def test_predicate_restricts_proposed_t1_target(monkeypatch, tmp_path):
    """Direct call: when a proposed action targets a T1 path, the
    predicate returns True via the tier arm.

    This exercises the wired-but-unreachable path arm.  Until PR 5
    teaches ``parse_proposed_actions`` to surface ``target_path``, the
    production code path is taint-only — but the predicate itself is
    already wired so PR 5 only has to extend the parser.
    """
    monkeypatch.setitem(
        carpenter_config.CONFIG, "repo_dir", str(tmp_path / "repo"),
    )
    # The path need not exist; classification is realpath-based and
    # uses the repo-root prefix list, not filesystem presence.
    t1_target = str(tmp_path / "repo" / "carpenter" / "security" / "judge.py")

    # Templates must be loaded once so the synthetic
    # ``carpenter_template_packages.reflection`` package is registered
    # in sys.modules; otherwise the import below raises ModuleNotFoundError.
    _load_package_handlers(tmp_path)
    step_handlers = importlib.import_module(
        "carpenter_template_packages.reflection.step_handlers",
    )

    trusted_id = arc_manager.create_arc(
        name="user-goal-trusted", goal="reflection source",
    )
    assert step_handlers._is_reflection_restricted(
        trusted_id, {"target_path": t1_target},
    ) is True


def test_predicate_allows_proposed_t2_kb_target(monkeypatch, tmp_path):
    """Direct call: a proposed action targeting a T2 KB path with a kb
    category returns False — confirming the broader arms don't over-
    refuse legitimate user-home edits.
    """
    monkeypatch.setitem(
        carpenter_config.CONFIG, "repo_dir", str(tmp_path / "repo"),
    )
    kb_target = str(tmp_path / "user" / "kb" / "note.md")

    _load_package_handlers(tmp_path)
    step_handlers = importlib.import_module(
        "carpenter_template_packages.reflection.step_handlers",
    )

    trusted_id = arc_manager.create_arc(
        name="user-goal-trusted", goal="reflection source",
    )
    assert step_handlers._is_reflection_restricted(
        trusted_id, {"target_path": kb_target},
    ) is False


def test_legacy_alias_still_works(tmp_path):
    """``_is_reflected_arc_tainted`` is preserved as an alias so any
    out-of-tree caller (e.g. tests that import the name directly)
    continues to work after the rename."""
    _load_package_handlers(tmp_path)
    step_handlers = importlib.import_module(
        "carpenter_template_packages.reflection.step_handlers",
    )
    assert hasattr(step_handlers, "_is_reflected_arc_tainted")
    # Untrusted arc → True via the alias.
    tainted_id = arc_manager._insert_arc(
        name="alias-check", integrity_level="untrusted",
    )
    assert step_handlers._is_reflected_arc_tainted(tainted_id) is True
