"""Tests for the untrusted-shape registry + shared batch helper."""

import json

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.trust import untrusted_shapes
from carpenter.core.trust.batch import create_untrusted_batch
from carpenter.db import get_db


# ── preset registry ────────────────────────────────────────────────

def test_fetch_web_shape_resolves():
    shape = untrusted_shapes.get_shape("fetch_web")
    assert "specs" in shape
    assert len(shape["specs"]) == 3

    # Canonical ordering: EXECUTOR-untrusted, REVIEWER, JUDGE.
    assert shape["specs"][0]["integrity_level"] == "untrusted"
    assert shape["specs"][0]["agent_type"] == "EXECUTOR"
    assert shape["specs"][1]["agent_type"] == "REVIEWER"
    assert shape["specs"][2]["agent_type"] == "JUDGE"


def test_unknown_shape_raises():
    with pytest.raises(untrusted_shapes.UnknownShapeError) as exc:
        untrusted_shapes.get_shape("does-not-exist")
    assert "fetch_web" in str(exc.value)


def test_render_shape_substitutes_goal():
    specs = untrusted_shapes.render_shape("fetch_web", {"goal": "SENTINEL_GOAL"})
    reviewer_goal = specs[1]["goal"]
    assert "SENTINEL_GOAL" in reviewer_goal


def test_render_shape_preserves_embedded_braces():
    """The executor goal contains a literal Python snippet with `{`."""
    specs = untrusted_shapes.render_shape("fetch_web", {"goal": "X"})
    assert "```python" in specs[0]["goal"]
    # Template substitution must not have escaped the braces.
    assert "{\"key\"" in specs[0]["goal"] or '{"key"' in specs[0]["goal"]


def test_render_shape_returns_fresh_copies():
    """Mutating one render must not affect subsequent renders."""
    first = untrusted_shapes.render_shape("fetch_web", {"goal": "A"})
    first[0]["name"] = "MUTATED"
    second = untrusted_shapes.render_shape("fetch_web", {"goal": "B"})
    assert second[0]["name"] != "MUTATED"


# ── validate_step_against_shape ────────────────────────────────────

def test_validate_step_unknown_shape():
    with pytest.raises(untrusted_shapes.UnknownShapeError):
        untrusted_shapes.validate_step_against_shape(
            {"name": "s", "untrusted_shape": "bogus"}
        )


@pytest.mark.parametrize("conflict_key,value", [
    ("agent_type", "EXECUTOR"),
    ("integrity_level", "untrusted"),
    ("reviewer_profile", "judge"),
    ("output_type", "json"),
])
def test_validate_step_rejects_conflicts(conflict_key, value):
    with pytest.raises(ValueError, match="owned by the shape"):
        untrusted_shapes.validate_step_against_shape({
            "name": "step",
            "untrusted_shape": "fetch_web",
            conflict_key: value,
        })


def test_validate_step_no_shape_is_noop():
    # No untrusted_shape declared → no-op.
    untrusted_shapes.validate_step_against_shape(
        {"name": "plain step", "order": 1}
    )


# ── create_untrusted_batch shared helper ───────────────────────────

def test_create_untrusted_batch_via_shape():
    """Rendering + helper reproduces the fetch_web arc structure."""
    parent_id = arc_manager.create_arc("parent", "Parent goal", agent_type="PLANNER")

    specs = untrusted_shapes.render_shape("fetch_web", {"goal": "extract"})
    for spec in specs:
        spec["parent_id"] = parent_id

    result = create_untrusted_batch(specs, parent_id=parent_id)
    assert "arc_ids" in result, result
    assert len(result["arc_ids"]) == 3

    executor_id, reviewer_id, judge_id = result["arc_ids"]

    ex = arc_manager.get_arc(executor_id)
    rv = arc_manager.get_arc(reviewer_id)
    jg = arc_manager.get_arc(judge_id)
    assert ex["integrity_level"] == "untrusted"
    assert rv["agent_type"] == "REVIEWER" and rv["integrity_level"] == "trusted"
    assert jg["agent_type"] == "JUDGE" and jg["integrity_level"] == "trusted"

    # Fernet key present for every (target, reviewer) pair.
    db = get_db()
    try:
        rows = db.execute(
            "SELECT reviewer_arc_id FROM review_keys WHERE target_arc_id = ?",
            (executor_id,),
        ).fetchall()
        reviewer_ids = {r["reviewer_arc_id"] for r in rows}
        assert reviewer_ids == {reviewer_id, judge_id}

        # _reviewer_profile + _review_target wired for the REVIEWER.
        prof = db.execute(
            "SELECT value_json FROM arc_state "
            "WHERE arc_id = ? AND key = '_reviewer_profile'",
            (reviewer_id,),
        ).fetchone()
        assert json.loads(prof["value_json"]) == "security-reviewer"

        tgt = db.execute(
            "SELECT value_json FROM arc_state "
            "WHERE arc_id = ? AND key = '_review_target'",
            (reviewer_id,),
        ).fetchone()
        assert json.loads(tgt["value_json"]) == executor_id
    finally:
        db.close()


def test_create_untrusted_batch_rejects_missing_reviewer():
    result = create_untrusted_batch([
        {"name": "tainted", "integrity_level": "untrusted"},
    ])
    assert "error" in result
    assert "REVIEWER or JUDGE" in result["error"]


def test_create_untrusted_batch_rejects_conflicting_parents():
    parent_a = arc_manager.create_arc("a")
    parent_b = arc_manager.create_arc("b")
    result = create_untrusted_batch([
        {"name": "x", "parent_id": parent_a},
        {"name": "y", "parent_id": parent_b},
    ])
    assert "error" in result
    assert "same parent_id" in result["error"]


def test_create_untrusted_batch_rejects_bad_reviewer_profile():
    result = create_untrusted_batch([
        {
            "name": "rev",
            "agent_type": "REVIEWER",
            "reviewer_profile": "no-such-profile",
        },
    ])
    assert "error" in result
    assert "Unknown agent_role" in result["error"]


def test_create_untrusted_batch_trusted_only_passes_through():
    """Pure-trusted batches shouldn't create any review_keys rows."""
    result = create_untrusted_batch([
        {"name": "a"},
        {"name": "b"},
    ])
    assert len(result["arc_ids"]) == 2
    db = get_db()
    try:
        rows = db.execute(
            "SELECT COUNT(*) AS n FROM review_keys "
            "WHERE target_arc_id IN (?, ?)",
            tuple(result["arc_ids"]),
        ).fetchone()
        assert rows["n"] == 0
    finally:
        db.close()
