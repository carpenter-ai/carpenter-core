"""Activity data for a reflection: one completed arc's trajectory.

In the cadence-era this module aggregated N days of conversations. The
per-arc trigger model replaced that: a reflection fires when a root arc
completes, and reflects on that specific arc — its goal, its outputs,
its children, its outcome. :func:`gather_from_arc` produces the markdown
block fed into the reflect step's goal, framed by a small period-stats
block (see :mod:`.period_stats`).

For the triage step (which decides whether reflect runs at all), a
lightweight parallel view is produced by :func:`triage_summary_from_subject`
— chat prompts, top-level agent responses, and arc-tree friction signals
only, no verbose trajectories. Cheap enough to feed haiku on every
batch.
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

# Triage-view character caps. Deliberately snug: triage should be able
# to spot obvious failure/loop/re-fetch patterns from prompts + top-level
# responses without carrying full trajectories.
_TRIAGE_USER_MAX_CHARS = 1500
_TRIAGE_AGENT_MAX_CHARS = 1500


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


# ── Triage view ──────────────────────────────────────────────────────
#
# The triage step is a cheap haiku call that decides whether the reflect
# step should run at all. It reads a lightweight per-arc view — user
# turns, top-level agent responses, and coarse friction signals — rather
# than the full trajectory dump the reflect step gets.


def _arc_conversation_snippets(arc_id: int) -> tuple[list[str], list[str]]:
    """Return (user_turns, assistant_turns) for arc_id's originating conv.

    An arc may be linked to zero, one, or many conversations
    (``conversation_arcs``). We take the earliest-linked conversation
    (usually the originating chat), read its ``messages`` in order, and
    split into user / assistant turns capped at
    ``_TRIAGE_USER_MAX_CHARS`` / ``_TRIAGE_AGENT_MAX_CHARS`` total per
    side. Trigger/background arcs with no conversation return two empty
    lists.
    """
    with db_connection() as db:
        row = db.execute(
            "SELECT conversation_id FROM conversation_arcs "
            "WHERE arc_id = ? ORDER BY created_at ASC LIMIT 1",
            (arc_id,),
        ).fetchone()
        if not row:
            return [], []
        conv_id = row["conversation_id"]
        msg_rows = db.execute(
            "SELECT role, content FROM messages "
            "WHERE conversation_id = ? ORDER BY id ASC",
            (conv_id,),
        ).fetchall()

    users: list[str] = []
    assistants: list[str] = []
    user_used = 0
    agent_used = 0
    for m in msg_rows:
        role = m["role"]
        content = m["content"] or ""
        if role == "user" and user_used < _TRIAGE_USER_MAX_CHARS:
            remaining = _TRIAGE_USER_MAX_CHARS - user_used
            snippet = content[:remaining]
            users.append(snippet)
            user_used += len(snippet)
        elif role == "assistant" and agent_used < _TRIAGE_AGENT_MAX_CHARS:
            remaining = _TRIAGE_AGENT_MAX_CHARS - agent_used
            snippet = content[:remaining]
            assistants.append(snippet)
            agent_used += len(snippet)
    return users, assistants


def _arc_friction_signals(arc_id: int) -> dict:
    """Return a small dict of arc-tree friction signals for triage.

    Signals actually collected today:
      - status: the arc's final status (completed / failed / etc.)
      - child_count / failed_child_count
      - retry_count: number of retry_attempts recorded on this arc

    Per-arc tool-call / KB-fetch counts are NOT collected — the
    coordinator's ``kb_access_log`` records per-conversation, and there
    is no per-arc tool-invocation counter today. Adding either is
    instrumentation work tracked in ``carpenter_reflection_v2.md``; the
    triage prompt (``triage-goal.md``) has been trimmed to reference
    only the signals actually available. This keeps the prompt honest
    rather than asking triage to decide against evidence it can't see.
    """
    signals: dict = {"status": "unknown"}
    with db_connection() as db:
        arc = db.execute(
            "SELECT status FROM arcs WHERE id = ?", (arc_id,),
        ).fetchone()
        if arc:
            signals["status"] = arc["status"]
        children = db.execute(
            "SELECT status FROM arcs WHERE parent_id = ?", (arc_id,),
        ).fetchall()
        signals["child_count"] = len(children)
        signals["failed_child_count"] = sum(
            1 for c in children if c["status"] == "failed"
        )
        try:
            retries = db.execute(
                "SELECT COUNT(*) AS n FROM retry_attempts WHERE arc_id = ?",
                (arc_id,),
            ).fetchone()
            signals["retry_count"] = int(retries["n"]) if retries else 0
        except Exception:  # noqa: BLE001 — table may not exist in older DBs
            signals["retry_count"] = 0
    return signals


def _triage_block_for_arc(arc_id: int) -> str:
    """Return a compact triage-summary markdown block for one arc."""
    arc = arc_manager.get_arc(arc_id)
    if not arc:
        return f"### Arc #{arc_id} — missing\n"
    users, assistants = _arc_conversation_snippets(arc_id)
    signals = _arc_friction_signals(arc_id)
    goal = (arc.get("goal") or "").strip()
    parts = [
        f"### Arc #{arc['id']} [{signals.get('status')}] {arc.get('name') or ''}",
    ]
    if goal:
        parts.append(f"- goal: {goal[:200]}")
    parts.append(
        "- signals: children={child_count} failed_children={failed_child_count} "
        "retries={retry_count}".format(**signals)
    )
    if users:
        parts.append("- user turns:")
        for u in users:
            for line in u.splitlines():
                parts.append(f"  > {line}")
    if assistants:
        parts.append("- assistant turns:")
        for a in assistants:
            for line in a.splitlines():
                parts.append(f"  > {line}")
    if not users and not assistants:
        parts.append("- (no originating conversation)")
    parts.append("")
    return "\n".join(parts)


def triage_summary_from_subject(subject: dict) -> str:
    """Return the lightweight triage view for a subject.

    Per root arc: originating chat prompts, top-level agent responses,
    and coarse arc-tree friction signals. Designed to be small enough to
    feed haiku on every batch — no full trajectories, no child-arc
    outputs.
    """
    from ._subject import subject_arc_ids

    arc_ids = subject_arc_ids(subject)
    if not arc_ids:
        return "# Triage Summary — no arcs in subject\n"
    window = subject.get("window") or {}
    parts = [
        "# Triage Summary",
        "",
        (
            "For each root arc: the user's prompt (if any), the agent's "
            "top-level response(s), and coarse arc-tree friction "
            "signals. Decide whether synthesis is warranted."
        ),
        "",
        f"- period: {window.get('from', '?')} → {window.get('to', '?')}",
        f"- arc count: {len(arc_ids)}",
        "",
    ]
    for aid in arc_ids:
        parts.append(_triage_block_for_arc(aid))
    return "\n".join(parts)
