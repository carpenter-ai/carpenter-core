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


# Cap how many arcs we inline in full for a batch, to bound the reflect
# step's prompt size. Beyond this the batch is summarised by id only.
_BATCH_FULL_DETAIL_MAX = 8


def gather_from_subject(subject: dict) -> str:
    """Return the reflection data block for a typed subject.

    - ``arcs`` (one ref) → the single-arc block (unchanged behaviour).
    - ``period`` / ``arcs`` (many) → a batch block: a framing header plus
      each arc's trajectory (capped), so the reflect step can find
      cross-arc patterns across the day's work.
    - ``theme`` → a set-of-updates block built from the KB subtree named
      by the subject (``theme``/``slug``).
    """
    from ._subject import KIND_THEME, subject_arc_ids

    if subject.get("kind") == KIND_THEME:
        return _gather_theme(subject)

    arc_ids = subject_arc_ids(subject)
    if not arc_ids:
        return "# Reflection — no arcs in subject\n"
    if len(arc_ids) == 1:
        return gather_from_arc(arc_ids[0])

    window = subject.get("window") or {}
    parts = [
        "# Reflection Data — batch of completed arcs",
        "",
        (
            "You are reflecting on a *batch* of arcs that completed in one "
            "period. Look across them for recurring patterns, repeated "
            "failures, and lessons worth distilling into a KB entry under "
            "`skills/`. Note that several of these may be related. Prefer "
            "concrete, cross-cutting observations over per-arc detail."
        ),
        "",
        f"- period: {window.get('from', '?')} → {window.get('to', '?')}",
        f"- arc count: {len(arc_ids)}",
        f"- arc ids: {', '.join('#' + str(i) for i in arc_ids)}",
        "",
    ]
    for aid in arc_ids[:_BATCH_FULL_DETAIL_MAX]:
        parts.append("---")
        parts.append(gather_from_arc(aid))
    if len(arc_ids) > _BATCH_FULL_DETAIL_MAX:
        rest = arc_ids[_BATCH_FULL_DETAIL_MAX:]
        parts.append("---")
        parts.append(
            f"## {len(rest)} further arc(s) in this batch (ids only): "
            + ", ".join("#" + str(i) for i in rest)
        )
    parts.append("")
    return "\n".join(parts)


def _gather_theme(subject: dict) -> str:
    """Build a 'set of updates' block from a KB subtree for a theme subject."""
    theme = subject.get("theme") or subject.get("slug") or "general"
    kb_prefix = subject.get("kb_prefix") or f"skills/{theme}"
    parts = [
        f"# Reflection Data — theme: {theme}",
        "",
        (
            "You are reflecting on a set of related updates rather than a "
            "single arc. Review the KB entries below as a group: what is the "
            "intent behind these changes, are they consistent, and what "
            "should be distilled or corrected?"
        ),
        "",
        f"- kb prefix: `{kb_prefix}`",
        "",
    ]
    try:
        from carpenter.kb import get_store
        store = get_store()
        children = store.list_children(kb_prefix)
        if not children:
            parts.append("## Updates\n- (none found under this prefix)")
        else:
            parts.append("## Updates")
            for child in children:
                if child.get("is_folder"):
                    continue
                parts.append(f"- {child.get('path')}: {child.get('description', '')}")
    except Exception:
        logger.exception("theme gather failed for prefix %s", kb_prefix)
        parts.append("## Updates\n- (error reading KB)")
    parts.append("")
    return "\n".join(parts)
