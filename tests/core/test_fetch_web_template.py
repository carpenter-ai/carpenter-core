"""Regression tests for the ``fetch_web`` workflow template.

The ``fetch_web`` arc-batch lives in
``config_seed/templates/fetch_web.yaml`` and is loaded into the
runtime template store like any other workflow template.  These tests
pin its instantiated batch behaviour so a regression in either the
YAML or the template loader breaks loud.

What we assert about the instantiated batch:

- exactly three children: EXECUTOR-untrusted → REVIEWER-trusted → JUDGE-trusted
- ``output_type: json`` on the executor (the runtime
  ``create_untrusted_batch`` invariant)
- monotonic ``step_order`` (0, 1, 2) under the parent
- ``reviewer_profile`` arc_state set to ``"security-reviewer"`` /
  ``"judge"`` (used by the review pipeline to pick prompts)
- ``review_target`` arc_state on each REVIEWER/JUDGE pointing at the
  EXECUTOR (so the reviewers know what they are reviewing)
- ``review_keys`` rows for each (EXECUTOR, reviewer) pair (Fernet
  plumbing — required for encrypted untrusted output at rest)
- the YAML's ``$goal`` placeholder in the REVIEWER description gets
  replaced by the runtime ``bindings={"goal": ...}`` value
"""

from __future__ import annotations

import json
import os

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import template_manager
from carpenter.db import get_db
from carpenter.verify.registry import verify


_FETCH_WEB_YAML_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "config_seed", "templates", "fetch_web.yaml",
)


@pytest.fixture
def fetch_web_template_id() -> int:
    """Load the shipped fetch_web template into the isolated test DB."""
    return template_manager.load_template(_FETCH_WEB_YAML_PATH)


def test_fetch_web_yaml_passes_verifier():
    """The shipped template must pass the YAML-template verifier with no errors."""
    with open(_FETCH_WEB_YAML_PATH) as f:
        result = verify("yaml-template", f.read())
    error_findings = [f for f in result.findings if f.severity == "error"]
    assert result.ok, (
        f"fetch_web.yaml fails verifier with errors: {error_findings}"
    )


def test_instantiate_creates_three_children_with_correct_topology(
    fetch_web_template_id: int,
):
    """The batch must instantiate as EXECUTOR → REVIEWER → JUDGE under the parent."""
    parent_id = arc_manager.create_arc(
        "fetch parent", "fetch some URL", agent_type="PLANNER",
    )
    arc_ids = template_manager.instantiate_template(
        fetch_web_template_id,
        parent_id,
        bindings={"goal": "extract the headline"},
    )
    assert len(arc_ids) == 3, arc_ids

    executor = arc_manager.get_arc(arc_ids[0])
    reviewer = arc_manager.get_arc(arc_ids[1])
    judge = arc_manager.get_arc(arc_ids[2])

    # Trust topology
    assert executor["agent_type"] == "EXECUTOR"
    assert executor["integrity_level"] == "untrusted"
    assert executor["output_type"] == "json"

    assert reviewer["agent_type"] == "REVIEWER"
    assert reviewer["integrity_level"] == "trusted"

    assert judge["agent_type"] == "JUDGE"
    assert judge["integrity_level"] == "trusted"

    # Step ordering
    assert executor["step_order"] == 0
    assert reviewer["step_order"] == 1
    assert judge["step_order"] == 2

    # All children parented to the same arc
    for arc in (executor, reviewer, judge):
        assert arc["parent_id"] == parent_id
        assert arc["from_template"] == 1 or arc["from_template"] is True
        assert arc["template_id"] == fetch_web_template_id


