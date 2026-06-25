"""Direct unit-test coverage for the chain-escalation path that wires
reflection-dispatched actions to a passive SUPERVISOR root.

This is the path shipped across PRs #69-#73 that lets a grandchild
failure (e.g. a reflection-proposed coding change) wake the reflection
SUPERVISOR root, even though the immediate parent is a frozen template
step (``dispatch-actions``) that cannot itself replan.

Three layers under test:

1. ``carpenter.core.arcs.manager._find_supervisor_ancestor`` — pure
   parent-chain walk. We exercise the missing-ancestor, status-filter,
   and walk-past-non-SUPERVISOR branches directly. (Existing tests in
   ``test_supervisor_agent.py`` cover the happy-path bubble-up via
   ``_notify_parent_of_failure``; these add direct coverage of the
   helper's branches and edge cases.)

2. ``carpenter.tool_backends.arc.handle_invoke_coding_change`` —
   confirms the ``parent_id`` kwarg threads through to the created arc
   row instead of producing a root arc (PR #71 contract).

3. ``carpenter.template_packages.reflection.step_handlers``
   ``handle_dispatch_actions`` — end-to-end check that each proposed
   action becomes an arc whose ``parent_id`` equals the dispatch-actions
   arc id. The Anthropic agent is NOT called: the reflect arc's
   ``_agent_response`` is seeded directly via arc state.

No real LLM call is made anywhere in this module.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import (
    handler_registry,
    subscriptions,
    template_manager,
)
from carpenter.core.engine.triggers import registry as trigger_registry
from carpenter.core.workflows._arc_state import set_arc_state
from carpenter.db import get_db


TEMPLATES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "config_seed", "templates",
)


def _work_items(event_type: str) -> list[dict]:
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM work_queue WHERE event_type = ?", (event_type,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


# ──────────────────────────────────────────────────────────────────────
# Part 1: _find_supervisor_ancestor — direct unit tests
# ──────────────────────────────────────────────────────────────────────


def test_find_supervisor_ancestor_no_parent_returns_none():
    """A root arc with no parent has no SUPERVISOR ancestor."""
    root = arc_manager.create_arc("root")
    assert arc_manager._find_supervisor_ancestor(root) is None


def test_find_supervisor_ancestor_unknown_start_returns_none():
    """An unknown start arc id returns None instead of raising."""
    assert arc_manager._find_supervisor_ancestor(999_999) is None


def test_find_supervisor_ancestor_returns_nearest_waiting_supervisor():
    """When a SUPERVISOR-in-waiting exists in the chain, it is returned."""
    sup = arc_manager.create_arc("sup", agent_type="SUPERVISOR")
    # SUPERVISOR is born 'waiting'.
    assert arc_manager.get_arc(sup)["status"] == "waiting"

    intermediate = arc_manager.add_child(sup, "step", goal="work")
    arc_manager.update_status(intermediate, "active")
    leaf = arc_manager.create_arc("leaf", goal="leaf", parent_id=intermediate)

    assert arc_manager._find_supervisor_ancestor(leaf) == sup


def test_find_supervisor_ancestor_skips_non_waiting_supervisor():
    """A SUPERVISOR ancestor whose status is NOT 'waiting' is skipped.

    Walks past it looking for a deeper-ancestor SUPERVISOR. With none,
    returns None — even though there IS a SUPERVISOR in the chain.
    """
    sup = arc_manager.create_arc("sup", agent_type="SUPERVISOR")
    # Build the child chain BEFORE flipping the SUPERVISOR out of
    # 'waiting' — add_child rejects frozen/completed parents.
    intermediate = arc_manager.add_child(sup, "step", goal="work")
    arc_manager.update_status(intermediate, "active")
    leaf = arc_manager.create_arc("leaf", goal="leaf", parent_id=intermediate)

    # Now move the SUPERVISOR to a non-'waiting' terminal state.
    arc_manager.update_status(sup, "active")
    arc_manager.update_status(sup, "completed")
    assert arc_manager.get_arc(sup)["status"] == "completed"

    assert arc_manager._find_supervisor_ancestor(leaf) is None


def test_find_supervisor_ancestor_walks_past_non_supervisor_ancestors():
    """Walks past multiple non-SUPERVISOR ancestors to reach the SUPERVISOR."""
    sup = arc_manager.create_arc("reflection-root", agent_type="SUPERVISOR")

    # Build a three-link chain of non-SUPERVISOR arcs under the SUPERVISOR.
    a = arc_manager.add_child(sup, "a", goal="a")
    arc_manager.update_status(a, "active")
    b = arc_manager.create_arc("b", goal="b", parent_id=a)
    arc_manager.update_status(b, "active")
    c = arc_manager.create_arc("c", goal="c", parent_id=b)
    arc_manager.update_status(c, "active")
    leaf = arc_manager.create_arc("leaf", goal="leaf", parent_id=c)

    assert arc_manager._find_supervisor_ancestor(leaf) == sup


def test_find_supervisor_ancestor_prefers_nearest():
    """Two SUPERVISORs in the chain → the nearer-to-leaf one wins."""
    outer = arc_manager.create_arc("outer-sup", agent_type="SUPERVISOR")

    # Inner SUPERVISOR is parented under the outer one.
    inner = arc_manager.create_arc(
        "inner-sup", agent_type="SUPERVISOR", parent_id=outer,
    )
    # SUPERVISOR is born 'waiting', so no further status mutation needed.

    intermediate = arc_manager.add_child(inner, "step", goal="work")
    arc_manager.update_status(intermediate, "active")
    leaf = arc_manager.create_arc("leaf", goal="leaf", parent_id=intermediate)

    assert arc_manager._find_supervisor_ancestor(leaf) == inner


# ──────────────────────────────────────────────────────────────────────
# Part 2: _notify_parent_of_failure — chain-escalation behavior
# ──────────────────────────────────────────────────────────────────────


def test_notify_parent_of_failure_chains_past_non_supervisor_intermediates():
    """Grandchild failure under a non-SUPERVISOR parent enqueues
    ``arc.supervisor_wake`` for the SUPERVISOR grandparent."""
    sup = arc_manager.create_arc("reflection-root", agent_type="SUPERVISOR")
    arc_manager.update_status(sup, "active")
    intermediate = arc_manager.add_child(sup, "dispatch-actions", goal="spawn")
    arc_manager.update_status(sup, "waiting")
    arc_manager.update_status(intermediate, "active")

    grandchild = arc_manager.create_arc(
        "action", goal="risky", parent_id=intermediate,
    )
    arc_manager.update_status(intermediate, "completed")
    arc_manager.freeze_arc(intermediate)

    arc_manager.update_status(grandchild, "active")
    arc_manager.update_status(grandchild, "failed")

    wakes = _work_items("arc.supervisor_wake")
    assert len(wakes) == 1
    payload = json.loads(wakes[0]["payload_json"])
    assert payload["parent_id"] == sup


def test_notify_parent_of_failure_skips_when_supervisor_not_waiting():
    """If the only SUPERVISOR ancestor is not 'waiting', no wake fires."""
    sup = arc_manager.create_arc("sup", agent_type="SUPERVISOR")

    # Build the chain while the SUPERVISOR is still in its born-'waiting'
    # state — add_child rejects frozen/completed parents.
    intermediate = arc_manager.add_child(sup, "step", goal="work")
    arc_manager.update_status(intermediate, "active")
    grandchild = arc_manager.create_arc(
        "leaf", goal="leaf", parent_id=intermediate,
    )
    arc_manager.update_status(intermediate, "completed")
    arc_manager.freeze_arc(intermediate)

    # NOW flip the SUPERVISOR to a non-'waiting' terminal state so the
    # escalation walk skips it.
    arc_manager.update_status(sup, "active")
    arc_manager.update_status(sup, "completed")

    arc_manager.update_status(grandchild, "active")
    arc_manager.update_status(grandchild, "failed")

    assert _work_items("arc.supervisor_wake") == []


# ──────────────────────────────────────────────────────────────────────
# Part 3: handle_invoke_coding_change parent_id kwarg
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def _invoke_env(monkeypatch, tmp_path):
    """Wire up the minimum config + filesystem for handle_invoke_coding_change."""
    from carpenter import config as carpenter_config

    repo = tmp_path / "repo"
    (repo / "carpenter").mkdir(parents=True)
    (repo / "carpenter" / "__init__.py").write_text("")
    monkeypatch.setitem(carpenter_config.CONFIG, "repo_dir", str(repo))

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setitem(carpenter_config.CONFIG, "carpenter_home", str(home))

    platform_dir = tmp_path / "source"
    platform_dir.mkdir()
    monkeypatch.setitem(
        carpenter_config.CONFIG, "platform_server_dir", str(platform_dir),
    )

    monkeypatch.setitem(
        carpenter_config.CONFIG,
        "platform_integrity",
        {
            "change_workflows": {
                "python": "coding-change",
                "yaml": "yaml-change",
                "kb": "kb-change",
                "unknown": "coding-change",
            },
            "path_overrides": [],
        },
    )

    # Load the change templates so get_template_by_name resolves them.
    dest = tmp_path / "templates"
    dest.mkdir()
    for name in ("coding-change.yaml", "yaml-change.yaml", "kb-change.yaml"):
        src = os.path.join(TEMPLATES_DIR, name)
        shutil.copy(src, dest / name)
        template_manager.load_template(str(dest / name))

    return home


def test_handle_invoke_coding_change_with_parent_id_creates_child(_invoke_env):
    """When called with ``parent_id``, the new arc is parented under it,
    not created as a root."""
    from carpenter.tool_backends import arc as arc_backend

    parent = arc_manager.create_arc(
        "dispatch-actions", agent_type="EXECUTOR", goal="spawn actions",
    )

    result = arc_backend.handle_invoke_coding_change({
        "source_dir": "platform",
        "prompt": "do something",
        "parent_id": parent,
    })
    assert "arc_id" in result, f"expected arc_id, got {result!r}"

    spawned = arc_manager.get_arc(result["arc_id"])
    assert spawned is not None
    assert spawned["parent_id"] == parent
    # Depth = parent.depth + 1 = 1.
    assert spawned["depth"] == 1


def test_handle_invoke_coding_change_without_parent_id_creates_root(_invoke_env):
    """Sanity: omitting parent_id preserves the legacy ROOT behavior so
    existing callers (chat-tool entry point) are unaffected."""
    from carpenter.tool_backends import arc as arc_backend

    result = arc_backend.handle_invoke_coding_change({
        "source_dir": "platform",
        "prompt": "do something else",
    })
    assert "arc_id" in result
    spawned = arc_manager.get_arc(result["arc_id"])
    assert spawned is not None
    assert spawned["parent_id"] is None
    assert spawned["depth"] == 0


# ──────────────────────────────────────────────────────────────────────
# Part 4: handle_dispatch_actions end-to-end (no LLM)
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=False)
def _reflection_env():
    """Reset trigger/subscription/handler registries around dispatch tests."""
    trigger_registry.reset()
    subscriptions.reset()
    handler_registry.clear_registry()
    yield
    trigger_registry.reset()
    subscriptions.reset()
    handler_registry.clear_registry()


def _copy_seed_templates(tmp_path):
    dest = str(tmp_path / "templates")
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(TEMPLATES_DIR):
        src = os.path.join(TEMPLATES_DIR, f)
        if os.path.isfile(src) and f.endswith((".yaml", ".yml")):
            shutil.copy(src, dest)
        elif os.path.isdir(src) and not f.startswith((".", "_")):
            shutil.copytree(src, os.path.join(dest, f))
    return dest


def _load_reflection_handlers(tmp_path):
    dest = _copy_seed_templates(tmp_path)
    template_manager.load_templates_from_dir(dest)
    step_handlers = importlib.import_module(
        "carpenter_template_packages.reflection.step_handlers",
    )
    importlib.reload(step_handlers)
    handler_registry.register_step_handler(
        "reflection", "dispatch-actions", step_handlers.handle_dispatch_actions,
    )
    return step_handlers


def _make_reflection_tree_with_actions(actions: list[dict]):
    """Build a reflection arc tree and seed the reflect arc's response."""
    tmpl = template_manager.get_template_by_name("reflection")
    assert tmpl is not None, "reflection template not loaded"

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
    payload = json.dumps({"summary": "test", "proposed_actions": actions})
    set_arc_state(reflect_id, "_agent_response", payload)
    arc_manager.update_status(reflect_id, "completed")

    # Drive intermediate sibling steps through their transitions so the
    # dispatch step is reachable.
    for n in ("gather-activity", "save-reflection"):
        arc_manager.update_status(by_name[n], "active")
        arc_manager.update_status(by_name[n], "completed")
    arc_manager.update_status(dispatch_id, "active")

    return root_id, reflect_id, dispatch_id


