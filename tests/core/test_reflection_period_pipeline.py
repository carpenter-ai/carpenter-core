"""Tests for the *period*-subject reflection pipeline (daily batch path).

Reflection moved from a per-arc trigger to a daily batch over a typed
``subject`` (``{kind, refs, window}``).  ``test_reflection_cadence.py`` and
``test_reflection_dispatch_actions.py`` already cover the subject helpers,
daily-tick batching, and the dispatch step.  These tests fill the gaps for
the *period* path proper:

- ``activity_gatherer.gather_from_subject`` batch / single-arc / theme
  framing.
- ``reflection_storage.save_reflection`` is now a **v2 no-op** (the
  diary write paths were removed); the test below asserts nothing is
  enqueued.
- An end-to-end run of the Python step handlers against a real DB
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
        "reflection", "reflect", step_handlers.handle_reflect_gated)
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


@pytest.fixture
def stub_invoke(monkeypatch):
    """Stub ``handle_invoke_coding_change`` so dispatch tests don't need a
    fully-wired platform_server_dir + workspace_manager setup. The invoke
    entry point is covered by ``tests/templates/test_workflow_selection.py``.
    """
    from carpenter.tool_backends import arc as arc_backend

    calls: list[dict] = []

    def fake_invoke(params: dict) -> dict:
        calls.append(dict(params))
        arc_id = arc_manager.create_arc(
            name="coding-change-stub", goal=params.get("prompt", ""),
        )
        return {"arc_id": arc_id}

    monkeypatch.setattr(arc_backend, "handle_invoke_coding_change", fake_invoke)
    return calls


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


# ── reflection_storage.save_reflection ──────────────────────────────


def _enqueued_kb_writes():
    with db_connection() as db:
        rows = db.execute(
            "SELECT payload_json FROM work_queue WHERE event_type = "
            "'kb.write_entry' ORDER BY id",
        ).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


def test_save_reflection_v2_period_subject_is_noop(pkg):
    """v2 removed the diary write paths — save_reflection() is a no-op."""
    a1 = _insert_arc("goal-1")
    a2 = _insert_arc("goal-2")
    subject = _period_subject([a1, a2], date="2026-06-19")

    pkg.storage.save_reflection(subject, "lessons learned today")

    payloads = _enqueued_kb_writes()
    assert payloads == []


def test_save_reflection_v2_legacy_int_arg_is_noop(pkg):
    """v2 removed the diary write paths — even the legacy int-arg call
    form no longer enqueues a kb.write_entry."""
    a1 = _insert_arc("legacy-goal")

    pkg.storage.save_reflection(a1, "per-arc lesson")

    payloads = _enqueued_kb_writes()
    assert payloads == []


# ── end-to-end period pipeline through the three step handlers ──────


def _build_period_reflection_tree(refs, date="2026-06-19"):
    """Instantiate the reflection template and set a period subject.

    Returns (root_id, gather_id, triage_id, reflect_id, save_id, dispatch_id).
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
        by_name["triage"],
        by_name["reflect"],
        by_name["save-reflection"],
        by_name["dispatch-actions"],
    )


def _set_triage(triage_id, needs_synthesis, focus_pointers=None, reasons=None):
    """Convenience: set the triage arc's _agent_response to a valid TriageResult JSON."""
    payload = json.dumps({
        "needs_synthesis": bool(needs_synthesis),
        "reasons": reasons or [],
        "focus_pointers": focus_pointers or [],
    })
    set_arc_state(triage_id, "_agent_response", payload)
    arc_manager.update_status(triage_id, "active")
    arc_manager.update_status(triage_id, "completed")


def _run(handler, arc_id):
    arc_info = arc_manager.get_arc(arc_id)
    asyncio.run(handler(arc_id, arc_info))


def test_period_pipeline_gather_then_save(pkg):
    """v2 end-to-end: gather writes typed GatheredActivity; save-reflection
    records provenance on arc_state and does NOT write to KB (diary
    write paths removed)."""
    from carpenter.core.arcs.dispatch_handler import (
        _render_goal_from_sibling_output,
    )

    a1 = _insert_arc("goal-1", goal="alpha work")
    a2 = _insert_arc("goal-2", goal="beta work")
    root_id, gather_id, triage_id, reflect_id, save_id, dispatch_id = (
        _build_period_reflection_tree([a1, a2], date="2026-06-19"))

    # gather-activity → typed GatheredActivity output on the gather arc;
    # the reflect arc's goal column stays as the static step description.
    arc_manager.update_status(gather_id, "active")
    _run(pkg.step_handlers.handle_gather_activity, gather_id)

    # Dispatch-time goal rendering picks up the sibling's typed output.
    rendered = _render_goal_from_sibling_output(reflect_id)
    assert rendered is not None
    assert "batch of completed arcs" in rendered
    assert "arc count: 2" in rendered
    assert f"#{a1}" in rendered and f"#{a2}" in rendered

    # The reflect arc's goal column was NOT mutated by the gather handler.
    with db_connection() as db:
        reflect_goal = db.execute(
            "SELECT goal FROM arcs WHERE id = ?", (reflect_id,),
        ).fetchone()["goal"]
    assert "batch of completed arcs" not in reflect_goal

    # reflect produces output, then save-reflection records provenance
    # ONLY — no KB write is enqueued by save-reflection in v2.
    set_arc_state(reflect_id, "_agent_response", "Lessons: keep tests small.")
    arc_manager.update_status(reflect_id, "active")
    arc_manager.update_status(reflect_id, "completed")
    arc_manager.update_status(save_id, "active")
    _run(pkg.step_handlers.handle_save_reflection, save_id)

    payloads = _enqueued_kb_writes()
    assert payloads == [], (
        "v2 save-reflection must not enqueue any kb.write_entry "
        "(diary write paths removed)"
    )
    with db_connection() as db:
        save_status = db.execute(
            "SELECT status FROM arcs WHERE id = ?", (save_id,),
        ).fetchone()["status"]
    assert save_status == "completed"
    # Provenance was recorded on the arc.
    prov = get_arc_state(save_id, "_agent_response")
    assert isinstance(prov, dict)
    assert prov["summary"] == "Lessons: keep tests small."
    assert prov["proposed_action_count"] == 0


