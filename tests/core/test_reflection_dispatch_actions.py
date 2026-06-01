"""Tests for the reflection template's ``dispatch-actions`` step.

Covers the ``handle_dispatch_actions`` handler from
``config_seed/templates/reflection/step_handlers.py``: parsing proposed
actions out of the sibling reflect arc's output, template selection by
action type, fan-out cap, tainted-reflection gating, and no-op handling
for empty proposed-actions payloads.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import shutil

import pytest

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
    """Load templates + trigger the package's register_handlers hook."""
    dest = _copy_seed(tmp_path)
    template_manager.load_templates_from_dir(dest)
    # Ensure the reflection package handlers are registered with our
    # (reset) registry.
    step_handlers = importlib.import_module(
        "carpenter_template_packages.reflection.step_handlers",
    )
    importlib.reload(step_handlers)
    handler_registry.register_step_handler(
        "reflection", "dispatch-actions", step_handlers.handle_dispatch_actions,
    )
    return step_handlers


def _make_reflection_arc_tree(reflect_response: str | None):
    """Build a reflection arc tree via the actual template.

    Creates a parent ``reflection`` arc and instantiates the template so
    children carry the right ``role`` / ``name`` / ``step_order`` for
    sibling-by-role lookup to work the same way it does in prod.
    """
    tmpl = template_manager.get_template_by_name("reflection")
    assert tmpl is not None, "reflection template not loaded"

    root_id = arc_manager.create_arc(
        name="reflection", goal="reflection", template_id=tmpl["id"],
    )
    arc_manager.update_status(root_id, "active")

    child_ids = template_manager.instantiate_template(tmpl["id"], root_id)
    # Map by step name.
    from carpenter.db import db_connection
    with db_connection() as db:
        rows = db.execute(
            "SELECT id, name FROM arcs WHERE parent_id = ? ORDER BY step_order",
            (root_id,),
        ).fetchall()
    by_name = {row["name"]: row["id"] for row in rows}

    reflect_id = by_name["reflect"]
    dispatch_id = by_name["dispatch-actions"]

    # Simulate reflect running to completion.
    arc_manager.update_status(reflect_id, "active")
    if reflect_response is not None:
        set_arc_state(reflect_id, "_agent_response", reflect_response)
    arc_manager.update_status(reflect_id, "completed")

    # Make dispatch-actions active (as dispatch would).
    arc_manager.update_status(by_name["gather-activity"], "active")
    arc_manager.update_status(by_name["gather-activity"], "completed")
    arc_manager.update_status(by_name["save-reflection"], "active")
    arc_manager.update_status(by_name["save-reflection"], "completed")
    arc_manager.update_status(dispatch_id, "active")
    return root_id, reflect_id, dispatch_id


def _run(handler, arc_id):
    arc_info = arc_manager.get_arc(arc_id)
    asyncio.run(handler(arc_id, arc_info))


# ── Unit tests ──────────────────────────────────────────────────────


def test_no_proposed_actions_is_noop(tmp_path):
    """Empty/absent reflect output → no children spawned, no error."""
    step_handlers = _load_package_handlers(tmp_path)

    root_id, reflect_id, dispatch_id = _make_reflection_arc_tree(
        reflect_response=None,
    )

    _run(step_handlers.handle_dispatch_actions, dispatch_id)

    # Dispatch arc completed with an empty spawn list.
    resp = get_arc_state(dispatch_id, "_agent_response")
    assert resp is not None
    assert resp["spawned_arcs"] == []
    assert resp["total_proposed"] == 0

    # Only the 4 template steps; no spawned action children.
    from carpenter.db import db_connection
    with db_connection() as db:
        rows = db.execute(
            "SELECT id FROM arcs WHERE parent_id = ?", (root_id,),
        ).fetchall()
    assert len(rows) == 4  # gather-activity, reflect, save-reflection, dispatch-actions


def test_three_proposed_actions_spawn_three_arcs(tmp_path):
    """Three plain-text actions → three spawned arcs with correct goals."""
    step_handlers = _load_package_handlers(tmp_path)

    reflect_output = (
        "- create kb entry on caching patterns\n"
        "- implement code fix for the widget bug\n"
        "- update knowledge base on deployment\n"
    )
    root_id, _, dispatch_id = _make_reflection_arc_tree(reflect_output)

    _run(step_handlers.handle_dispatch_actions, dispatch_id)

    resp = get_arc_state(dispatch_id, "_agent_response")
    assert len(resp["spawned_arcs"]) == 3
    assert resp["total_proposed"] == 3
    assert resp["truncated"] == 0

    # Verify each spawned arc has the expected action metadata.
    for i, spawned_id in enumerate(resp["spawned_arcs"]):
        assert get_arc_state(spawned_id, "action_type") is not None
        assert get_arc_state(spawned_id, "action_description")