def test_instantiate_wires_reviewer_profiles(fetch_web_template_id: int):
    """REVIEWER and JUDGE arc_state must carry the correct profile names."""
    parent_id = arc_manager.create_arc(
        "fetch parent", "fetch a URL", agent_type="PLANNER",
    )
    arc_ids = template_manager.instantiate_template(
        fetch_web_template_id,
        parent_id,
        bindings={"goal": "summarise"},
    )
    _, reviewer_id, judge_id = arc_ids

    db = get_db()
    try:
        rev_prof = db.execute(
            "SELECT value_json FROM arc_state "
            "WHERE arc_id = ? AND key = '_reviewer_profile'",
            (reviewer_id,),
        ).fetchone()
        assert rev_prof is not None, (
            "REVIEWER arc must have _reviewer_profile arc_state"
        )
        assert json.loads(rev_prof["value_json"]) == "security-reviewer"

        jdg_prof = db.execute(
            "SELECT value_json FROM arc_state "
            "WHERE arc_id = ? AND key = '_reviewer_profile'",
            (judge_id,),
        ).fetchone()
        assert jdg_prof is not None
        assert json.loads(jdg_prof["value_json"]) == "judge"
    finally:
        db.close()


def test_instantiate_wires_review_targets_and_fernet_keys(
    fetch_web_template_id: int,
):
    """Each reviewer must point at the EXECUTOR; review_keys must exist."""
    parent_id = arc_manager.create_arc(
        "fetch parent", "fetch a URL", agent_type="PLANNER",
    )
    executor_id, reviewer_id, judge_id = template_manager.instantiate_template(
        fetch_web_template_id,
        parent_id,
        bindings={"goal": "extract"},
    )

    db = get_db()
    try:
        for reviewer_arc_id in (reviewer_id, judge_id):
            tgt = db.execute(
                "SELECT value_json FROM arc_state "
                "WHERE arc_id = ? AND key = '_review_target'",
                (reviewer_arc_id,),
            ).fetchone()
            assert tgt is not None, (
                f"arc {reviewer_arc_id} missing _review_target arc_state"
            )
            assert json.loads(tgt["value_json"]) == executor_id

        # Fernet keys: one row per (executor, reviewer) pair.
        rows = db.execute(
            "SELECT reviewer_arc_id FROM review_keys WHERE target_arc_id = ?",
            (executor_id,),
        ).fetchall()
        reviewer_ids = {r["reviewer_arc_id"] for r in rows}
        assert reviewer_ids == {reviewer_id, judge_id}
    finally:
        db.close()


def test_instantiate_substitutes_goal_into_reviewer_description(
    fetch_web_template_id: int,
):
    """The REVIEWER goal must contain the bound ``$goal`` value."""
    parent_id = arc_manager.create_arc(
        "fetch parent", "fetch a URL", agent_type="PLANNER",
    )
    sentinel = "EXTRACT_THIS_SPECIFIC_THING_FROM_PAGE_42"
    _, reviewer_id, _ = template_manager.instantiate_template(
        fetch_web_template_id,
        parent_id,
        bindings={"goal": sentinel},
    )
    reviewer = arc_manager.get_arc(reviewer_id)
    assert sentinel in (reviewer["goal"] or "")


def test_instantiate_preserves_executor_fetch_script(
    fetch_web_template_id: int,
):
    """The EXECUTOR goal must contain the literal pre-verified fetch script.

    The runtime ``_FETCH_SCRIPT`` constant in
    ``carpenter.agent.invocation`` and the YAML executor description
    must stay byte-for-byte equivalent — ``test_fetch_script_runs_in_restricted_sandbox``
    in ``test_fetch_web_content.py`` exercises the script via
    RestrictedPython and would silently drift if the YAML diverged.
    """
    from carpenter.agent.invocation import _FETCH_SCRIPT

    parent_id = arc_manager.create_arc(
        "fetch parent", "fetch a URL", agent_type="PLANNER",
    )
    executor_id, _, _ = template_manager.instantiate_template(
        fetch_web_template_id,
        parent_id,
        bindings={"goal": "any"},
    )
    executor = arc_manager.get_arc(executor_id)
    # Strip leading/trailing whitespace from each line of _FETCH_SCRIPT
    # before comparing — YAML block-scalar indentation is preserved
    # but the rendered text in the goal still contains every line.
    for line in _FETCH_SCRIPT.strip().splitlines():
        assert line.strip() in (executor["goal"] or ""), (
            f"executor goal missing fetch script line: {line!r}"
        )