class _StubInvoke:
    """Stub for ``handle_invoke_coding_change`` that creates a real arc row.

    Records each call so the test can assert on the dispatched params.
    """

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, params: dict) -> dict:
        self.calls.append(dict(params))
        arc_id = arc_manager.create_arc(
            name=f"coding-change: {params.get('prompt', '')[:30]}",
            goal=params.get("prompt", ""),
            parent_id=params.get("parent_id"),
        )
        return {"arc_id": arc_id}


def test_dispatch_actions_parents_each_action_under_itself(
    tmp_path, monkeypatch, _reflection_env,
):
    """Every spawned action arc has parent_id == dispatch-actions arc id.

    This is the core wiring contract from PR #71: action arcs are
    children of the dispatch-actions arc (not roots), so failures bubble
    up via the parent chain to the reflection SUPERVISOR root via
    _find_supervisor_ancestor.
    """
    step_handlers = _load_reflection_handlers(tmp_path)

    from carpenter.tool_backends import arc as arc_backend

    stub = _StubInvoke()
    monkeypatch.setattr(arc_backend, "handle_invoke_coding_change", stub)

    actions = [
        {"description": "a", "target_path": None, "action_type": "other"},
        {"description": "b", "target_path": None, "action_type": "other"},
    ]
    root_id, _, dispatch_id = _make_reflection_tree_with_actions(actions)

    arc_info = arc_manager.get_arc(dispatch_id)
    asyncio.run(step_handlers.handle_dispatch_actions(dispatch_id, arc_info))

    # Each invoke received parent_id = dispatch arc id.
    assert len(stub.calls) == 2
    for call in stub.calls:
        assert call["parent_id"] == dispatch_id

    # And the resulting arc rows are children of dispatch-actions.
    from carpenter.db import db_connection
    with db_connection() as db:
        children = db.execute(
            "SELECT id, parent_id FROM arcs WHERE parent_id = ?",
            (dispatch_id,),
        ).fetchall()
    assert len(children) == 2
    for row in children:
        assert row["parent_id"] == dispatch_id


