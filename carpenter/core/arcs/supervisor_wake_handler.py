"""Handler for SUPERVISOR arc wake-ups.

When a child of a SUPERVISOR arc fails, ``_notify_parent_of_failure`` in
:mod:`carpenter.core.arcs.manager` appends a failure record to the
supervisor's ``_pending_failures`` arc_state list and enqueues an
``arc.supervisor_wake`` work item with idempotency_key
``supervisor_wake:<parent_id>``.

This handler reads-and-clears the pending failure list atomically and
re-enqueues an ``arc.dispatch`` work item carrying a ``supervisor_wake``
flag and a textual failure summary, so the standard dispatch path
invokes the agent with the failure context in its goal.

Singleton semantic: while a wake is pending/claimed, additional failures
land in ``_pending_failures`` but cannot enqueue a second wake. Once
the wake completes (status='complete'), the next failure's enqueue will
clear the completed row and re-fire — picking up any further failures
that landed during dispatch.
"""

import json
import logging

from ...db import db_transaction

logger = logging.getLogger(__name__)


def _format_failure_summary(failures: list) -> str:
    if not failures:
        return ""
    lines = ["## Pending Child Failures", ""]
    for i, f in enumerate(failures, 1):
        lines.append(
            f"{i}. Arc #{f.get('child_id')} ({f.get('child_name', '?')}) "
            f"failed at {f.get('failed_at', '?')}"
        )
        goal = f.get("child_goal")
        if goal:
            lines.append(f"   Goal: {goal}")
        err = f.get("error_info")
        if err:
            lines.append(f"   Error: {err}")
    return "\n".join(lines)


async def handle_supervisor_wake(work_id: int, payload: dict):
    parent_id = payload.get("parent_id")
    if parent_id is None:
        logger.warning("arc.supervisor_wake missing parent_id")
        return

    from . import manager as arc_manager

    parent = arc_manager.get_arc(parent_id)
    if parent is None:
        logger.warning("Supervisor arc %d not found", parent_id)
        return
    if parent.get("agent_type") != "SUPERVISOR":
        logger.warning(
            "Arc %d is not a SUPERVISOR (agent_type=%s); ignoring wake",
            parent_id, parent.get("agent_type"),
        )
        return
    if parent["status"] != "waiting":
        logger.info(
            "Supervisor %d not in waiting status (%s); skipping wake",
            parent_id, parent["status"],
        )
        return

    with db_transaction() as db:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = '_pending_failures'",
            (parent_id,),
        ).fetchone()
        if row:
            try:
                failures = json.loads(row["value_json"])
                if not isinstance(failures, list):
                    failures = []
            except (ValueError, TypeError):
                failures = []
        else:
            failures = []
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?) "
            "ON CONFLICT(arc_id, key) DO UPDATE SET value_json = excluded.value_json, "
            "updated_at = CURRENT_TIMESTAMP",
            (parent_id, "_pending_failures", json.dumps([])),
        )

    if not failures:
        logger.info("Supervisor %d woken with no pending failures; no-op", parent_id)
        return

    summary = _format_failure_summary(failures)

    from ..engine import work_queue, main_loop
    work_queue.enqueue(
        "arc.dispatch",
        {
            "arc_id": parent_id,
            "supervisor_wake": True,
            "failure_summary": summary,
        },
        idempotency_key=f"supervisor_dispatch:{parent_id}:{work_id}",
    )
    main_loop.wake_signal.set()
    logger.info(
        "Supervisor %d wake: dispatched with %d pending failure(s)",
        parent_id, len(failures),
    )


def register_handlers(register_fn):
    register_fn("arc.supervisor_wake", handle_supervisor_wake)
