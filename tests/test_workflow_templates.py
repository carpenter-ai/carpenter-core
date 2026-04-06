"""Tests for workflow template loading and instantiation."""

import os
import shutil

from carpenter.core.engine import template_manager
from carpenter.core.arcs import manager as arc_manager
from carpenter.db import get_db


TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "config_seed", "templates")


def _copy_templates(tmp_path):
    """Copy template files to tmp_path for test isolation."""
    dest = str(tmp_path / "templates")
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(TEMPLATES_DIR):
        if f.endswith((".yaml", ".yml")):
            shutil.copy(os.path.join(TEMPLATES_DIR, f), dest)
    return dest


def test_load_writing_repo_template(tmp_path):
    templates_dir = _copy_templates(tmp_path)
    yaml_path = os.path.join(templates_dir, "writing-repo-change.yaml")
    tid = template_manager.load_template(yaml_path)
    assert tid > 0
    template = template_manager.get_template(tid)
    assert template["name"] == "writing-repo-change"
    assert len(template["steps"]) == 6


def test_load_templates_from_dir(tmp_path):
    templates_dir = _copy_templates(tmp_path)
    count = template_manager.load_templates_from_dir(templates_dir)
    assert count == 8
    templates = template_manager.list_templates()
    names = [t["name"] for t in templates]
    assert "writing-repo-change" in names
    assert "coding-change" in names
    assert "dark-factory" in names
    assert "external-coding-change" in names
    assert "pr-review" in names
    assert "reflection" in names


def test_find_template_for_resource(tmp_path):
    templates_dir = _copy_templates(tmp_path)
    template_manager.load_templates_from_dir(templates_dir)

    t = template_manager.find_template_for_resource("repo:writing")
    assert t is not None
    assert t["name"] == "writing-repo-change"

    t = template_manager.find_template_for_resource("nonexistent")
    assert t is None


def test_instantiate_writing_template(tmp_path):
    templates_dir = _copy_templates(tmp_path)
    yaml_path = os.path.join(templates_dir, "writing-repo-change.yaml")
    tid = template_manager.load_template(yaml_path)

    # Create a parent arc
    parent_id = arc_manager.create_arc("write-change", goal="Test change")

    # Instantiate template
    arc_ids = template_manager.instantiate_template(tid, parent_id)
    assert len(arc_ids) == 6

    # Check children
    children = arc_manager.get_children(parent_id)
    assert len(children) == 6
    assert children[0]["name"] == "create-branch"
    assert children[5]["name"] == "merge"
    assert all(c["from_template"] for c in children)


def test_activation_events_registered(tmp_path):
    templates_dir = _copy_templates(tmp_path)
    yaml_path = os.path.join(templates_dir, "writing-repo-change.yaml")
    tid = template_manager.load_template(yaml_path)

    parent_id = arc_manager.create_arc("write-change", goal="Test")
    arc_ids = template_manager.instantiate_template(tid, parent_id)

    # The human-approval step (5th step, index 4) should have arc.manual_trigger
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM arc_activations WHERE arc_id = ?",
            (arc_ids[4],),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["event_type"] == "arc.manual_trigger"
    finally:
        db.close()


def test_template_rigidity(tmp_path):
    templates_dir = _copy_templates(tmp_path)
    yaml_path = os.path.join(templates_dir, "writing-repo-change.yaml")
    tid = template_manager.load_template(yaml_path)

    parent_id = arc_manager.create_arc("write-change", goal="Test")
    # Set template_id on parent so rigidity check works
    db = get_db()
    try:
        db.execute("UPDATE arcs SET template_id = ? WHERE id = ?", (tid, parent_id))
        db.commit()
    finally:
        db.close()

    template_manager.instantiate_template(tid, parent_id)

    # Rigidity should be valid
    assert template_manager.validate_template_rigidity(parent_id) is True


def test_reload_bumps_version(tmp_path):
    templates_dir = _copy_templates(tmp_path)
    yaml_path = os.path.join(templates_dir, "writing-repo-change.yaml")

    tid1 = template_manager.load_template(yaml_path)
    t1 = template_manager.get_template(tid1)
    assert t1["version"] == 1

    # Reload same template
    tid2 = template_manager.load_template(yaml_path)
    t2 = template_manager.get_template(tid2)
    assert tid1 == tid2  # Same ID
    assert t2["version"] == 2
