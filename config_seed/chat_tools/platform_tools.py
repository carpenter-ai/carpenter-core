"""Chat tools for platform status and configuration queries."""

import json

from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description=(
        "Get current platform status: active arcs, in-flight work items, "
        "and restart state."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    capabilities=["config_read"],
)
def get_platform_status(tool_input, **kwargs):
    from carpenter.db import get_db
    from carpenter.core.engine import main_loop

    db = get_db()
    try:
        active_arcs = db.execute(
            "SELECT COUNT(*) FROM arcs WHERE status='active'"
        ).fetchone()[0]
        pending_arcs = db.execute(
            "SELECT COUNT(*) FROM arcs WHERE status='pending'"
        ).fetchone()[0]
        pending_work = db.execute(
            "SELECT COUNT(*) FROM work_queue WHERE status='pending'"
        ).fetchone()[0]
    finally:
        db.close()

    in_flight = len(main_loop._in_flight)
    restart_pending = main_loop._restart_pending
    restart_mode = main_loop._restart_mode if restart_pending else None

    parts = [
        f"Active arcs: {active_arcs}",
        f"Pending arcs: {pending_arcs}",
        f"Pending work items: {pending_work}",
        f"In-flight handlers: {in_flight}",
    ]
    if restart_pending:
        parts.append(f"Restart pending: yes (mode={restart_mode})")
    else:
        parts.append("Restart pending: no")
    return "\n".join(parts)


@chat_tool(
    description=(
        "List all platform config keys that can be changed at runtime, with "
        "their current values and human-readable descriptions. Use this to "
        "discover which setting controls a feature before changing it."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    capabilities=["config_read"],
)
def list_config_keys(tool_input, **kwargs):
    from carpenter.tool_backends import config_tool as config_tool_backend
    result = config_tool_backend.handle_list_keys({})
    return json.dumps(result)


@chat_tool(
    description=(
        "List all configured AI models with their provider, cost tier, "
        "context window, and assigned roles."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    capabilities=["config_read"],
)
def list_models(tool_input, **kwargs):
    from carpenter.tool_backends import config_tool as config_tool_backend
    result = config_tool_backend.handle_models({})
    return json.dumps(result)


@chat_tool(
    description=(
        "List all active scheduled tasks (cron entries) with their cron "
        "expressions, target event types, and next fire times. Schedules "
        "flow through the trigger/event pipeline: cron fires -> timer.fired "
        "event -> subscription -> work_queue handler."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    capabilities=["config_read"],
)
def list_schedules(tool_input, **kwargs):
    from carpenter.core.engine import trigger_manager
    entries = trigger_manager.list_cron()
    return json.dumps({
        "entries": entries,
        "pipeline": "timer.fired -> subscription -> work_queue",
    })


@chat_tool(
    description=(
        "List all currently outstanding human-approval reviews. Returns "
        "gated arcs (typically reflection-spawned action arcs) that have "
        "a review_url and are still waiting for the user to approve or "
        "reject them. Use when the user asks what reviews are pending or "
        "needs the review URLs."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    capabilities=["config_read"],
)
def list_pending_reviews(tool_input, **kwargs):
    from carpenter.db import db_connection
    from carpenter.core.arcs import manager as arc_manager
    from carpenter.core.workflows._arc_state import get_arc_state
    from carpenter.core.workflows.arc_notify_handler import (
        _absolutize_review_url,
    )

    entries: list[dict] = []
    with db_connection() as db:
        rows = db.execute(
            "SELECT a.id, a.name, a.status, a.parent_id, a.goal FROM arcs a "
            "WHERE EXISTS ("
            "  SELECT 1 FROM arc_state s "
            "  WHERE s.arc_id = a.id AND s.key = 'review_url'"
            ")"
        ).fetchall()
    for row in rows:
        if row["status"] in arc_manager.FROZEN_STATUSES:
            continue
        if get_arc_state(row["id"], "_review_mode") != "human":
            continue
        url = get_arc_state(row["id"], "review_url")
        if not url:
            continue
        description = row["goal"] or row["name"] or f"arc #{row['id']}"
        entries.append({
            "arc_id": row["id"],
            "name": row["name"],
            "status": row["status"],
            "parent_arc_id": row["parent_id"],
            "description": description,
            "url": _absolutize_review_url(url),
        })
    return json.dumps({"pending_reviews": entries})
