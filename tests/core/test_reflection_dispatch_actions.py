"""Tests for the reflection template's ``dispatch-actions`` step.

Covers the ``handle_dispatch_actions`` handler from
``config_seed/templates/reflection/step_handlers.py``: parsing proposed
actions out of the sibling reflect arc's typed ``ReflectionResult``
output, the per-reflection action cap, and routing each proposed action
through the platform's standard ``invoke_coding_change`` entry point so
the change-workflow selector picks the right pipeline
(``coding-change`` / ``yaml-change`` / ``kb-change``).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
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
    step_handlers = importlib.import_module(
        "carpenter_template_packages.reflection.step_handlers",
    )
    importlib.reload(step_handlers)
    handler_registry.register_step_handler(
        "reflection", "dispatch-actions", step_handlers.handle_dispatch_actions,
    )
    return step_handlers


class _StubInvoke:
    """Stub for ``handle_invoke_coding_change`` that creates a real arc row.

    Returns the same ``{"arc_id": int}`` shape as the real backend, and
    records each call so tests can assert on the dispatched params.
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


@pytest.fixture
def stub_invoke(monkeypatch):
    """Patch ``handle_invoke_coding_change`` so dispatch tests don't need
    a fully-wired platform_server_dir / workspace_manager setup. The
    invoke entry point is exhaustively covered by
    ``tests/templates/test_workflow_selection.py``.
    """
    from carpenter.tool_backends import arc as arc_backend
    stub = _StubInvoke()
    monkeypatch.setattr(arc_backend, "handle_invoke_coding_change", stub)
    return stub


_BACKTICK_RE = re.compile(r"`([^`]+)`")
_PATH_RE = re.compile(r"^[\w./\-]+\.[\w]+$")


def _line_to_action(line: str) -> dict:
    text = line.strip()
    for prefix in ("- ", "* "):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    target = None
    for m in _BACKTICK_RE.finditer(text):
        tok = m.group(1).strip()
        if _PATH_RE.match(tok):
            target = tok
            break
    return {"description": text, "target_path": target, "action_type": "other"}


def _as_reflection_result_json(legacy: str | None) -> str | None:
    if legacy is None:
        return None
    if not legacy.strip():
        return json.dumps({"summary": "", "proposed_actions": []})
    actions = [
        _line_to_action(line) for line in legacy.strip().split("\n") if line.strip()
    ]
    return json.dumps({
        "summary": "test reflection",
        "proposed_actions": actions,
    })


def _make_reflection_arc_tree(reflect_response: str | None):
    """Build a reflection arc tree via the actual template."""
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
    payload = _as_reflection_result_json(reflect_response)
    if payload is not None:
        set_arc_state(reflect_id, "_agent_response", payload)
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


# ── Unit tests ──────────────────────────────────────────────────────


def test_no_proposed_actions_is_noop(tmp_path, stub_invoke):
    """Empty/absent reflect output → no children spawned, no error."""
    step_handlers = _load_package_handlers(tmp_path)

    root_id, reflect_id, dispatch_id = _make_reflection_arc_tree(
        reflect_response=None,
    )

    _run(step_handlers.handle_dispatch_actions, dispatch_id)

    resp = get_arc_state(dispatch_id, "_agent_response")
    assert resp is not None
    assert resp["spawned_arcs"] == []
    assert resp["total_proposed"] == 0
    assert stub_invoke.calls == []


def test_three_proposed_actions_spawn_three_arcs(tmp_path, stub_invoke):
    """Three plain-text actions → three invoke_coding_change calls."""
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
    assert len(stub_invoke.calls) == 3

    for spawned_id in resp["spawned_arcs"]:
        assert get_arc_state(spawned_id, "action_type") is not None
        assert get_arc_state(spawned_id, "action_description")
        assert get_arc_state(spawned_id, "reflection_parent_arc_id") == root_id
        # Action arcs are children of the dispatch-actions arc, NOT roots,
        # so failure escalation can chain up to the reflection SUPERVISOR.
        spawned = arc_manager.get_arc(spawned_id)
        assert spawned["parent_id"] == dispatch_id

    # And each spawned arc forwarded parent_id in the invoke params.
    for call in stub_invoke.calls:
        assert call["parent_id"] == dispatch_id


def test_ten_actions_truncated_to_cap(tmp_path, monkeypatch, stub_invoke):
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
    assert len(stub_invoke.calls) == 5


def test_target_path_passed_as_affected_paths(tmp_path, stub_invoke):
    """When a proposed action has a target_path, it is forwarded to
    invoke_coding_change as ``affected_paths`` so the standard
    workflow selector routes to the right pipeline."""
    step_handlers = _load_package_handlers(tmp_path)

    reflect_output = "- update the doc at `docs/foo.md`\n"
    root_id, _, dispatch_id = _make_reflection_arc_tree(reflect_output)

    _run(step_handlers.handle_dispatch_actions, dispatch_id)

    assert len(stub_invoke.calls) == 1
    call = stub_invoke.calls[0]
    assert call["source_dir"] == "platform"
    assert call["prompt"] == "update the doc at `docs/foo.md`"
    assert call["affected_paths"] == ["docs/foo.md"]


def test_action_with_no_target_path_omits_affected_paths(tmp_path, stub_invoke):
    """Actions without a target_path do not pass ``affected_paths`` —
    the standard backend then emits a default_pending_classification
    audit and uses coding-change as the fallback."""
    step_handlers = _load_package_handlers(tmp_path)

    reflect_output = "- some vague idea\n"
    root_id, _, dispatch_id = _make_reflection_arc_tree(reflect_output)

    _run(step_handlers.handle_dispatch_actions, dispatch_id)

    assert len(stub_invoke.calls) == 1
    assert "affected_paths" not in stub_invoke.calls[0]


def test_invoke_failure_skips_action_continues_with_others(tmp_path, monkeypatch):
    """A failing invoke_coding_change call skips that action but does
    not abort the dispatch — remaining actions still spawn."""
    step_handlers = _load_package_handlers(tmp_path)

    from carpenter.tool_backends import arc as arc_backend

    call_count = {"n": 0}

    def flaky_invoke(params):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated invoke failure")
        arc_id = arc_manager.create_arc(
            name="ok",
            goal=params.get("prompt", ""),
            parent_id=params.get("parent_id"),
        )
        return {"arc_id": arc_id}

    monkeypatch.setattr(arc_backend, "handle_invoke_coding_change", flaky_invoke)

    reflect_output = (
        "- first action that will fail\n"
        "- second action that will succeed\n"
    )
    root_id, _, dispatch_id = _make_reflection_arc_tree(reflect_output)

    _run(step_handlers.handle_dispatch_actions, dispatch_id)

    resp = get_arc_state(dispatch_id, "_agent_response")
    assert resp["total_proposed"] == 2
    assert len(resp["spawned_arcs"]) == 1


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
