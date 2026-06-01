"""Tests for carpenter.core.engine.arc_outputs."""

import json

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import arc_outputs, template_manager
from carpenter.db import get_db


SAMPLE_YAML = """\
name: role-test-workflow
description: Workflow with roles on steps
steps:
  - name: gather
    description: Gather inputs
    order: 0
    role: analyze
  - name: save
    description: Persist output
    order: 1
    role: persist
  - name: notify
    description: Send notification
    order: 2
    # intentionally no role — should fall back to name
"""


# ── set/get/list arc outputs ───────────────────────────────────────


def test_set_and_get_arc_output():
    arc_id = arc_manager.create_arc("a")
    arc_outputs.set_arc_output(arc_id, "result", {"score": 7, "ok": True})
    assert arc_outputs.get_arc_output(arc_id, "result") == {"score": 7, "ok": True}


def test_get_arc_output_missing_returns_default():
    arc_id = arc_manager.create_arc("a")
    assert arc_outputs.get_arc_output(arc_id, "missing") is None
    assert arc_outputs.get_arc_output(arc_id, "missing", default="x") == "x"


def test_set_arc_output_overwrites():
    arc_id = arc_manager.create_arc("a")
    arc_outputs.set_arc_output(arc_id, "k", 1)
    arc_outputs.set_arc_output(arc_id, "k", 2)
    assert arc_outputs.get_arc_output(arc_id, "k") == 2


def test_list_arc_outputs():
    arc_id = arc_manager.create_arc("a")
    arc_outputs.set_arc_output(arc_id, "x", "a")
    arc_outputs.set_arc_output(arc_id, "y", [1, 2])
    assert arc_outputs.list_arc_outputs(arc_id) == {"x": "a", "y": [1, 2]}


def test_outputs_namespaced_from_other_arc_state():
    """Outputs do not collide with other workflow keys at the same name."""
    arc_id = arc_manager.create_arc("a")
    from carpenter.core.workflows._arc_state import set_arc_state, get_arc_state
    set_arc_state(arc_id, "result", "workflow-private-value")
    arc_outputs.set_arc_output(arc_id, "result", "public-output-value")
    # The two live in different keys, not clobbering.
    assert get_arc_state(arc_id, "result") == "workflow-private-value"
    assert arc_outputs.get_arc_output(arc_id, "result") == "public-output-value"


def test_set_arc_output_rejects_bad_name():
    arc_id = arc_manager.create_arc("a")
    with pytest.raises(ValueError):
        arc_outputs.set_arc_output(arc_id, "", "v")
    with pytest.raises(ValueError):
        arc_outputs.set_arc_output(arc_id, "has:colon", "v")


def test_set_arc_output_rejects_unserialisable():
    arc_id = arc_manager.create_arc("a")
    with pytest.raises(TypeError):
        arc_outputs.set_arc_output(arc_id, "k", object())


# ── find_sibling_arc_id ────────────────────────────────────────────


def test_find_sibling_returns_none_for_root_arc():
    arc_id = arc_manager.create_arc("root")
    assert arc_outputs.find_sibling_arc_id(arc_id, "anything") is None


def test_find_sibling_returns_none_when_no_match():
    parent = arc_manager.create_arc("parent")
    me = arc_manager.create_arc("child-a", parent_id=parent)
    arc_manager.create_arc("child-b", parent_id=parent)
    assert arc_outputs.find_sibling_arc_id(me, "no-such-role") is None


def test_find_sibling_by_arc_name_fallback():
    """When there is no template, sibling_role is matched against arcs.name."""
    parent = arc_manager.create_arc("parent")
    me = arc_manager.create_arc("save", parent_id=parent)
    analyst = arc_manager.create_arc("analyze", parent_id=parent)
    assert arc_outputs.find_sibling_arc_id(me, "analyze") == analyst
    assert arc_outputs.find_sibling_arc_id(me, "save") is None  # self is excluded


