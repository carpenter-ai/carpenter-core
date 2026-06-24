"""Tests for SUPERVISOR agent_type.

SUPERVISOR arcs are passive escalation handlers — created silent, never
auto-dispatched, only invoked when a child fails. Multiple concurrent
failures coalesce into a single wake via the work-queue idempotency key.
"""

import json
from unittest.mock import patch, AsyncMock

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.arcs import dispatch_handler as arc_dispatch_handler
from carpenter.core.arcs.supervisor_wake_handler import handle_supervisor_wake
from carpenter.core.engine import work_queue
from carpenter.db import get_db


def _work_items(event_type: str) -> list[dict]:
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM work_queue WHERE event_type = ?", (event_type,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def _arc_state(arc_id: int, key: str):
    db = get_db()
    try:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = ?",
            (arc_id, key),
        ).fetchone()
        return json.loads(row["value_json"]) if row else None
    finally:
        db.close()


# ── Creation: SUPERVISOR not enqueued, heartbeat skips ────────────


def test_supervisor_creation_not_enqueued():
    """A freshly-created SUPERVISOR (no code) is not on the work queue."""
    sup = arc_manager.create_arc("sup", agent_type="SUPERVISOR")

    items = _work_items("arc.dispatch")
    payload_ids = [json.loads(it["payload_json"]).get("arc_id") for it in items]
    assert sup not in payload_ids


def test_heartbeat_skips_supervisor_arcs():
    """scan_for_ready_arcs does not enqueue SUPERVISOR arcs."""
    sup = arc_manager.create_arc("sup", agent_type="SUPERVISOR")
    # SUPERVISOR is at status='pending' by default.

    arc_dispatch_handler.scan_for_ready_arcs()

    items = _work_items("arc.dispatch")
    payload_ids = [json.loads(it["payload_json"]).get("arc_id") for it in items]
    assert sup not in payload_ids


def test_heartbeat_still_enqueues_planner_arcs():
    """Sanity: heartbeat exclusion is SUPERVISOR-specific, not all types."""
    planner = arc_manager.create_arc("planner", agent_type="PLANNER")
    # PLANNERs without code are enqueued by the heartbeat (after children),
    # but the heartbeat also enqueues childless pending arcs as a safety net.

    arc_dispatch_handler.scan_for_ready_arcs()
    items = _work_items("arc.dispatch")
    payload_ids = [json.loads(it["payload_json"]).get("arc_id") for it in items]
    assert planner in payload_ids


# ── Happy path: SUPERVISOR completes quietly when children succeed ────


@pytest.mark.asyncio
async def test_supervisor_no_llm_when_all_children_complete():
    """SUPERVISOR with all children completing runs zero agent invocations."""
    sup = arc_manager.create_arc("sup", agent_type="SUPERVISOR")
    arc_manager.update_status(sup, "active")
    child = arc_manager.add_child(sup, "child", goal="do work")
    arc_manager.update_status(sup, "waiting")
    arc_manager.update_status(child, "active")
    arc_manager.update_status(child, "completed")

    # Propagation step: freeze_arc(parent) after child completes.
    arc_manager.freeze_arc(sup)

    # No supervisor_wake should have fired, no LLM mock needed.
    assert _work_items("arc.supervisor_wake") == []
    assert arc_manager.get_arc(sup)["status"] == "completed"


@pytest.mark.asyncio
async def test_dispatch_skips_passive_supervisor_no_wake_flag():
    """handle_arc_dispatch on a SUPERVISOR with no wake flag is a no-op."""
    sup = arc_manager.create_arc("sup", agent_type="SUPERVISOR")
    # Force an arc.dispatch via a payload with no supervisor_wake flag.

    with patch.object(arc_dispatch_handler, "_run_arc_agent", new=AsyncMock()) as mock_run:
        await arc_dispatch_handler.handle_arc_dispatch(work_id=1, payload={"arc_id": sup})

    mock_run.assert_not_called()
    # Arc stays pending — dispatch was skipped before the state transition.
    assert arc_manager.get_arc(sup)["status"] == "pending"


