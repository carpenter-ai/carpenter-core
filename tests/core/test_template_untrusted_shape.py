"""Tests for ``untrusted_shape`` support in workflow templates."""

import json

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import template_manager
from carpenter.db import get_db


SHAPE_YAML = """\
name: fetch-workflow
description: Fetch a page and extract data
steps:
  - name: prep
    description: Set up
    order: 0
  - name: fetch_content
    description: Fetch the URL and extract structured data
    untrusted_shape: fetch_web
    order: 10
"""


UNKNOWN_SHAPE_YAML = """\
name: bad-shape
description: References a shape that does not exist
steps:
  - name: fetch
    untrusted_shape: no-such-shape
    order: 0
"""


CONFLICTING_SHAPE_YAML = """\
name: conflicting-shape
description: Mixes untrusted_shape with a conflicting override
steps:
  - name: fetch
    untrusted_shape: fetch_web
    integrity_level: untrusted
    order: 0
"""


# ── load_template validation ───────────────────────────────────────

def test_load_template_accepts_valid_shape(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text(SHAPE_YAML)
    tid = template_manager.load_template(str(path))
    tmpl = template_manager.get_template(tid)
    steps = tmpl["steps"]
    assert any(s.get("untrusted_shape") == "fetch_web" for s in steps)


def test_load_template_rejects_unknown_shape(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text(UNKNOWN_SHAPE_YAML)
    with pytest.raises(ValueError, match="no-such-shape"):
        template_manager.load_template(str(path))


def test_load_template_rejects_conflicting_overrides(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text(CONFLICTING_SHAPE_YAML)
    with pytest.raises(ValueError, match="owned by the shape"):
        template_manager.load_template(str(path))


# ── instantiate_template expansion ─────────────────────────────────

def test_instantiate_expands_shape_into_canonical_batch(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text(SHAPE_YAML)
    tid = template_manager.load_template(str(path))

    parent_id = arc_manager.create_arc("root", "run it", agent_type="PLANNER")
    arc_ids = template_manager.instantiate_template(tid, parent_id)

    # 1 prep step + 3 children from the fetch_web shape = 4 arcs total.
    assert len(arc_ids) == 4

    # The last three are the shape expansion: EXECUTOR-untrusted,
    # REVIEWER, JUDGE.
    executor, reviewer, judge = [arc_manager.get_arc(i) for i in arc_ids[1:]]
    assert executor["integrity_level"] == "untrusted"
    assert executor["agent_type"] == "EXECUTOR"
    assert reviewer["agent_type"] == "REVIEWER"
    assert reviewer["integrity_level"] == "trusted"
    assert judge["agent_type"] == "JUDGE"
    assert judge["integrity_level"] == "trusted"

    # Step-order offset: the step declared order=10 in YAML, so shape
    # children sit at 10, 11, 12.
    shape_orders = sorted(a["step_order"] for a in (executor, reviewer, judge))
    assert shape_orders == [10, 11, 12]

    # template_id / from_template flags propagate to shape children.
    for arc in (executor, reviewer, judge):
        assert arc["template_id"] == tid
        assert arc["from_template"] in (True, 1)

    # Fernet key wiring + _review_target arc_state exist.
    db = get_db()
    try:
        keys = db.execute(
            "SELECT reviewer_arc_id FROM review_keys WHERE target_arc_id = ?",
            (executor["id"],),
        ).fetchall()
        assert {k["reviewer_arc_id"] for k in keys} == {reviewer["id"], judge["id"]}

        targets = db.execute(
            "SELECT arc_id FROM arc_state "
            "WHERE key = '_review_target' "
            "AND arc_id IN (?, ?)",
            (reviewer["id"], judge["id"]),
        ).fetchall()
        assert len(targets) == 2
    finally:
        db.close()


def test_instantiate_shape_step_passes_description_as_goal_binding(tmp_path):
    """The reviewer's goal string should incorporate the step description."""
    path = tmp_path / "t.yaml"
    path.write_text(SHAPE_YAML)
    tid = template_manager.load_template(str(path))

    parent_id = arc_manager.create_arc("root")
    arc_ids = template_manager.instantiate_template(tid, parent_id)

    reviewer = arc_manager.get_arc(arc_ids[2])
    assert "Fetch the URL and extract structured data" in (reviewer["goal"] or "")
