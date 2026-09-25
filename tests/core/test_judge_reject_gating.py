"""A JUDGE that does not approve must fail its run.

In a batch built by ``arc.create_batch`` the untrusted target precedes
its REVIEWER and JUDGE in step order, so the target has already frozen
as ``completed`` when the JUDGE issues its verdict.  Frozen arcs never
change status, so the rejection has to be carried by the JUDGE arc
itself: the JUDGE fails, later siblings stay blocked, and the parent
rolls up to ``failed`` rather than ``completed``.
"""

import json
from unittest.mock import patch

import pytest

from carpenter.core.arcs import dispatch_handler
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.arcs.root_failure_handler import (
    escalate_to_next_model,
    failed_judge_in_subtree,
)
from carpenter.db import get_db
from carpenter.security.judge import JudgeResult, PolicyCheck
from carpenter.tool_backends import arc as arc_backend


def _build_review_tree(extra_sibling: bool = False) -> dict:
    """PLANNER parent with EXECUTOR(untrusted) -> REVIEWER -> JUDGE.

    Drives the tree to the point where the JUDGE is about to run: the
    target and the REVIEWER have completed and the parent is waiting.
    """
    parent = arc_manager.create_arc("review-tree", goal="g", agent_type="PLANNER")
    arc_manager.update_status(parent, "active")
    arcs = [
        {"name": "target", "parent_id": parent, "integrity_level": "untrusted",
         "agent_type": "EXECUTOR", "step_order": 0},
        {"name": "reviewer", "parent_id": parent, "agent_type": "REVIEWER",
         "reviewer_profile": "security-reviewer", "step_order": 1},
        {"name": "judge", "parent_id": parent, "agent_type": "JUDGE",
         "reviewer_profile": "judge", "step_order": 2},
    ]
    result = arc_backend.handle_create_batch({"arcs": arcs})
    assert "arc_ids" in result, result
    target, reviewer, judge = result["arc_ids"]

    consumer = None
    if extra_sibling:
        consumer = arc_manager.create_arc(
            "consumer", goal="use the reviewed output",
            parent_id=parent, step_order=3,
        )

    for arc_id in (target, reviewer):
        arc_manager.update_status(arc_id, "active")
        arc_manager.update_status(arc_id, "completed")
    arc_manager.freeze_arc(parent)  # active -> waiting (children still open)
    assert arc_manager.get_arc(parent)["status"] == "waiting"
    return {"parent": parent, "target": target, "reviewer": reviewer,
            "judge": judge, "consumer": consumer}


def _reject(reason="Policy check(s) failed"):
    return JudgeResult(
        approved=False,
        checks=[PolicyCheck("f", "email", "x", passed=False, reason="nope")],
        reason=reason,
    )


def _history(arc_id, entry_type):
    return [
        json.loads(h["content_json"])
        for h in arc_manager.get_history(arc_id)
        if h["entry_type"] == entry_type
    ]


async def _dispatch(arc_id):
    await dispatch_handler.handle_arc_dispatch(0, {"arc_id": arc_id})


@pytest.mark.asyncio
async def test_reject_after_target_completed_fails_judge_and_parent():
    """Regression: a reject on an already-completed target used to be
    dropped, and the parent completed as if the JUDGE had approved."""
    t = _build_review_tree()

    with patch("carpenter.security.judge.run_policy_checks", return_value=_reject()):
        await _dispatch(t["judge"])

    # The frozen target stays frozen and untrusted.
    target = arc_manager.get_arc(t["target"])
    assert target["status"] == "completed"
    assert target["integrity_level"] == "untrusted"
    assert _history(t["target"], "review_verdict")[0]["decision"] == "reject"

    # The JUDGE carries the rejection, and the parent rolls it up.
    assert arc_manager.get_arc(t["judge"])["status"] == "failed"
    assert _history(t["judge"], "judge_rejected")[0]["target_arc_id"] == t["target"]
    assert arc_manager.get_arc(t["parent"])["status"] == "failed"


@pytest.mark.asyncio
async def test_approve_completes_judge_and_parent():
    t = _build_review_tree()

    with patch(
        "carpenter.security.judge.run_policy_checks",
        return_value=JudgeResult(approved=True, reason="ok"),
    ):
        await _dispatch(t["judge"])

    assert arc_manager.get_arc(t["judge"])["status"] == "completed"
    assert arc_manager.get_arc(t["parent"])["status"] == "completed"
    assert arc_manager.get_arc(t["target"])["integrity_level"] == "trusted"
    assert _history(t["judge"], "judge_rejected") == []


