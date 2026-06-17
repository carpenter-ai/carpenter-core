"""Arc provenance (origin_kind / origin_ref) + inheritance."""

import json

from carpenter.core.arcs import manager as arc_manager
from carpenter.tool_backends import arc as arc_backend
from carpenter.db import get_db


def _origin(arc_id: int) -> tuple[str | None, str | None]:
    db = get_db()
    try:
        row = db.execute(
            "SELECT origin_kind, origin_ref FROM arcs WHERE id = ?", (arc_id,)
        ).fetchone()
    finally:
        db.close()
    return row["origin_kind"], row["origin_ref"]


def test_create_arc_stamps_origin():
    arc_id = arc_manager.create_arc(
        "root", agent_type="PLANNER",
        origin_kind="trigger", origin_ref=json.dumps({"subscription": "x"}),
    )
    kind, ref = _origin(arc_id)
    assert kind == "trigger"
    assert json.loads(ref)["subscription"] == "x"


def test_origin_defaults_null_for_unstamped_root():
    arc_id = arc_manager.create_arc("plain-root", agent_type="PLANNER")
    kind, ref = _origin(arc_id)
    assert kind is None
    assert ref is None


def test_child_inherits_origin_from_parent():
    parent = arc_manager.create_arc(
        "p", agent_type="PLANNER",
        origin_kind="trigger", origin_ref=json.dumps({"subscription": "s"}),
    )
    child = arc_manager.create_arc("c", goal="g", parent_id=parent)
    kind, ref = _origin(child)
    assert kind == "trigger"
    assert json.loads(ref)["subscription"] == "s"


def test_child_explicit_origin_overrides_inheritance():
    parent = arc_manager.create_arc(
        "p", agent_type="PLANNER", origin_kind="trigger",
    )
    child = arc_manager.create_arc(
        "c", goal="g", parent_id=parent, origin_kind="arc",
        origin_ref=json.dumps({"spawned_by_arc_id": parent}),
    )
    kind, ref = _origin(child)
    assert kind == "arc"
    assert json.loads(ref)["spawned_by_arc_id"] == parent


def test_batch_children_inherit_root_origin():
    """The triage/index pattern: PLANNER root + untrusted EXECUTOR +
    REVIEWER + JUDGE created via handle_create_batch all carry the root's
    origin, so a trigger-spawned tree is fully attributable."""
    parent = arc_manager.create_arc(
        "triage-root", agent_type="PLANNER",
        origin_kind="trigger", origin_ref=json.dumps({"subscription": "email"}),
    )
    arc_manager.update_status(parent, "active")
    result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": "exec", "goal": "g", "parent_id": parent,
                "integrity_level": "untrusted", "output_type": "json",
                "agent_type": "EXECUTOR", "step_order": 0,
            },
            {
                "name": "rev", "goal": "g", "parent_id": parent,
                "agent_type": "REVIEWER", "integrity_level": "trusted",
                "reviewer_profile": "security-reviewer", "step_order": 1,
            },
            {
                "name": "judge", "goal": "g", "parent_id": parent,
                "agent_type": "JUDGE", "integrity_level": "trusted",
                "reviewer_profile": "judge", "step_order": 2,
            },
        ],
    })
    assert "error" not in result, result
    for cid in result["arc_ids"]:
        kind, ref = _origin(cid)
        assert kind == "trigger"
        assert json.loads(ref)["subscription"] == "email"


def test_chat_arc_create_stamps_chat_origin():
    result = arc_backend.handle_create({
        "name": "chat-root", "goal": "g", "agent_type": "PLANNER",
    })
    kind, _ref = _origin(result["arc_id"])
    assert kind == "chat"
