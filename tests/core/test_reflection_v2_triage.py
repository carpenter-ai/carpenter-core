"""Tests for the v2 triage-gated reflection pipeline.

Covers the observable-behaviour invariants promised by the v2 design:

- ``handle_reflect_gated`` short-circuits when the sibling triage
  returned ``needs_synthesis=false`` — no LLM call, no downstream KB
  write, no action dispatch.
- Missing / unparseable triage output biases to "false" (skip).
- ``save-reflection`` never enqueues a ``kb.write_entry`` — the v1
  diary write paths have been fully removed.
- The generic ``kb.write_entry`` handler in the coordinator dedupes on
  content hash: when the proposed body matches the existing entry
  exactly, no write is performed.
- ``dispatch-actions`` routes each action via its ``target_path``
  directly (single source of truth for what the action touches).
- ``dispatch-actions`` drops actions whose ``target_path`` matches a
  per-time-period diary shape (``reflections/*``, dated components,
  etc.) and records the drop on ``_dispatch_dropped_diary_targets``.
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
    from carpenter.tool_backends import arc as arc_backend
    calls: list[dict] = []

    def fake_invoke(params):
        calls.append(dict(params))
        arc_id = arc_manager.create_arc(
            name="coding-change-stub", goal=params.get("prompt", ""),
        )
        return {"arc_id": arc_id}

    monkeypatch.setattr(arc_backend, "handle_invoke_coding_change", fake_invoke)
    return calls


@pytest.fixture
def stub_agent(monkeypatch):
    """Stub ``_run_arc_agent`` so tests never hit real LLM/API code.

    Records every invocation so tests can assert reflect was (or was
    not) called for a given batch.
    """
    calls: list[dict] = []

    async def fake_run_arc_agent(arc_id, goal, source_conv_id,
                                 agent_config=None):
        calls.append({
            "arc_id": arc_id,
            "goal": goal[:200] if isinstance(goal, str) else goal,
            "source_conv_id": source_conv_id,
        })

    from carpenter.core.arcs import dispatch_handler
    monkeypatch.setattr(
        dispatch_handler, "_run_arc_agent", fake_run_arc_agent,
    )
    return calls


def _insert_arc(name, *, status="completed", parent_id=None, goal=None,
                integrity_level="trusted"):
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


def _build_tree(refs, date="2026-06-19"):
    tmpl = template_manager.get_template_by_name("reflection")
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


def _run(handler, arc_id):
    arc_info = arc_manager.get_arc(arc_id)
    asyncio.run(handler(arc_id, arc_info))


def _enqueued_kb_writes():
    with db_connection() as db:
        rows = db.execute(
            "SELECT payload_json FROM work_queue WHERE event_type = "
            "'kb.write_entry' ORDER BY id",
        ).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


def _set_triage(triage_id, needs_synthesis, focus_pointers=None, reasons=None):
    payload = json.dumps({
        "needs_synthesis": bool(needs_synthesis),
        "reasons": reasons or [],
        "focus_pointers": focus_pointers or [],
    })
    set_arc_state(triage_id, "_agent_response", payload)
    arc_manager.update_status(triage_id, "active")
    arc_manager.update_status(triage_id, "completed")


# ── the core invariant: triage=false → no LLM, no KB write ─────────


def test_triage_false_skips_reflect_and_downstream_kb_write(
    pkg, stub_agent, stub_invoke,
):
    """When triage says skip, reflect does NOT invoke the agent, save
    does not enqueue a KB write, and dispatch spawns no children."""
    a1 = _insert_arc("goal-1", goal="alpha")
    a2 = _insert_arc("goal-2", goal="beta")
    _, gather_id, triage_id, reflect_id, save_id, dispatch_id = (
        _build_tree([a1, a2]))

    arc_manager.update_status(gather_id, "active")
    _run(pkg.step_handlers.handle_gather_activity, gather_id)

    _set_triage(triage_id, needs_synthesis=False,
                reasons=["routine batch, nothing to learn"])

    arc_manager.update_status(reflect_id, "active")
    _run(pkg.step_handlers.handle_reflect_gated, reflect_id)

    # No LLM was invoked.
    assert stub_agent == [], (
        f"reflect_gated must not call _run_arc_agent when triage says "
        f"skip; got {stub_agent}"
    )
    # The reflect arc wrote an empty ReflectionResult and marked skipped.
    assert get_arc_state(reflect_id, "_reflect_gated_skipped") is True
    raw = get_arc_state(reflect_id, "_agent_response")
    parsed = json.loads(raw)
    assert parsed["proposed_actions"] == []

    # save-reflection: no KB write, arc completes with provenance.
    arc_manager.update_status(save_id, "active")
    _run(pkg.step_handlers.handle_save_reflection, save_id)
    assert _enqueued_kb_writes() == []

    # dispatch-actions: no children spawned.
    arc_manager.update_status(dispatch_id, "active")
    _run(pkg.step_handlers.handle_dispatch_actions, dispatch_id)
    assert stub_invoke == []
    resp = get_arc_state(dispatch_id, "_agent_response")
    assert resp["spawned_arcs"] == []


def test_triage_true_invokes_reflect_agent(pkg, stub_agent):
    """When triage flags synthesis, reflect_gated invokes the standard
    EXECUTOR agent path (which the fixture stubs)."""
    a1 = _insert_arc("goal-1", goal="alpha")
    _, gather_id, triage_id, reflect_id, save_id, dispatch_id = (
        _build_tree([a1]))

    arc_manager.update_status(gather_id, "active")
    _run(pkg.step_handlers.handle_gather_activity, gather_id)

    _set_triage(triage_id, needs_synthesis=True,
                focus_pointers=[f"#{a1}", "skills/foo"])

    arc_manager.update_status(reflect_id, "active")
    _run(pkg.step_handlers.handle_reflect_gated, reflect_id)

    assert len(stub_agent) == 1, (
        f"reflect_gated should invoke _run_arc_agent exactly once when "
        f"triage flags synthesis; got {stub_agent}"
    )
    assert stub_agent[0]["arc_id"] == reflect_id
    # The goal was rendered from the gather-activity sibling output.
    assert "batch" in stub_agent[0]["goal"] or "Reflection" in stub_agent[0]["goal"]
    assert get_arc_state(reflect_id, "_reflect_gated_skipped") is None


def test_triage_missing_output_biases_to_false(pkg, stub_agent):
    """A triage arc that produced no _agent_response is treated as
    'skip synthesis' (bias-toward-false default)."""
    a1 = _insert_arc("goal-1", goal="alpha")
    _, gather_id, triage_id, reflect_id, _, _ = _build_tree([a1])

    arc_manager.update_status(gather_id, "active")
    _run(pkg.step_handlers.handle_gather_activity, gather_id)

    # Do NOT set _agent_response on triage_id — simulates a triage
    # invocation that produced nothing parseable.
    arc_manager.update_status(triage_id, "active")
    arc_manager.update_status(triage_id, "completed")

    arc_manager.update_status(reflect_id, "active")
    _run(pkg.step_handlers.handle_reflect_gated, reflect_id)

    assert stub_agent == []
    assert get_arc_state(reflect_id, "_reflect_gated_skipped") is True


def test_triage_unparseable_json_biases_to_false(pkg, stub_agent):
    """A triage arc whose _agent_response is non-JSON is treated as skip."""
    a1 = _insert_arc("goal-1", goal="alpha")
    _, gather_id, triage_id, reflect_id, _, _ = _build_tree([a1])

    arc_manager.update_status(gather_id, "active")
    _run(pkg.step_handlers.handle_gather_activity, gather_id)

    set_arc_state(triage_id, "_agent_response", "not JSON at all {")
    arc_manager.update_status(triage_id, "active")
    arc_manager.update_status(triage_id, "completed")

    arc_manager.update_status(reflect_id, "active")
    _run(pkg.step_handlers.handle_reflect_gated, reflect_id)

    assert stub_agent == []
    assert get_arc_state(reflect_id, "_reflect_gated_skipped") is True


# ── multi-batch: any flagged batch triggers synthesis for that batch ─


def test_three_batches_one_flagged_only_flagged_batch_invokes_reflect(
    pkg, stub_agent,
):
    """When 3 batches run and only 1 flags synthesis, reflect fires
    exactly once (for the flagged batch). Per-batch gating semantics."""
    trees = []
    for i in range(3):
        a = _insert_arc(f"goal-{i}", goal=f"work {i}")
        trees.append(_build_tree([a], date=f"2026-06-{20 + i:02d}"))

    for _, gather_id, _, _, _, _ in trees:
        arc_manager.update_status(gather_id, "active")
        _run(pkg.step_handlers.handle_gather_activity, gather_id)

    # Batch 0 and 2 → skip; batch 1 → synthesise.
    for idx, (_, _, triage_id, reflect_id, _, _) in enumerate(trees):
        _set_triage(triage_id, needs_synthesis=(idx == 1))
        arc_manager.update_status(reflect_id, "active")
        _run(pkg.step_handlers.handle_reflect_gated, reflect_id)

    assert len(stub_agent) == 1, (
        f"exactly one reflect call expected across three batches; "
        f"got {len(stub_agent)}"
    )
    assert stub_agent[0]["arc_id"] == trees[1][3]  # reflect_id of batch 1


# ── save-reflection: no diary KB writes ever ────────────────────────


def test_save_reflection_never_enqueues_kb_write(pkg):
    """Whether the reflect output has content or not, save-reflection
    must never enqueue a kb.write_entry work item in v2."""
    a1 = _insert_arc("goal-1", goal="alpha")
    _, gather_id, triage_id, reflect_id, save_id, _ = _build_tree([a1])

    arc_manager.update_status(gather_id, "active")
    _run(pkg.step_handlers.handle_gather_activity, gather_id)

    _set_triage(triage_id, needs_synthesis=True)

    # Rich reflect output with a real summary.
    set_arc_state(reflect_id, "_agent_response", json.dumps({
        "summary": "The batch showed a recurring flaky-test pattern.",
        "proposed_actions": [],
    }))
    arc_manager.update_status(reflect_id, "active")
    arc_manager.update_status(reflect_id, "completed")

    arc_manager.update_status(save_id, "active")
    _run(pkg.step_handlers.handle_save_reflection, save_id)

    assert _enqueued_kb_writes() == []


# Content-hash dedupe lives in ``KBStore.write_entry`` (see
# ``tests/kb/test_store.py::test_write_identical_body_is_noop``). The
# coordinator's ``kb.write_entry`` handler is now a thin dispatcher over
# that method, so the dedupe test lives at the store layer where it
# applies to every KB writer.


# ── dispatch-actions routes via each action's target_path ──────────


def test_dispatch_routes_via_action_target_path(pkg, stub_invoke):
    """Each proposed action's target_path drives dispatch directly —
    it is passed as ``affected_paths`` to invoke_coding_change."""
    a1 = _insert_arc("goal-1")
    _, _, _, reflect_id, _, dispatch_id = _build_tree([a1])

    set_arc_state(reflect_id, "_agent_response", json.dumps({
        "summary": "small edit to an existing entry",
        "proposed_actions": [
            {
                "description": "clarify wording of the caching note",
                "target_path": "skills/caching/lru",
                "action_type": "kb",
            },
        ],
    }))
    arc_manager.update_status(reflect_id, "active")
    arc_manager.update_status(reflect_id, "completed")
    arc_manager.update_status(dispatch_id, "active")
    _run(pkg.step_handlers.handle_dispatch_actions, dispatch_id)

    assert len(stub_invoke) == 1
    assert stub_invoke[0]["affected_paths"] == ["skills/caching/lru"]


def test_dispatch_no_target_path_omits_affected_paths(pkg, stub_invoke):
    """An action with no target_path still dispatches, and the coding-
    change invocation gets no ``affected_paths`` (the workflow selector
    then routes generically on the description)."""
    a1 = _insert_arc("goal-1")
    _, _, _, reflect_id, _, dispatch_id = _build_tree([a1])

    set_arc_state(reflect_id, "_agent_response", json.dumps({
        "summary": "generic follow-up with no specific target",
        "proposed_actions": [
            {
                "description": "investigate the flaky-test pattern",
                "target_path": None,
                "action_type": "other",
            },
        ],
    }))
    arc_manager.update_status(reflect_id, "active")
    arc_manager.update_status(reflect_id, "completed")
    arc_manager.update_status(dispatch_id, "active")
    _run(pkg.step_handlers.handle_dispatch_actions, dispatch_id)

    assert len(stub_invoke) == 1
    assert "affected_paths" not in stub_invoke[0]


# ── dispatch-actions drops diary-shaped target_path values ─────────


@pytest.mark.parametrize("diary_path", [
    "reflections/cache-efficiency-baseline",
    "reflections/by-day/2026-06-19",
    "by-day/2026-06-19/summary",
    "by-arc/9304",
    "daily/2026-06-19",
    "weekly/2026-W25",
    "monthly/2026-06",
    "topics/cache/2026-06-19/notes",
    "insights/2026-06-19-cache-hit-rate",
])
def test_dispatch_drops_diary_shaped_target_paths(
    pkg, stub_invoke, diary_path,
):
    """Actions whose target_path looks like a per-time-period diary
    entry are dropped, and the drop is recorded on
    ``_dispatch_dropped_diary_targets`` for provenance."""
    a1 = _insert_arc("goal-1")
    _, _, _, reflect_id, _, dispatch_id = _build_tree([a1])

    set_arc_state(reflect_id, "_agent_response", json.dumps({
        "summary": "LLM proposed a diary write despite the prompt",
        "proposed_actions": [
            {
                "description": "note today's cache hit rate as baseline",
                "target_path": diary_path,
                "action_type": "kb",
            },
            {
                "description": "add cross-ref to canonical caching note",
                "target_path": "skills/caching/lru",
                "action_type": "kb",
            },
        ],
    }))
    arc_manager.update_status(reflect_id, "active")
    arc_manager.update_status(reflect_id, "completed")
    arc_manager.update_status(dispatch_id, "active")
    _run(pkg.step_handlers.handle_dispatch_actions, dispatch_id)

    # Only the durable-topic action reached dispatch.
    assert len(stub_invoke) == 1
    assert stub_invoke[0]["affected_paths"] == ["skills/caching/lru"]

    # The drop was recorded with enough context for post-hoc audit.
    dropped = get_arc_state(dispatch_id, "_dispatch_dropped_diary_targets")
    assert isinstance(dropped, list)
    assert len(dropped) == 1
    assert dropped[0]["target_path"] == diary_path
    assert dropped[0]["action_type"] == "kb"

    # Provenance summary reflects the drop count.
    resp = get_arc_state(dispatch_id, "_agent_response")
    assert resp["dropped_diary_targets"] == 1


def test_dispatch_no_diary_paths_leaves_dropped_state_unset(
    pkg, stub_invoke,
):
    """When no proposed action hits the diary filter,
    ``_dispatch_dropped_diary_targets`` is not written and the
    provenance count is zero."""
    a1 = _insert_arc("goal-1")
    _, _, _, reflect_id, _, dispatch_id = _build_tree([a1])

    set_arc_state(reflect_id, "_agent_response", json.dumps({
        "summary": "clean output",
        "proposed_actions": [
            {
                "description": "tighten wording of the caching note",
                "target_path": "skills/caching/lru",
                "action_type": "kb",
            },
        ],
    }))
    arc_manager.update_status(reflect_id, "active")
    arc_manager.update_status(reflect_id, "completed")
    arc_manager.update_status(dispatch_id, "active")
    _run(pkg.step_handlers.handle_dispatch_actions, dispatch_id)

    assert get_arc_state(
        dispatch_id, "_dispatch_dropped_diary_targets",
    ) is None
    resp = get_arc_state(dispatch_id, "_agent_response")
    assert resp["dropped_diary_targets"] == 0


def test_is_diary_path_predicate(pkg):
    """Unit-level guard against future prefix regressions in
    :func:`_is_diary_path`."""
    from carpenter_template_packages.reflection.step_handlers import (
        _is_diary_path,
    )
    # Positive cases:
    for p in [
        "reflections/cache-baseline",
        "REFLECTIONS/Foo",  # case-insensitive
        "/reflections/leading-slash",
        "by-day/anything",
        "by-arc/9304",
        "daily/whatever",
        "weekly/2026-W25",
        "monthly/2026-06",
        "topics/foo/2026-06-19",
        "insights/2026-06-19-note",
    ]:
        assert _is_diary_path(p), f"expected diary: {p!r}"
    # Negative cases:
    for p in [
        "",
        "skills/caching/lru",
        "topics/foo/durable-idea",
        "runbooks/failover",
        "concepts/associative-memory",
    ]:
        assert not _is_diary_path(p), f"expected non-diary: {p!r}"