# ── Failure: single child fails → wake enqueued once ────────


def test_single_child_failure_enqueues_supervisor_wake():
    """One child failing enqueues exactly one arc.supervisor_wake and one pending failure."""
    sup = arc_manager.create_arc("sup", agent_type="SUPERVISOR")
    arc_manager.update_status(sup, "active")
    child = arc_manager.add_child(sup, "child", goal="risky thing")
    arc_manager.update_status(sup, "waiting")
    arc_manager.update_status(child, "active")
    arc_manager.update_status(child, "failed")

    # Standard arc.child_failed handler should NOT have fired
    assert _work_items("arc.child_failed") == []

    wakes = _work_items("arc.supervisor_wake")
    assert len(wakes) == 1
    payload = json.loads(wakes[0]["payload_json"])
    assert payload["parent_id"] == sup

    failures = _arc_state(sup, "_pending_failures")
    assert failures is not None and len(failures) == 1
    assert failures[0]["child_id"] == child
    assert failures[0]["child_name"] == "child"
    assert failures[0]["child_goal"] == "risky thing"


def test_concurrent_failures_coalesce_into_single_wake():
    """Two children failing back-to-back enqueue ONE wake but both failures land."""
    sup = arc_manager.create_arc("sup", agent_type="SUPERVISOR")
    arc_manager.update_status(sup, "active")
    a = arc_manager.add_child(sup, "a", goal="alpha")
    b = arc_manager.add_child(sup, "b", goal="beta")
    arc_manager.update_status(sup, "waiting")
    arc_manager.update_status(a, "active")
    arc_manager.update_status(a, "failed")
    # Don't drain the queue; failure b lands while wake-a is still pending.
    arc_manager.update_status(b, "active")
    arc_manager.update_status(b, "failed")

    wakes = _work_items("arc.supervisor_wake")
    assert len(wakes) == 1

    failures = _arc_state(sup, "_pending_failures")
    assert failures is not None and len(failures) == 2
    child_ids = {f["child_id"] for f in failures}
    assert child_ids == {a, b}


def test_supervisor_does_not_propagate_failure_while_waiting():
    """SUPERVISOR stays in 'waiting' after a child fails, not 'failed'."""
    sup = arc_manager.create_arc("sup", agent_type="SUPERVISOR")
    arc_manager.update_status(sup, "active")
    child = arc_manager.add_child(sup, "child", goal="do work")
    arc_manager.update_status(sup, "waiting")
    arc_manager.update_status(child, "active")
    arc_manager.update_status(child, "failed")

    # Manually invoke propagation as dispatch_handler would.
    arc_manager.freeze_arc(sup)

    assert arc_manager.get_arc(sup)["status"] == "waiting"


# ── Wake handler: reads-and-clears, re-dispatches with summary ────


@pytest.mark.asyncio
async def test_wake_handler_clears_failures_and_redispatches():
    """handle_supervisor_wake clears _pending_failures and enqueues arc.dispatch."""
    sup = arc_manager.create_arc("sup", agent_type="SUPERVISOR")
    arc_manager.update_status(sup, "active")
    child = arc_manager.add_child(sup, "child", goal="risky")
    arc_manager.update_status(sup, "waiting")
    arc_manager.update_status(child, "active")
    arc_manager.update_status(child, "failed")

    # Drain the auto-enqueued wake from the queue (we'll invoke handler directly).
    wakes = _work_items("arc.supervisor_wake")
    assert len(wakes) == 1
    work_id = wakes[0]["id"]

    await handle_supervisor_wake(work_id=work_id, payload={"parent_id": sup})

    # _pending_failures has been read-and-cleared to []
    assert _arc_state(sup, "_pending_failures") == []

    # A follow-up arc.dispatch with supervisor_wake=True was enqueued
    dispatches = [
        json.loads(it["payload_json"]) for it in _work_items("arc.dispatch")
        if json.loads(it["payload_json"]).get("arc_id") == sup
    ]
    assert any(p.get("supervisor_wake") for p in dispatches)
    summary_dispatches = [p for p in dispatches if p.get("supervisor_wake")]
    assert "child" in summary_dispatches[0]["failure_summary"]