def test_ten_actions_truncated_to_cap(tmp_path, monkeypatch):
    """10 proposed actions with cap=5 → exactly 5 spawned."""
    step_handlers = _load_package_handlers(tmp_path)

    from carpenter import config as _config
    monkeypatch.setitem(
        _config.CONFIG, "reflection", {"max_actions_per_reflection": 5},
    )

    reflect_output = "\n".join(
        f"- kb entry about topic {i}" for i in range(10)
    )
    root_id, _, dispatch_id = _make_reflection_arc_tree(reflect_output)

    _run(step_handlers.handle_dispatch_actions, dispatch_id)

    resp = get_arc_state(dispatch_id, "_agent_response")
    assert len(resp["spawned_arcs"]) == 5
    assert resp["total_proposed"] == 10
    assert resp["truncated"] == 5


def test_action_type_classification_routes_to_correct_template(tmp_path):
    """KB-keyword action → reflection-kb-action; code-keyword → reflection-code-action."""
    step_handlers = _load_package_handlers(tmp_path)

    reflect_output = (
        "- create kb entry on error budgets\n"
        "- implement code fix for the login bug\n"
    )
    root_id, _, dispatch_id = _make_reflection_arc_tree(reflect_output)

    _run(step_handlers.handle_dispatch_actions, dispatch_id)

    resp = get_arc_state(dispatch_id, "_agent_response")
    assert resp["action_types"] == ["kb", "code"]

    kb_arc_id, code_arc_id = resp["spawned_arcs"]

    # The spawned arcs should carry the originating template id; look it
    # up to confirm the routing.
    kb_template = template_manager.get_template_by_name(
        "reflection-kb-action",
    )
    code_template = template_manager.get_template_by_name(
        "reflection-code-action",
    )
    assert kb_template is not None
    assert code_template is not None

    kb_arc = arc_manager.get_arc(kb_arc_id)
    code_arc = arc_manager.get_arc(code_arc_id)
    assert kb_arc["template_id"] == kb_template["id"]
    assert code_arc["template_id"] == code_template["id"]


def test_config_and_other_route_to_kb_template(tmp_path):
    """'config'/'other' action types fall through to the kb template."""
    step_handlers = _load_package_handlers(tmp_path)

    reflect_output = (
        "- enable some setting threshold\n"   # → config
        "- something totally unclassifiable\n"  # → other
    )
    root_id, _, dispatch_id = _make_reflection_arc_tree(reflect_output)

    _run(step_handlers.handle_dispatch_actions, dispatch_id)

    resp = get_arc_state(dispatch_id, "_agent_response")
    assert set(resp["action_types"]) <= {"config", "other"}

    kb_template = template_manager.get_template_by_name(
        "reflection-kb-action",
    )
    for spawned_id in resp["spawned_arcs"]:
        arc = arc_manager.get_arc(spawned_id)
        assert arc["template_id"] == kb_template["id"]


def test_tainted_reflected_arc_marks_spawned_arcs_for_review(tmp_path):
    """If the reflected arc is non-trusted, spawned arcs get _review_mode=human."""
    step_handlers = _load_package_handlers(tmp_path)

    # Create a non-trusted reflected arc whose id we'll thread through
    # the reflection's parent arc_state. Use the unchecked _insert_arc
    # since the public create_arc rejects bare untrusted creation.
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
    for spawned_id in resp["spawned_arcs"]:
        assert get_arc_state(spawned_id, "_review_mode") == "human"