@pytest.mark.asyncio
async def test_reject_blocks_later_sibling():
    """A sibling after the JUDGE must not run on rejected output."""
    t = _build_review_tree(extra_sibling=True)

    with patch("carpenter.security.judge.run_policy_checks", return_value=_reject()):
        await _dispatch(t["judge"])

    assert arc_manager.get_arc(t["judge"])["status"] == "failed"
    assert arc_manager.check_dependencies(t["consumer"]) is False
    assert arc_manager.get_arc(t["consumer"])["status"] == "pending"
    db = get_db()
    try:
        row = db.execute(
            "SELECT id FROM work_queue WHERE event_type = 'arc.dispatch' "
            "AND payload_json = ?",
            (json.dumps({"arc_id": t["consumer"]}),),
        ).fetchone()
    finally:
        db.close()
    assert row is None
    assert arc_manager.get_arc(t["parent"])["status"] != "completed"


@pytest.mark.asyncio
async def test_judge_check_exception_fails_closed():
    t = _build_review_tree()

    with patch(
        "carpenter.security.judge.run_policy_checks",
        side_effect=RuntimeError("plugin blew up"),
    ):
        await _dispatch(t["judge"])

    assert arc_manager.get_arc(t["judge"])["status"] == "failed"
    assert arc_manager.get_arc(t["parent"])["status"] == "failed"
    entry = _history(t["judge"], "judge_rejected")[0]
    assert "RuntimeError" in entry["reason"]
    assert "Traceback" in entry["traceback"]
    assert arc_manager.get_arc(t["target"])["integrity_level"] == "untrusted"


@pytest.mark.asyncio
async def test_judge_without_review_target_fails():
    parent = arc_manager.create_arc("p", goal="g", agent_type="PLANNER")
    arc_manager.update_status(parent, "active")
    judge = arc_manager.create_arc(
        "judge", goal="g", parent_id=parent, agent_type="JUDGE",
    )

    with patch(
        "carpenter.security.judge.run_policy_checks",
        return_value=JudgeResult(approved=True, reason="ok"),
    ):
        await _dispatch(judge)

    assert arc_manager.get_arc(judge)["status"] == "failed"
    assert arc_manager.get_arc(parent)["status"] == "failed"


def test_judge_arc_is_never_model_escalated(monkeypatch):
    import carpenter.config
    monkeypatch.setitem(
        carpenter.config.CONFIG, "escalation",
        {"stacks": {"general": ["model-small", "model-large"]}},
    )
    carpenter.config.CONFIG.setdefault("model_roles", {})["default"] = "model-small"
    judge = arc_manager.create_arc("judge", goal="g", agent_type="JUDGE")

    assert escalate_to_next_model(judge) is None
    assert arc_manager.get_arc(judge)["status"] == "pending"


@pytest.mark.asyncio
async def test_root_failed_by_judge_is_not_escalated(monkeypatch):
    """Re-running a rejected tree on a stronger model would repeat the
    rejected work; the root failure is reported instead."""
    import carpenter.config
    monkeypatch.setitem(
        carpenter.config.CONFIG, "escalation",
        {"stacks": {"default": ["model-small", "model-medium", "model-large"]}},
    )
    carpenter.config.CONFIG.setdefault("model_roles", {})["default"] = "model-small"
    t = _build_review_tree()

    with patch("carpenter.security.judge.run_policy_checks", return_value=_reject()), \
         patch("carpenter.core.notifications.notify") as mock_notify:
        await _dispatch(t["judge"])

    assert arc_manager.get_arc(t["parent"])["status"] == "failed"
    assert failed_judge_in_subtree(t["parent"]) == t["judge"]
    db = get_db()
    try:
        escalated = db.execute(
            "SELECT id FROM arcs WHERE name LIKE '%escalated%'"
        ).fetchall()
    finally:
        db.close()
    assert escalated == []
    categories = [c.kwargs.get("category") for c in mock_notify.call_args_list]
    assert "judge_rejected" in categories


def test_failed_judge_in_subtree_none_without_failed_judge():
    t_parent = arc_manager.create_arc("p", goal="g", agent_type="PLANNER")
    arc_manager.create_arc("judge", goal="g", parent_id=t_parent, agent_type="JUDGE")
    assert failed_judge_in_subtree(t_parent) is None