@pytest.mark.asyncio
async def test_wake_dispatch_invokes_agent_with_failure_summary():
    """When dispatch sees supervisor_wake=True, it invokes the agent with the summary in the goal."""
    sup = arc_manager.create_arc("sup", goal="watch over things", agent_type="SUPERVISOR")
    arc_manager.update_status(sup, "active")
    child = arc_manager.add_child(sup, "child", goal="risky")
    arc_manager.update_status(sup, "waiting")
    arc_manager.update_status(child, "active")
    arc_manager.update_status(child, "failed")

    with patch.object(arc_dispatch_handler, "_run_arc_agent", new=AsyncMock()) as mock_run:
        await arc_dispatch_handler.handle_arc_dispatch(
            work_id=99,
            payload={
                "arc_id": sup,
                "supervisor_wake": True,
                "failure_summary": "## Pending Child Failures\n\n1. Arc #X (child) failed",
            },
        )

    mock_run.assert_called_once()
    args = mock_run.call_args
    invoked_goal = args.args[1] if len(args.args) > 1 else args.kwargs.get("goal")
    assert "watch over things" in invoked_goal
    assert "Pending Child Failures" in invoked_goal


@pytest.mark.asyncio
async def test_wake_after_completion_can_refire():
    """Singleton semantic: after a wake completes, a new failure fires a fresh wake.

    The work_queue.enqueue() helper deletes ``complete``/``dead_letter`` rows
    with the same idempotency key, so the second failure's enqueue succeeds.
    """
    sup = arc_manager.create_arc("sup", agent_type="SUPERVISOR")
    arc_manager.update_status(sup, "active")
    a = arc_manager.add_child(sup, "a")
    b = arc_manager.add_child(sup, "b")
    arc_manager.update_status(sup, "waiting")
    arc_manager.update_status(a, "active")
    arc_manager.update_status(a, "failed")

    # Mark the first wake as complete to simulate handler finishing.
    db = get_db()
    try:
        db.execute(
            "UPDATE work_queue SET status = 'complete' "
            "WHERE event_type = 'arc.supervisor_wake'"
        )
        db.commit()
    finally:
        db.close()

    # Second failure should be able to enqueue a fresh wake.
    arc_manager.update_status(b, "active")
    arc_manager.update_status(b, "failed")

    pending_wakes = [
        it for it in _work_items("arc.supervisor_wake")
        if it["status"] == "pending"
    ]
    assert len(pending_wakes) == 1
    failures = _arc_state(sup, "_pending_failures")
    # Both failures recorded (the first wake didn't drain — we simulated completion only).
    assert len(failures) == 2


# ── Capability matrix sanity ────


def test_supervisor_capability_present():
    """SUPERVISOR appears in the agent capability matrix with planner-like tools."""
    from carpenter.core.trust.types import AgentType, get_agent_capabilities
    caps = get_agent_capabilities()
    assert AgentType.SUPERVISOR in caps
    entry = caps[AgentType.SUPERVISOR]
    assert entry["can_create_untrusted_arcs"] is True
    tools = entry["allowed_tools"]
    assert tools is not None
    assert "arc.create" in tools
    assert "arc.add_child" in tools
    assert "messaging.send" in tools


def test_validate_agent_type_accepts_supervisor():
    from carpenter.core.trust.types import validate_agent_type
    assert validate_agent_type("SUPERVISOR") == "SUPERVISOR"