def test_tainted_reflection_spawns_gated_template(tmp_path):
    """Tainted reflection → kb action uses reflection-kb-action-gated;
    code action uses reflection-code-action-gated. Each spawned action arc
    has a blocking ``await-approval`` child with ``arc.manual_trigger``
    activation before the ``execute-action`` child."""
    step_handlers = _load_package_handlers(tmp_path)

    tainted_id = arc_manager._insert_arc(
        name="user-goal-tainted", goal="something tainted",
        integrity_level="untrusted",
    )

    reflect_output = (
        "- create kb entry on error budgets\n"
        "- implement code fix for the login bug\n"
    )
    root_id, _, dispatch_id = _make_reflection_arc_tree(reflect_output)
    set_arc_state(root_id, "reflected_arc_id", tainted_id)

    _run(step_handlers.handle_dispatch_actions, dispatch_id)

    resp = get_arc_state(dispatch_id, "_agent_response")
    assert resp["tainted"] is True
    assert resp["action_types"] == ["kb", "code"]

    kb_gated = template_manager.get_template_by_name(
        "reflection-kb-action-gated",
    )
    code_gated = template_manager.get_template_by_name(
        "reflection-code-action-gated",
    )
    assert kb_gated is not None, "reflection-kb-action-gated not loaded"
    assert code_gated is not None, "reflection-code-action-gated not loaded"

    kb_arc_id, code_arc_id = resp["spawned_arcs"]

    # Both spawned action arcs must point at the gated template variants.
    kb_arc = arc_manager.get_arc(kb_arc_id)
    code_arc = arc_manager.get_arc(code_arc_id)
    assert kb_arc["template_id"] == kb_gated["id"]
    assert code_arc["template_id"] == code_gated["id"]

    # Each spawned action arc has a first-step ``await-approval`` child
    # gated on ``arc.manual_trigger``. The gate step sits at step_order 0
    # so it blocks the ``execute-action`` step at step_order 1.
    from carpenter.db import db_connection
    for action_arc_id in (kb_arc_id, code_arc_id):
        with db_connection() as db:
            steps = db.execute(
                "SELECT id, name, step_order FROM arcs WHERE parent_id = ? "
                "ORDER BY step_order",
                (action_arc_id,),
            ).fetchall()
        names = [s["name"] for s in steps]
        assert names == ["await-approval", "execute-action"]

        gate_arc_id = steps[0]["id"]
        with db_connection() as db:
            rows = db.execute(
                "SELECT event_type FROM arc_activations WHERE arc_id = ?",
                (gate_arc_id,),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["event_type"] == "arc.manual_trigger"

        # The action arc is gated: check_activation on the gate child
        # returns False until an ``arc.manual_trigger`` event is recorded
        # and marked processed.
        assert arc_manager.check_activation(gate_arc_id) is False


def test_clean_reflection_spawns_auto_template(tmp_path):
    """Regression guard: a clean (trusted) reflected arc still routes to
    the single-step auto templates — gated routing must not fire when
    the reflected arc has no taint."""
    step_handlers = _load_package_handlers(tmp_path)

    # Trusted reflected arc (the default integrity_level).
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
    assert resp["action_types"] == ["kb", "code"]

    kb_auto = template_manager.get_template_by_name("reflection-kb-action")
    code_auto = template_manager.get_template_by_name("reflection-code-action")
    assert kb_auto is not None
    assert code_auto is not None

    kb_arc_id, code_arc_id = resp["spawned_arcs"]
    kb_arc = arc_manager.get_arc(kb_arc_id)
    code_arc = arc_manager.get_arc(code_arc_id)
    assert kb_arc["template_id"] == kb_auto["id"]
    assert code_arc["template_id"] == code_auto["id"]

    # No ``_review_mode`` on clean-path spawned arcs; no gate step.
    for action_arc_id in (kb_arc_id, code_arc_id):
        assert get_arc_state(action_arc_id, "_review_mode") is None
        from carpenter.db import db_connection
        with db_connection() as db:
            steps = db.execute(
                "SELECT name FROM arcs WHERE parent_id = ? ORDER BY step_order",
                (action_arc_id,),
            ).fetchall()
        names = [s["name"] for s in steps]
        assert names == ["execute-action"]


def test_manual_trigger_unblocks_gated_action_arc(tmp_path):
    """Once an ``arc.manual_trigger`` event is recorded and processed,
    ``check_activation`` on the gated action arc's gate step returns True
    — confirming the gate pattern is actually wired to the activation
    table in the shape we expect."""
    import json as _json

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
    spawned_id = resp["spawned_arcs"][0]

    from carpenter.db import db_connection, db_transaction
    with db_connection() as db:
        gate = db.execute(
            "SELECT id FROM arcs WHERE parent_id = ? AND name = 'await-approval'",
            (spawned_id,),
        ).fetchone()
    assert gate is not None
    gate_id = gate["id"]

    # Pre-gate: dispatch blocked.
    assert arc_manager.check_activation(gate_id) is False

    # Emit and mark processed — the minimal "operator trips the gate" step.
    with db_transaction() as db:
        db.execute(
            "INSERT INTO events (event_type, payload_json, processed) "
            "VALUES (?, ?, TRUE)",
            ("arc.manual_trigger", _json.dumps({"arc_id": gate_id})),
        )

    # Post-gate: dispatch unblocked.
    assert arc_manager.check_activation(gate_id) is True


# ── Template + handler registration smoke ───────────────────────────


def test_reflection_template_declares_dispatch_step(tmp_path):
    """After loading, the reflection template has a dispatch-actions step
    and the handler is in the registry."""
    _load_package_handlers(tmp_path)

    tmpl = template_manager.get_template_by_name("reflection")
    step_names = [s["name"] for s in tmpl["steps"]]
    assert "dispatch-actions" in step_names

    handler = handler_registry.lookup_step_handler(
        "reflection", "dispatch-actions",
    )
    assert handler is not None


def test_gated_template_variants_are_loadable(tmp_path):
    """Both gated-action templates load from config_seed and declare the
    expected two-step shape with ``arc.manual_trigger`` on the gate."""
    _load_package_handlers(tmp_path)

    for name in ("reflection-kb-action-gated", "reflection-code-action-gated"):
        tmpl = template_manager.get_template_by_name(name)
        assert tmpl is not None, f"template {name!r} not loaded"
        step_names = [s["name"] for s in tmpl["steps"]]
        assert step_names == ["await-approval", "execute-action"]
        gate_step = tmpl["steps"][0]
        assert gate_step.get("activation_event") == "arc.manual_trigger"
