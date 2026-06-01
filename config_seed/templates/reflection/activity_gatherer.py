"""Activity data for a reflection: one completed arc's trajectory.

In the cadence-era this module aggregated N days of conversations. The
per-arc trigger model replaced that: a reflection fires when a root arc
completes, and reflects on that specific arc — its goal, its outputs,
its children, its outcome. :func:`gather_from_arc` produces the markdown
block fed into the reflect step's goal, framed by a small period-stats
block (see :mod:`.period_stats`).
"""

from __future__ import annotations

import logging

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.workflows._arc_state import get_arc_state
from carpenter.db import db_connection

from .period_stats import gather_period_stats

logger = logging.getLogger(__name__)

# Character cap when inlining a child arc's output.
_CHILD_OUTPUT_MAX_CHARS = 400
# Character cap when inlining the reflected arc's own final state.
_FINAL_STATE_MAX_CHARS = 1500


def gather_from_arc(arc_id: int) -> str:
    """Return the reflection prompt/data block for a single root arc.

    The block is structured so an LLM can directly analyse one arc's
    trajectory. It includes:

    - A framing instruction (what "reflect on this arc" means).
    - The arc's goal, status, timestamps.
    - Each child arc's name / status / truncated output.
    - The arc's own final ``_agent_response`` (if any).
    - A small period-stats block for context.

    Returns a markdown string — empty-safe if the arc is missing.
    """
    arc = arc_manager.get_arc(arc_id)
    if not arc:
        return (
            "# Reflection — arc missing\n\n"
            f"Arc #{arc_id} was not found; nothing to reflect on.\n"
        )

    parts: list[str] = [
        "# Reflection Data — single-arc trajectory",
        "",
        (
            "You are reflecting on a single completed arc. Read its goal, "
            "its child steps, and its final output. Identify what went "
            "well, what didn't, and any pattern worth preserving as a KB "
            "entry under `skills/`. Keep the reflection concise and "
            "actionable; prefer concrete observations over general "
            "advice."
        ),
        "",
        "## Arc",
        f"- id: #{arc['id']}",
        f"- name: {arc.get('name') or '(unnamed)'}",
        f"- status: {arc.get('status')}",
        f"- created_at: {arc.get('created_at') or ''}",
        f"- updated_at: {arc.get('updated_at') or ''}",
    ]

    goal = arc.get("goal") or ""
    if goal:
        parts.append("")
        parts.append("### Goal")
        parts.append(goal.strip())

    # Child arcs in step order.
    with db_connection() as db:
        child_rows = db.execute(
            "SELECT id, name, status, goal, step_order FROM arcs "
            "WHERE parent_id = ? ORDER BY step_order ASC, id ASC",
            (arc_id,),
        ).fetchall()

    if child_rows:
        parts.append("")
        parts.append("## Child steps")
        for c in child_rows:
            cg = (c["goal"] or "")[:120]
            parts.append(
                f"- #{c['id']} [{c['status']}] {c['name']}: {cg}"
            )
            response = get_arc_state(c["id"], "_agent_response")
            if response:
                snippet = response[:_CHILD_OUTPUT_MAX_CHARS]
                parts.append("")
                parts.append(f"  Output (first {_CHILD_OUTPUT_MAX_CHARS} chars):")
                for line in snippet.splitlines():
                    parts.append(f"  > {line}")
    else:
        parts.append("")
        parts.append("## Child steps")
        parts.append("- (none)")

    final_response = get_arc_state(arc_id, "_agent_response")
    if final_response:
        parts.append("")
        parts.append("## Final agent output")
        parts.append(final_response[:_FINAL_STATE_MAX_CHARS])

    parts.append("")
    parts.append(gather_period_stats(1))
    parts.append("")
    return "\n".join(parts)
