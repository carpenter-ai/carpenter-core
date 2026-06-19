"""Tests for the *period*-subject reflection pipeline (daily batch path).

Reflection moved from a per-arc trigger to a daily batch over a typed
``subject`` (``{kind, refs, window}``).  ``test_reflection_cadence.py`` and
``test_reflection_dispatch_actions.py`` already cover the subject helpers,
daily-tick batching, and the dispatch step.  These tests fill the gaps for
the *period* path proper:

- ``activity_gatherer.gather_from_subject`` batch / single-arc / theme
  framing.
- ``step_handlers._is_batch_restricted`` taint roll-up over a list of arcs.
- ``reflection_storage.save_reflection`` KB keying for a period subject
  (vs. the legacy int-arg form).
- An end-to-end run of the three Python step handlers against a real DB
  with a period subject over two completed arcs.

The reflection submodules are only importable under
``carpenter_template_packages.reflection.*`` after the seed templates are
loaded, so all such imports happen inside the ``pkg`` fixture.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import types
from datetime import datetime, timezone

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import (
    handler_registry,
    subscriptions,
    template_manager,
)
from carpenter.core.engine.triggers import registry as trigger_registry
from carpenter.core.workflows._arc_state import get_arc_state, set_arc_state
from carpenter.db import db_connection, db_transaction


TEMPLATES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "config_seed", "templates",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


@pytest.fixture
def pkg(tmp_path):
    """Load seed templates + return the reflection submodules.

    Mirrors the ``pkg`` fixture in test_reflection_cadence.py and the
    handler-registration of test_reflection_dispatch_actions.py: the
    ``carpenter_template_packages.reflection`` namespace only exists after
    the loader runs, so submodule imports must live here.
    """
    trigger_registry.reset()
    subscriptions.reset()
    handler_registry.clear_registry()
    template_manager.load_templates_from_dir(_copy_seed(tmp_path))

    step_handlers = importlib.import_module(
        "carpenter_template_packages.reflection.step_handlers")
    importlib.reload(step_handlers)
    handler_registry.register_step_handler(
        "reflection", "gather-activity", step_handlers.handle_gather_activity)
    handler_registry.register_step_handler(
        "reflection", "save-reflection", step_handlers.handle_save_reflection)
    handler_registry.register_step_handler(
        "reflection", "dispatch-actions", step_handlers.handle_dispatch_actions)

    ns = types.SimpleNamespace(
        subject=importlib.import_module(
            "carpenter_template_packages.reflection._subject"),
        gatherer=importlib.import_module(
            "carpenter_template_packages.reflection.activity_gatherer"),
        storage=importlib.import_module(
            "carpenter_template_packages.reflection.reflection_storage"),
        step_handlers=step_handlers,
    )
    yield ns
    trigger_registry.reset()
    subscriptions.reset()
    handler_registry.clear_registry()


def _insert_arc(name, *, status="completed", parent_id=None, goal=None,
                integrity_level="trusted"):
    """Insert an arc row directly with a recent ISO timestamp."""
    ts = _now_iso()
    with db_transaction() as db:
        cur = db.execute(
            "INSERT INTO arcs (name, status, parent_id, goal, "
            "created_at, updated_at, integrity_level) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, status, parent_id, goal, ts, ts, integrity_level),
        )
        return cur.lastrowid


def _period_subject(refs, date="2026-06-19"):
    return {
        "kind": "period",
        "refs": list(refs),
        "window": {
            "from": "2026-06-18T00:00:00+00:00",
            "to": f"{date}T00:00:00+00:00",
            "date": date,
        },
    }


# ── activity_gatherer.gather_from_subject ───────────────────────────


def test_gather_from_subject_multi_arc_emits_batch_block(pkg):
    """A period subject with multiple arcs returns the batch framing with
    the window, arc count, and each arc id + trajectory."""
    a1 = _insert_arc("goal-alpha", goal="ship the widget")
    a2 = _insert_arc("goal-beta", goal="fix the login")
    _insert_arc("child-of-beta", parent_id=a2, goal="subtask")

    block = pkg.gatherer.gather_from_subject(_period_subject([a1, a2]))

    assert "# Reflection Data — batch of completed arcs" in block
    assert "*batch* of arcs" in block
    assert "arc count: 2" in block
    assert f"#{a1}" in block and f"#{a2}" in block
    # Each arc's single-arc trajectory is inlined.
    assert block.count("single-arc trajectory") == 2


def test_gather_from_subject_single_arc_delegates_to_single_block(pkg):
    """A subject with one arc id uses the single-arc block, NOT the batch
    framing."""
    a1 = _insert_arc("solo-goal", goal="the only arc")

    block = pkg.gatherer.gather_from_subject(_period_subject([a1]))

    assert "# Reflection Data — single-arc trajectory" in block
    assert "batch of completed arcs" not in block
    assert "the only arc" in block


def test_gather_from_subject_theme_empty_prefix_is_graceful(pkg):
    """A theme subject over an empty KB prefix lists the '(none found)'
    sentinel rather than erroring."""
    subject = {"kind": "theme", "theme": "email", "kb_prefix": "skills/email"}

    block = pkg.gatherer.gather_from_subject(subject)

    assert "# Reflection Data — theme: email" in block
    assert "`skills/email`" in block
    assert "(none found under this prefix)" in block


def test_gather_from_subject_theme_lists_kb_updates(pkg):
    """A theme subject reads its KB subtree and lists each child entry."""
    from carpenter.kb import get_store
    store = get_store()
    store.write_entry(
        path="skills/caching/lru",
        content="# LRU\n\nUse an LRU cache for hot reads.\n",
        description="lru caching note",
        validate_links=False,
    )

    subject = {"kind": "theme", "theme": "caching",
               "kb_prefix": "skills/caching"}
    block = pkg.gatherer.gather_from_subject(subject)

    assert "# Reflection Data — theme: caching" in block
    assert "## Updates" in block
    assert "skills/caching/lru" in block
    assert "(none found under this prefix)" not in block


# ── step_handlers._is_batch_restricted ──────────────────────────────


def test_is_batch_restricted_true_when_any_arc_non_trusted(pkg):
    """One non-trusted arc in the batch restricts the whole batch."""
    clean = _insert_arc("clean", integrity_level="trusted")
    tainted = _insert_arc("tainted", integrity_level="untrusted")

    assert pkg.step_handlers._is_batch_restricted([clean, tainted], None) is True


def test_is_batch_restricted_false_when_all_trusted(pkg):
    """All-trusted batch is unrestricted."""
    a1 = _insert_arc("a", integrity_level="trusted")
    a2 = _insert_arc("b", integrity_level="trusted")

    assert pkg.step_handlers._is_batch_restricted([a1, a2], None) is False


def test_is_batch_restricted_empty_list_falls_back(pkg):
    """An empty arc list falls back to the path/category arm via
    ``_is_reflection_restricted(None, ...)`` — with no action, unrestricted."""
    assert pkg.step_handlers._is_batch_restricted([], None) is False


# ── reflection_storage.save_reflection ──────────────────────────────


def _enqueued_kb_writes():
    with db_connection() as db:
        rows = db.execute(
            "SELECT payload_json FROM work_queue WHERE event_type = "
            "'kb.write_entry' ORDER BY id",
        ).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


def test_save_reflection_period_subject_keys_by_day(pkg):
    """A period subject enqueues a kb.write_entry keyed reflections/by-day/."""
    a1 = _insert_arc("goal-1")
    a2 = _insert_arc("goal-2")
    subject = _period_subject([a1, a2], date="2026-06-19")

    pkg.storage.save_reflection(subject, "lessons learned today")

    payloads = _enqueued_kb_writes()
    assert len(payloads) == 1
    assert payloads[0]["kb_path"] == "reflections/by-day/2026-06-19"
    assert payloads[0]["entry_type"] == "reflection"


def test_save_reflection_legacy_int_arg_keys_by_arc(pkg):
    """The legacy single-arc-id call form still keys reflections/by-arc/."""
    a1 = _insert_arc("legacy-goal")

    pkg.storage.save_reflection(a1, "per-arc lesson")

    payloads = _enqueued_kb_writes()
    assert len(payloads) == 1
    assert payloads[0]["kb_path"] == f"reflections/by-arc/{a1}"


# ── end-to-end period pipeline through the three step handlers ──────


def _build_period_reflection_tree(refs, date="2026-06-19"):
    """Instantiate the reflection template and set a period subject.

    Returns (root_id, gather_id, reflect_id, save_id, dispatch_id).
    """
    tmpl = template_manager.get_template_by_name("reflection")
    assert tmpl is not None

    root_id = arc_manager.create_arc(
        name="reflection", goal="reflection", template_id=tmpl["id"])
    arc_manager.update_status(root_id, "active")
    set_arc_state(root_id, "reflection_subject", _period_subject(refs, date))

    template_manager.instantiate_template(tmpl["id"], root_id)
    with db_connection() as db:
        rows = db.execute(
            "SELECT id, name FROM arcs WHERE parent_id = ? ORDER BY step_order",
            (root_id,),
        ).fetchall()
    by_name = {r["name"]: r["id"] for r in rows}
    return (
        root_id,
        by_name["gather-activity"],
        by_name["reflect"],
        by_name["save-reflection"],
        by_name["dispatch-actions"],
    )


def _run(handler, arc_id):
    arc_info = arc_manager.get_arc(arc_id)
    asyncio.run(handler(arc_id, arc_info))


def test_period_pipeline_gather_then_save(pkg):
    """End-to-end: gather populates the reflect arc's goal with the batch
    framing for the period; save enqueues a by-day KB write."""
    a1 = _insert_arc("goal-1", goal="alpha work")
    a2 = _insert_arc("goal-2", goal="beta work")
    root_id, gather_id, reflect_id, save_id, dispatch_id = (
        _build_period_reflection_tree([a1, a2], date="2026-06-19"))

    # gather-activity → reflect arc's goal gets the batch block.
    arc_manager.update_status(gather_id, "active")
    _run(pkg.step_handlers.handle_gather_activity, gather_id)

    with db_connection() as db:
        reflect_goal = db.execute(
            "SELECT goal FROM arcs WHERE id = ?", (reflect_id,),
        ).fetchone()["goal"]
    assert "batch of completed arcs" in reflect_goal
    assert "arc count: 2" in reflect_goal
    assert f"#{a1}" in reflect_goal and f"#{a2}" in reflect_goal

    # reflect produces output, then save-reflection enqueues the KB write.
    set_arc_state(reflect_id, "_agent_response", "Lessons: keep tests small.")
    arc_manager.update_status(reflect_id, "active")
    arc_manager.update_status(reflect_id, "completed")
    arc_manager.update_status(save_id, "active")
    _run(pkg.step_handlers.handle_save_reflection, save_id)

    payloads = _enqueued_kb_writes()
    assert len(payloads) == 1
    assert payloads[0]["kb_path"] == "reflections/by-day/2026-06-19"
    with db_connection() as db:
        save_status = db.execute(
            "SELECT status FROM arcs WHERE id = ?", (save_id,),
        ).fetchone()["status"]
    assert save_status == "completed"


def test_period_pipeline_dispatch_actions_noop_completes(pkg):
    """With no proposed actions in the reflect output, dispatch-actions is a
    clean no-op that still completes."""
    a1 = _insert_arc("goal-1")
    a2 = _insert_arc("goal-2")
    root_id, gather_id, reflect_id, save_id, dispatch_id = (
        _build_period_reflection_tree([a1, a2]))

    # Empty reflect output → no proposed actions parsed → no-op dispatch.
    set_arc_state(reflect_id, "_agent_response", "")
    arc_manager.update_status(reflect_id, "active")
    arc_manager.update_status(reflect_id, "completed")
    arc_manager.update_status(dispatch_id, "active")

    _run(pkg.step_handlers.handle_dispatch_actions, dispatch_id)

    resp = get_arc_state(dispatch_id, "_agent_response")
    assert resp["spawned_arcs"] == []
    assert resp["total_proposed"] == 0
    with db_connection() as db:
        status = db.execute(
            "SELECT status FROM arcs WHERE id = ?", (dispatch_id,),
        ).fetchone()["status"]
    assert status == "completed"