def test_find_sibling_by_template_role(tmp_path):
    """A sibling whose template step has role=X is matched by X."""
    yaml_file = tmp_path / "wf.yaml"
    yaml_file.write_text(SAMPLE_YAML)
    template_id = template_manager.load_template(str(yaml_file))

    parent = arc_manager.create_arc("root")
    gather = arc_manager.create_arc(
        "gather", parent_id=parent,
        template_id=template_id, from_template=True, step_order=0,
    )
    save = arc_manager.create_arc(
        "save", parent_id=parent,
        template_id=template_id, from_template=True, step_order=1,
    )
    # From 'save', resolve 'analyze' (the role of 'gather').
    assert arc_outputs.find_sibling_arc_id(save, "analyze") == gather
    # And role 'persist' resolves to self — excluded, so None.
    assert arc_outputs.find_sibling_arc_id(save, "persist") is None


def test_find_sibling_falls_back_to_name_when_step_has_no_role(tmp_path):
    """A template step without ``role`` declared is addressable by its name."""
    yaml_file = tmp_path / "wf.yaml"
    yaml_file.write_text(SAMPLE_YAML)
    template_id = template_manager.load_template(str(yaml_file))

    parent = arc_manager.create_arc("root")
    notify = arc_manager.create_arc(
        "notify", parent_id=parent,
        template_id=template_id, from_template=True, step_order=2,
    )
    save = arc_manager.create_arc(
        "save", parent_id=parent,
        template_id=template_id, from_template=True, step_order=1,
    )
    # 'notify' has no declared role → its name serves as the role.
    assert arc_outputs.find_sibling_arc_id(save, "notify") == notify


def test_find_sibling_uses_arcs_step_role_column():
    """When a sibling arc has step_role set directly (post-D2 PR-α arcs),
    find_sibling_arc_id matches it without needing a template lookup."""
    parent = arc_manager.create_arc("root")
    # No template_id at all — only the column matters.
    gather = arc_manager.create_arc(
        "step-a", parent_id=parent, step_role="prepare", step_order=0,
    )
    save = arc_manager.create_arc(
        "step-b", parent_id=parent, step_role="persist", step_order=1,
    )
    # From save, find the prepare sibling by role.
    assert arc_outputs.find_sibling_arc_id(save, "prepare") == gather
    # And the column-based hit beats name-fallback even when names also
    # incidentally collide:
    other = arc_manager.create_arc(
        "prepare", parent_id=parent, step_order=2,
    )
    # The arc with step_role="prepare" still wins (lowest step_order).
    assert arc_outputs.find_sibling_arc_id(save, "prepare") == gather


def test_find_sibling_empty_role_returns_none():
    parent = arc_manager.create_arc("parent")
    me = arc_manager.create_arc("child", parent_id=parent)
    arc_manager.create_arc("other", parent_id=parent)
    assert arc_outputs.find_sibling_arc_id(me, "") is None


# ── get_sibling_output ─────────────────────────────────────────────


def test_get_sibling_output_happy_path(tmp_path):
    yaml_file = tmp_path / "wf.yaml"
    yaml_file.write_text(SAMPLE_YAML)
    template_id = template_manager.load_template(str(yaml_file))

    parent = arc_manager.create_arc("root")
    gather = arc_manager.create_arc(
        "gather", parent_id=parent,
        template_id=template_id, from_template=True, step_order=0,
    )
    save = arc_manager.create_arc(
        "save", parent_id=parent,
        template_id=template_id, from_template=True, step_order=1,
    )
    arc_outputs.set_arc_output(gather, "reflection_text", "hello world")

    assert arc_outputs.get_sibling_output(save, "analyze", "reflection_text") == "hello world"


def test_get_sibling_output_missing_sibling_returns_default():
    parent = arc_manager.create_arc("parent")
    me = arc_manager.create_arc("child", parent_id=parent)
    assert arc_outputs.get_sibling_output(me, "nope", "any") is None
    assert arc_outputs.get_sibling_output(me, "nope", "any", default="d") == "d"


def test_get_sibling_output_missing_output_returns_default():
    parent = arc_manager.create_arc("parent")
    other = arc_manager.create_arc("other", parent_id=parent)
    me = arc_manager.create_arc("me", parent_id=parent)
    # Sibling exists but has no output set.
    assert arc_outputs.get_sibling_output(me, "other", "any") is None
    assert arc_outputs.get_sibling_output(me, "other", "any", default="d") == "d"


def test_module_reexport():
    from carpenter.core import engine
    assert engine.set_arc_output is arc_outputs.set_arc_output
    assert engine.get_sibling_output is arc_outputs.get_sibling_output
