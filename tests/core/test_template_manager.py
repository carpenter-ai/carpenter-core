"""Tests for carpenter.core.template_manager."""

import json

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import template_manager
from carpenter.db import get_db


SAMPLE_YAML = """\
name: test-workflow
description: A test workflow
required_for:
  - "resource:test"
steps:
  - name: step-1
    description: First step
    order: 1
  - name: step-2
    description: Second step
    order: 2
  - name: step-3
    description: Third step
    order: 3
    activation_event: webhook.received
"""

SAMPLE_YAML_2 = """\
name: other-workflow
description: Another workflow
required_for:
  - "resource:other"
steps:
  - name: alpha
    description: Alpha step
    order: 1
  - name: beta
    description: Beta step
    order: 2
"""


# ── load_template ──────────────────────────────────────────────────

def test_load_template(tmp_path):
    """load_template reads a YAML file and stores it in the database."""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(SAMPLE_YAML)

    template_id = template_manager.load_template(str(yaml_file))
    assert isinstance(template_id, int)
    assert template_id > 0

    tmpl = template_manager.get_template(template_id)
    assert tmpl["name"] == "test-workflow"
    assert tmpl["description"] == "A test workflow"
    assert tmpl["version"] == 1
    assert len(tmpl["steps"]) == 3
    assert tmpl["steps"][0]["name"] == "step-1"


def test_load_template_update_increments_version(tmp_path):
    """Re-loading a template with the same name increments the version."""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(SAMPLE_YAML)

    tid1 = template_manager.load_template(str(yaml_file))
    tmpl1 = template_manager.get_template(tid1)
    assert tmpl1["version"] == 1

    tid2 = template_manager.load_template(str(yaml_file))
    assert tid2 == tid1  # Same ID, updated in place

    tmpl2 = template_manager.get_template(tid2)
    assert tmpl2["version"] == 2


def test_load_template_stores_required_for(tmp_path):
    """load_template stores required_for_json correctly."""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(SAMPLE_YAML)

    tid = template_manager.load_template(str(yaml_file))
    tmpl = template_manager.get_template(tid)
    assert tmpl["required_for"] == ["resource:test"]


# ── get_template, get_template_by_name ─────────────────────────────

def test_get_template_not_found():
    """get_template returns None for nonexistent ID."""
    assert template_manager.get_template(99999) is None


def test_get_template_by_name(tmp_path):
    """get_template_by_name returns the template with parsed steps."""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(SAMPLE_YAML)
    template_manager.load_template(str(yaml_file))

    tmpl = template_manager.get_template_by_name("test-workflow")
    assert tmpl is not None
    assert tmpl["name"] == "test-workflow"
    assert len(tmpl["steps"]) == 3


def test_get_template_by_name_not_found():
    """get_template_by_name returns None for nonexistent name."""
    assert template_manager.get_template_by_name("no-such-template") is None


# ── list_templates ─────────────────────────────────────────────────

def test_list_templates(tmp_path):
    """list_templates returns all stored templates."""
    f1 = tmp_path / "test1.yaml"
    f1.write_text(SAMPLE_YAML)
    f2 = tmp_path / "test2.yaml"
    f2.write_text(SAMPLE_YAML_2)

    template_manager.load_template(str(f1))
    template_manager.load_template(str(f2))

    templates = template_manager.list_templates()
    assert len(templates) == 2
    names = {t["name"] for t in templates}
    assert names == {"test-workflow", "other-workflow"}


# ── find_template_for_resource ─────────────────────────────────────

def test_find_template_for_resource_match(tmp_path):
    """find_template_for_resource returns the matching template."""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(SAMPLE_YAML)
    template_manager.load_template(str(yaml_file))

    tmpl = template_manager.find_template_for_resource("resource:test")
    assert tmpl is not None
    assert tmpl["name"] == "test-workflow"


def test_find_template_for_resource_no_match(tmp_path):
    """find_template_for_resource returns None when no template matches."""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(SAMPLE_YAML)
    template_manager.load_template(str(yaml_file))

    result = template_manager.find_template_for_resource("resource:nonexistent")
    assert result is None


# ── instantiate_template ───────────────────────────────────────────

def test_instantiate_template_creates_child_arcs(tmp_path):
    """instantiate_template creates one child arc per step."""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(SAMPLE_YAML)
    tid = template_manager.load_template(str(yaml_file))

    parent_id = arc_manager.create_arc("parent-arc", template_id=tid)
    arc_ids = template_manager.instantiate_template(tid, parent_id)

    assert len(arc_ids) == 3

    children = arc_manager.get_children(parent_id)
    assert len(children) == 3

    for child in children:
        assert child["from_template"] == 1  # SQLite stores booleans as integers
        assert child["template_id"] == tid

    # Check step_orders match template step orders
    orders = [c["step_order"] for c in children]
    assert orders == [1, 2, 3]

    # Check names
    names = [c["name"] for c in children]
    assert names == ["step-1", "step-2", "step-3"]