def test_period_pipeline_dispatch_actions_noop_completes(pkg):
    """With no proposed actions in the reflect output, dispatch-actions is a
    clean no-op that still completes."""
    a1 = _insert_arc("goal-1")
    a2 = _insert_arc("goal-2")
    root_id, gather_id, triage_id, reflect_id, save_id, dispatch_id = (
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


# ── typed contract round-trip ───────────────────────────────────────


def test_gather_activity_writes_typed_output(pkg):
    """gather-activity writes a GatheredActivity retrievable via typed read."""
    from carpenter.core.arcs.data_model_validation import validate_contract
    from carpenter.core.engine.arc_outputs import get_arc_output

    a1 = _insert_arc("goal-1", goal="alpha work")
    a2 = _insert_arc("goal-2", goal="beta work")
    root_id, gather_id, triage_id, reflect_id, _, _ = (
        _build_period_reflection_tree([a1, a2], date="2026-06-19"))

    arc_manager.update_status(gather_id, "active")
    _run(pkg.step_handlers.handle_gather_activity, gather_id)

    raw = get_arc_output(gather_id, "gathered_activity")
    assert raw is not None, "gather-activity did not write its typed output"

    model = validate_contract(raw, "data_models.reflection:GatheredActivity")
    assert model.subject_kind == "period"
    assert sorted(model.source_arc_ids) == sorted([a1, a2])
    assert model.window and model.window.get("date") == "2026-06-19"
    assert "batch of completed arcs" in model.content
    # v2: triage_summary is populated alongside the full content.
    assert model.triage_summary
    assert "Triage Summary" in model.triage_summary


def test_dispatch_reads_structured_proposed_actions(pkg, stub_invoke):
    """dispatch-actions reads the typed ReflectionResult from the reflect arc."""
    a1 = _insert_arc("goal-1")
    a2 = _insert_arc("goal-2")
    _, _, triage_id, reflect_id, _, dispatch_id = (
        _build_period_reflection_tree([a1, a2]))

    payload = json.dumps({
        "summary": "noticed a recurring login regression",
        "proposed_actions": [
            {
                "description": "Document the LRU caching pattern",
                "target_path": None,
                "action_type": "kb",
            },
            {
                "description": "Tighten the login retry loop",
                "target_path": None,
                "action_type": "code",
            },
        ],
    })
    set_arc_state(reflect_id, "_agent_response", payload)
    arc_manager.update_status(reflect_id, "active")
    arc_manager.update_status(reflect_id, "completed")
    arc_manager.update_status(dispatch_id, "active")

    _run(pkg.step_handlers.handle_dispatch_actions, dispatch_id)

    resp = get_arc_state(dispatch_id, "_agent_response")
    assert resp["total_proposed"] == 2
    assert resp["action_types"] == ["kb", "code"]
    assert len(resp["spawned_arcs"]) == 2


def test_end_to_end_typed_contracts(pkg, stub_invoke):
    """v2 full pipeline: gather → (triage flagged) reflect output →
    save-reflection provenance (no KB write) → dispatch-actions spawns
    one kb-type child."""
    a1 = _insert_arc("goal-1", goal="alpha work")
    a2 = _insert_arc("goal-2", goal="beta work")
    root_id, gather_id, triage_id, reflect_id, save_id, dispatch_id = (
        _build_period_reflection_tree([a1, a2], date="2026-06-20"))

    arc_manager.update_status(gather_id, "active")
    _run(pkg.step_handlers.handle_gather_activity, gather_id)

    _set_triage(triage_id, needs_synthesis=True,
                focus_pointers=[f"#{a1}", f"#{a2}"])

    payload = json.dumps({
        "summary": "the two arcs shared a flaky-test pattern",
        "proposed_actions": [
            {
                "description": "create kb entry on flaky-test triage",
                "target_path": None,
                "action_type": "kb",
            },
        ],
        "kb_edit_targets": [],
    })
    set_arc_state(reflect_id, "_agent_response", payload)
    arc_manager.update_status(reflect_id, "active")
    arc_manager.update_status(reflect_id, "completed")

    arc_manager.update_status(save_id, "active")
    _run(pkg.step_handlers.handle_save_reflection, save_id)

    payloads = _enqueued_kb_writes()
    assert payloads == [], (
        "v2 save-reflection must not enqueue any kb.write_entry"
    )
    prov = get_arc_state(save_id, "_agent_response")
    assert prov["proposed_action_count"] == 1
    assert prov["summary"] == "the two arcs shared a flaky-test pattern"

    arc_manager.update_status(dispatch_id, "active")
    _run(pkg.step_handlers.handle_dispatch_actions, dispatch_id)

    resp = get_arc_state(dispatch_id, "_agent_response")
    assert resp["total_proposed"] == 1
    assert resp["action_types"] == ["kb"]
    assert len(resp["spawned_arcs"]) == 1
