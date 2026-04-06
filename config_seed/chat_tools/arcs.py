"""Chat tools for arc introspection."""

import json

from carpenter.chat_tool_loader import chat_tool
from carpenter import config


@chat_tool(
    description=(
        "List arcs (units of work like coding changes). Returns id, name, "
        "status, goal, and creation time. Newest first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": (
                    "Filter by status: pending, active, waiting, completed, "
                    "failed, cancelled. Omit to show all."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default 20).",
            },
        },
        "required": [],
    },
    capabilities=["database_read"],
    always_available=True,
)
def list_arcs(tool_input, **kwargs):
    from carpenter.db import get_db
    status = tool_input.get("status")
    limit = tool_input.get("limit", 20)
    db = get_db()
    try:
        if status:
            rows = db.execute(
                "SELECT id, name, status, goal, created_at FROM arcs "
                "WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, name, status, goal, created_at FROM arcs "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    finally:
        db.close()
    if not rows:
        return "No arcs found."
    lines = []
    for r in rows:
        goal = (r["goal"] or "")[:80]
        lines.append(
            f"#{r['id']} [{r['status']}] {r['name']}: {goal}  ({r['created_at']})"
        )
    return "\n".join(lines)


@chat_tool(
    description=(
        "Get full details for an arc: status, goal, all state keys "
        "(workspace path, review URL, changed files, etc.), and history log. "
        "Use this after list_arcs to dive deeper."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "arc_id": {
                "type": "integer",
                "description": "The arc ID to inspect.",
            },
        },
        "required": ["arc_id"],
    },
    capabilities=["database_read"],
    always_available=True,
)
def get_arc_detail(tool_input, **kwargs):
    from carpenter.core.arcs import manager as arc_manager
    from carpenter.db import get_db
    arc_id = tool_input["arc_id"]
    arc = arc_manager.get_arc(arc_id)
    if arc is None:
        return f"Arc #{arc_id} not found."

    # Arc summary
    parts = [
        f"Arc #{arc['id']}: {arc['name']}",
        f"  Status: {arc['status']}",
        f"  Goal: {arc.get('goal') or '(none)'}",
        f"  Created: {arc['created_at']}",
    ]
    if arc.get("parent_id"):
        parts.append(f"  Parent: #{arc['parent_id']}")

    # Performance counters
    desc_tokens = arc.get("descendant_tokens", 0) or 0
    desc_execs = arc.get("descendant_executions", 0) or 0
    desc_arcs = arc.get("descendant_arc_count", 0) or 0
    if desc_tokens or desc_execs or desc_arcs:
        parts.append(
            f"  Counters: tokens={desc_tokens}, executions={desc_execs}, "
            f"child_arcs={desc_arcs}"
        )

    # Arc state
    db = get_db()
    try:
        state_rows = db.execute(
            "SELECT key, value_json FROM arc_state WHERE arc_id = ? ORDER BY key",
            (arc_id,),
        ).fetchall()
    finally:
        db.close()
    if state_rows:
        parts.append("\nState:")
        for row in state_rows:
            key = row["key"]
            val = row["value_json"]
            max_len = config.get_config("arc_state_value_max_length", 300)
            if len(val) > max_len:
                val = val[:max_len] + "..."
            parts.append(f"  {key}: {val}")

    # History
    history = arc_manager.get_history(arc_id)
    if history:
        parts.append(f"\nHistory ({len(history)} entries):")
        for h in history:
            content = h.get("content_json", "{}")
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    pass
            if isinstance(content, dict):
                summary = ", ".join(
                    f"{k}={str(v)[:60]}" for k, v in content.items()
                )
            else:
                summary = str(content)[:120]
            parts.append(
                f"  [{h['created_at']}] {h['entry_type']} by {h.get('actor', '?')}: {summary}"
            )

    return "\n".join(parts)


@chat_tool(
    description=(
        "Read the full result from a completed arc. Use this when an arc "
        "completion notification was truncated and you need the complete "
        "output. Only works for arcs in 'completed' status. Returns the "
        "full _agent_response content without truncation."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "arc_id": {
                "type": "integer",
                "description": "The arc ID to read the full result from.",
            },
            "offset": {
                "type": "integer",
                "description": (
                    "Character offset to start reading from (default 0). "
                    "Use this for paginating through very large results."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Maximum characters to return (default 50000). "
                    "Use with offset to paginate through large results."
                ),
            },
        },
        "required": ["arc_id"],
    },
    capabilities=["database_read"],
    always_available=True,
)
def read_arc_result(tool_input, **kwargs):
    from carpenter.core.arcs import manager as arc_manager
    from carpenter.core.workflows._arc_state import get_arc_state

    arc_id = tool_input["arc_id"]
    offset = tool_input.get("offset", 0)
    char_limit = tool_input.get("limit", 50000)

    arc = arc_manager.get_arc(arc_id)
    if arc is None:
        return f"Arc #{arc_id} not found."

    if arc["status"] != "completed":
        return (
            f"Arc #{arc_id} has status '{arc['status']}'. "
            f"read_arc_result only works for completed arcs."
        )

    # Try root arc's _agent_response first
    result = get_arc_state(arc_id, "_agent_response", "") or ""

    # If root arc has no response, check children (same logic as arc_notify_handler)
    if not result:
        children = arc_manager.get_children(arc_id) or []
        for child in reversed(children):
            child_resp = get_arc_state(child["id"], "_agent_response", "") or ""
            if child_resp:
                result = child_resp
                break

    if not result:
        return f"Arc #{arc_id} has no result content."

    total_len = len(result)
    chunk = result[offset:offset + char_limit]

    if total_len <= char_limit and offset == 0:
        return chunk

    # Include pagination metadata for large results
    remaining = max(0, total_len - offset - char_limit)
    return (
        f"[Showing characters {offset}-{offset + len(chunk)} of {total_len} total"
        f"{f', {remaining} remaining' if remaining else ''}]\n{chunk}"
    )


@chat_tool(
    description=(
        "List recent work queue items showing what's been processing. "
        "Returns event type, status, timestamps, and errors if any."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max results (default 10).",
            },
        },
        "required": [],
    },
    capabilities=["database_read"],
)
def list_recent_activity(tool_input, **kwargs):
    from carpenter.db import get_db
    limit = tool_input.get("limit", 10)
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, event_type, status, error, created_at, completed_at "
            "FROM work_queue ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        db.close()
    if not rows:
        return "No recent activity."
    lines = []
    for r in rows:
        line = f"#{r['id']} {r['event_type']} [{r['status']}] created={r['created_at']}"
        if r["completed_at"]:
            line += f" completed={r['completed_at']}"
        if r["error"]:
            error = r["error"][:100]
            line += f" error={error}"
        lines.append(line)
    return "\n".join(lines)