def test_instantiate_template_registers_activation_events(tmp_path):
    """instantiate_template registers activation_event in arc_activations."""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(SAMPLE_YAML)
    tid = template_manager.load_template(str(yaml_file))

    parent_id = arc_manager.create_arc("parent-arc", template_id=tid)
    arc_ids = template_manager.instantiate_template(tid, parent_id)

    # step-3 has activation_event: webhook.received
    step3_arc_id = arc_ids[2]

    db = get_db()
    try:
        activations = db.execute(
            "SELECT * FROM arc_activations WHERE arc_id = ?",
            (step3_arc_id,),
        ).fetchall()
        assert len(activations) == 1
        assert activations[0]["event_type"] == "webhook.received"
    finally:
        db.close()

    # step-1 and step-2 should have no activations
    db = get_db()
    try:
        for arc_id in arc_ids[:2]:
            acts = db.execute(
                "SELECT * FROM arc_activations WHERE arc_id = ?",
                (arc_id,),
            ).fetchall()
            assert len(acts) == 0
    finally:
        db.close()


# ── validate_template_rigidity ─────────────────────────────────────

def test_validate_template_rigidity_valid(tmp_path):
    """validate_template_rigidity returns True when arcs are intact."""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(SAMPLE_YAML)
    tid = template_manager.load_template(str(yaml_file))

    parent_id = arc_manager.create_arc("parent-arc", template_id=tid)
    template_manager.instantiate_template(tid, parent_id)

    assert template_manager.validate_template_rigidity(parent_id) is True


def test_validate_template_rigidity_arc_deleted(tmp_path):
    """validate_template_rigidity returns False when a template arc is deleted."""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(SAMPLE_YAML)
    tid = template_manager.load_template(str(yaml_file))

    parent_id = arc_manager.create_arc("parent-arc", template_id=tid)
    arc_ids = template_manager.instantiate_template(tid, parent_id)

    # Delete one of the template arcs
    db = get_db()
    try:
        db.execute("DELETE FROM arcs WHERE id = ?", (arc_ids[1],))
        db.commit()
    finally:
        db.close()

    assert template_manager.validate_template_rigidity(parent_id) is False


def test_validate_template_rigidity_no_template():
    """validate_template_rigidity returns True for arc without template_id."""
    parent_id = arc_manager.create_arc("no-template-arc")
    assert template_manager.validate_template_rigidity(parent_id) is True


# ── template immutability ──────────────────────────────────────────

def test_cannot_add_child_to_template_created_arc(tmp_path):
    """add_child raises ValueError for template-created arcs."""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(SAMPLE_YAML)
    tid = template_manager.load_template(str(yaml_file))

    parent_id = arc_manager.create_arc("parent-arc", template_id=tid)
    arc_ids = template_manager.instantiate_template(tid, parent_id)

    # Try to add a child to a template-created arc (step-1)
    with pytest.raises(ValueError, match="Cannot add child to arc .* created by template"):
        arc_manager.add_child(arc_ids[0], "new-child", goal="This should fail")


def test_can_add_child_to_non_template_arc():
    """add_child works normally for non-template arcs."""
    parent_id = arc_manager.create_arc("regular-arc")
    child_id = arc_manager.add_child(parent_id, "child-arc", goal="This is allowed")
    assert child_id > 0

    children = arc_manager.get_children(parent_id)
    assert len(children) == 1
    assert children[0]["name"] == "child-arc"


# ── load_templates_from_dir ────────────────────────────────────────

def test_load_templates_from_dir(tmp_path):
    """load_templates_from_dir loads all .yaml and .yml files."""
    f1 = tmp_path / "workflow1.yaml"
    f1.write_text(SAMPLE_YAML)
    f2 = tmp_path / "workflow2.yml"
    f2.write_text(SAMPLE_YAML_2)

    # Create a non-YAML file that should be ignored
    f3 = tmp_path / "readme.txt"
    f3.write_text("not a template")

    count = template_manager.load_templates_from_dir(str(tmp_path))
    assert count == 2

    templates = template_manager.list_templates()
    assert len(templates) == 2
    names = {t["name"] for t in templates}
    assert names == {"test-workflow", "other-workflow"}