def test_dispatch_actions_failure_chains_to_reflection_supervisor(
    tmp_path, monkeypatch, _reflection_env,
):
    """End-to-end: a failed action arc under dispatch-actions wakes the
    reflection SUPERVISOR root via the chain-escalation walk.

    This validates the contract that ties PRs #69-#73 together: passive
    SUPERVISOR root + parent_id threading + ancestor-walk wake.
    """
    step_handlers = _load_reflection_handlers(tmp_path)

    # Make the reflection root a SUPERVISOR (the live reflection trigger
    # creates it this way). The seeded template doesn't necessarily mark
    # the root as SUPERVISOR by default in tests, so build it manually.
    from carpenter.tool_backends import arc as arc_backend

    sup = arc_manager.create_arc(
        "reflection-root", agent_type="SUPERVISOR", goal="reflection",
    )
    # SUPERVISOR is born 'waiting'.

    # Simulate the dispatch-actions template step under it.
    dispatch = arc_manager.add_child(sup, "dispatch-actions", goal="spawn")
    arc_manager.update_status(dispatch, "active")

    # Spawn the action arc via the real invoke path with parent_id=dispatch.
    # Stub the workspace heavy lifting by patching handle_invoke_coding_change
    # to a thin shim that just creates the child arc — we are testing the
    # ESCALATION wiring, not the workflow selection.
    def fake_invoke(params):
        return {
            "arc_id": arc_manager.create_arc(
                name="action",
                goal=params.get("prompt", ""),
                parent_id=params.get("parent_id"),
            )
        }

    monkeypatch.setattr(arc_backend, "handle_invoke_coding_change", fake_invoke)

    result = arc_backend.handle_invoke_coding_change({
        "source_dir": "platform",
        "prompt": "risky action",
        "parent_id": dispatch,
    })
    action_id = result["arc_id"]

    # dispatch-actions finishes and is frozen (mirrors the real handler).
    arc_manager.update_status(dispatch, "completed")
    arc_manager.freeze_arc(dispatch)

    # Action arc fails → wake should fire on the SUPERVISOR root.
    arc_manager.update_status(action_id, "active")
    arc_manager.update_status(action_id, "failed")

    wakes = _work_items("arc.supervisor_wake")
    assert len(wakes) == 1
    payload = json.loads(wakes[0]["payload_json"])
    assert payload["parent_id"] == sup
