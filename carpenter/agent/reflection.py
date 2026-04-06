"""Reflective meta-cognition: cadenced self-reflection for Carpenter.

Daily, weekly, and monthly reflections that consume activity data
and propose knowledge/workflow improvements. Forms a compression chain:
raw messages -> summaries -> daily notes -> weekly patterns -> monthly insights.

Reflections are stored in the reflections table. Execution is handled by
the template-based reflection handler in core/workflows/reflection_template_handler.py.

This module provides data-gathering functions (gather_daily_data, etc.),
threshold checking (should_reflect), and storage (save_reflection, get_reflections).
"""

import logging
import sqlite3
from datetime import datetime, timezone, timedelta

from .. import config
from ..db import get_db, db_connection, db_transaction
from ..prompts import load_prompt_template

logger = logging.getLogger(__name__)

# Maximum characters when truncating daily reflection content for weekly summaries
REFLECTION_SUMMARY_MAX_CHARS = 500


def should_reflect(cadence: str) -> bool:
    """Check if enough activity occurred to justify a reflection API call.

    Args:
        cadence: 'daily', 'weekly', or 'monthly'.

    Returns:
        True if activity exceeds the configured threshold.
    """
    reflection_config = config.CONFIG.get("reflection", {})
    min_convs = reflection_config.get("min_daily_conversations", 1)

    if cadence == "daily":
        days = 1
    elif cadence == "weekly":
        days = 7
    elif cadence == "monthly":
        days = 30
    else:
        return False

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db_connection() as db:
        row = db.execute(
            "SELECT COUNT(*) as cnt FROM conversations "
            "WHERE started_at >= ? AND archived = FALSE",
            (since,),
        ).fetchone()
        count = row["cnt"] if row else 0

    threshold = min_convs * (days if cadence != "daily" else 1)
    return count >= threshold


def _gather_period_stats(days: int) -> str:
    """Gather aggregated stats for a period.

    Returns markdown-formatted stats: conversation count, arc success/fail rate,
    top tools used, total tokens, cache hit rate.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db_connection() as db:
        # Conversation count
        conv_row = db.execute(
            "SELECT COUNT(*) as cnt FROM conversations WHERE started_at >= ?",
            (since,),
        ).fetchone()
        conv_count = conv_row["cnt"] if conv_row else 0

        # Arc success/fail
        arc_rows = db.execute(
            "SELECT status, COUNT(*) as cnt FROM arcs "
            "WHERE created_at >= ? AND status IN ('completed', 'failed') "
            "GROUP BY status",
            (since,),
        ).fetchall()
        arc_stats = {r["status"]: r["cnt"] for r in arc_rows}

        # Top tools used
        tool_rows = db.execute(
            "SELECT tool_name, COUNT(*) as cnt, AVG(duration_ms) as avg_ms "
            "FROM tool_calls WHERE created_at >= ? "
            "GROUP BY tool_name ORDER BY cnt DESC LIMIT 10",
            (since,),
        ).fetchall()

        # Token totals
        token_row = db.execute(
            "SELECT SUM(input_tokens) as total_in, SUM(output_tokens) as total_out, "
            "SUM(cache_read_input_tokens) as cache_read, "
            "SUM(cache_creation_input_tokens) as cache_create "
            "FROM api_calls WHERE created_at >= ?",
            (since,),
        ).fetchone()

        # Work queue errors
        error_row = db.execute(
            "SELECT COUNT(*) as cnt FROM work_queue "
            "WHERE created_at >= ? AND status = 'failed'",
            (since,),
        ).fetchone()

    lines = [f"### Period Stats (last {days} days)"]
    lines.append(f"- Conversations: {conv_count}")
    completed = arc_stats.get("completed", 0)
    failed = arc_stats.get("failed", 0)
    total_arcs = completed + failed
    if total_arcs > 0:
        lines.append(f"- Arcs: {completed} completed, {failed} failed ({completed/total_arcs*100:.0f}% success)")
    else:
        lines.append("- Arcs: none completed/failed")

    if tool_rows:
        lines.append("- Top tools:")
        for t in tool_rows:
            avg = f" avg={int(t['avg_ms'])}ms" if t["avg_ms"] is not None else ""
            lines.append(f"  - {t['tool_name']}: {t['cnt']} calls{avg}")

    if token_row and token_row["total_in"]:
        total_in = token_row["total_in"] or 0
        total_out = token_row["total_out"] or 0
        cache_read = token_row["cache_read"] or 0
        cache_create = token_row["cache_create"] or 0
        full_price = total_in + cache_create + cache_read
        hit_rate = (cache_read / full_price * 100) if full_price > 0 else 0
        lines.append(f"- Tokens: {total_in + total_out:,} total (in={total_in:,}, out={total_out:,})")
        lines.append(f"- Cache: {hit_rate:.1f}% hit rate")

    error_count = error_row["cnt"] if error_row else 0
    if error_count:
        lines.append(f"- Work queue errors: {error_count}")

    # Skill KB access stats
    try:
        skill_kb_rows = db.execute(
            "SELECT path, COUNT(*) as cnt FROM kb_access_log "
            "WHERE path LIKE 'skills/%' AND accessed_at >= ? "
            "GROUP BY path ORDER BY cnt DESC LIMIT 10",
            (since,),
        ).fetchall()
        if skill_kb_rows:
            lines.append("- Skill knowledge usage:")
            for sr in skill_kb_rows:
                lines.append(f"  - {sr['path']}: {sr['cnt']} accesses")
    except (sqlite3.Error, KeyError) as _exc:
        pass  # kb_access_log table may not exist in older DBs

    # KB health metrics
    try:
        from ..kb import get_store
        from ..kb.health import graph_metrics
        metrics = graph_metrics(get_store())
        lines.append("- KB health:")
        lines.append(f"  - Entries: {metrics['total_entries']}, Links: {metrics['total_links']}")
        if metrics["broken_links"]:
            lines.append(f"  - Broken links ({len(metrics['broken_links'])}):")
            for bl in metrics["broken_links"][:10]:
                lines.append(f"    - {bl}")
            if len(metrics["broken_links"]) > 10:
                lines.append(f"    - ... and {len(metrics['broken_links']) - 10} more")
        if metrics["orphan_entries"]:
            lines.append(f"  - Orphan entries ({len(metrics['orphan_entries'])}): "
                          + ", ".join(metrics["orphan_entries"][:5]))
        if metrics["unreachable_entries"]:
            lines.append(f"  - Unreachable ({len(metrics['unreachable_entries'])}): "
                          + ", ".join(metrics["unreachable_entries"][:5]))
    except (ImportError, OSError, KeyError) as _exc:
        pass  # KB may not be initialized

    return "\n".join(lines)


def gather_daily_data() -> str:
    """Gather data for daily reflection: last 24h conversations, arcs, tools, errors."""
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with db_connection() as db:
        # Conversations with titles and summaries
        conv_rows = db.execute(
            "SELECT id, title, summary, context_tokens FROM conversations "
            "WHERE started_at >= ? ORDER BY id DESC",
            (since,),
        ).fetchall()

        # Arc completions/failures
        arc_rows = db.execute(
            "SELECT id, name, status, goal FROM arcs "
            "WHERE updated_at >= ? AND status IN ('completed', 'failed') "
            "ORDER BY id DESC LIMIT 20",
            (since,),
        ).fetchall()

    try:
        instruction = load_prompt_template("daily", subdirectory="reflections")
    except FileNotFoundError:
        instruction = (
            "You are reviewing the past 24 hours of platform activity. "
            "Analyze patterns, note what went well and what didn't, and identify "
            "any recurring issues or opportunities for improvement. If you see "
            "patterns worth preserving, use submit_code with carpenter_tools.act.kb.add() "
            "to create a KB entry under skills/. "
            "Keep your reflection concise and actionable."
        )

    parts = [
        "# Daily Reflection Data",
        "",
        instruction,
        "",
    ]

    # Conversations
    parts.append("## Conversations")
    if conv_rows:
        for c in conv_rows:
            title = c["title"] or "(untitled)"
            tokens = c["context_tokens"] or 0
            parts.append(f"- conv#{c['id']}: {title} ({tokens} tokens)")
            if c["summary"]:
                # Include first 300 chars of summary
                parts.append(f"  Summary: {c['summary'][:300]}")
    else:
        parts.append("- No conversations in the last 24 hours.")

    # Arcs
    parts.append("\n## Arc Activity")
    if arc_rows:
        for a in arc_rows:
            goal = (a["goal"] or "")[:100]
            parts.append(f"- #{a['id']} [{a['status']}] {a['name']}: {goal}")
    else:
        parts.append("- No arc completions/failures.")

    # Stats
    parts.append("")
    parts.append(_gather_period_stats(1))

    return "\n".join(parts)


def gather_weekly_data() -> str:
    """Gather data for weekly reflection: daily reflections + 7-day stats."""
    try:
        instruction = load_prompt_template("weekly", subdirectory="reflections")
    except FileNotFoundError:
        instruction = (
            "You are reviewing the past week of platform activity. "
            "Look for patterns across daily reflections, identify recurring themes, "
            "and propose actionable improvements. Consider whether existing knowledge entries "
            "need updating or new entries should be created under skills/. "
            "Keep your reflection focused on trends and patterns."
        )

    parts = [
        "# Weekly Reflection Data",
        "",
        instruction,
        "",
    ]

    # Include daily reflections from the past week
    daily_reflections = get_reflections("daily", limit=7)
    parts.append("## Daily Reflections This Week")
    if daily_reflections:
        for r in daily_reflections:
            parts.append(f"\n### {r['period_start']} to {r['period_end']}")
            content = r["content"][:REFLECTION_SUMMARY_MAX_CHARS]
            parts.append(content)
    else:
        parts.append("- No daily reflections available.")

    parts.append("")
    parts.append(_gather_period_stats(7))

    return "\n".join(parts)


def gather_monthly_data() -> str:
    """Gather data for monthly reflection: weekly reflections + 30-day stats + skill knowledge."""
    try:
        instruction = load_prompt_template("monthly", subdirectory="reflections")
    except FileNotFoundError:
        instruction = (
            "You are reviewing the past month of platform activity. "
            "This is a strategic review. Identify long-term patterns, evaluate "
            "the effectiveness of existing knowledge entries and workflows, and propose "
            "significant improvements. Consider skill knowledge maintenance. "
            "Keep your reflection strategic and forward-looking."
        )

    parts = [
        "# Monthly Reflection Data",
        "",
        instruction,
        "",
    ]

    # Include weekly reflections from the past month
    weekly_reflections = get_reflections("weekly", limit=4)
    parts.append("## Weekly Reflections This Month")
    if weekly_reflections:
        for r in weekly_reflections:
            parts.append(f"\n### {r['period_start']} to {r['period_end']}")
            content = r["content"][:800]
            parts.append(content)
    else:
        parts.append("- No weekly reflections available.")

    # Skill knowledge entries from KB
    try:
        from ..kb import get_store
        store = get_store()
        skill_entries = store.list_children("skills")
        if skill_entries:
            parts.append("\n## Current Skill Knowledge Entries")
            for entry in skill_entries:
                parts.append(f"- {entry['path']}: {entry.get('description', '')}")
    except (ImportError, OSError, KeyError) as _exc:
        pass

    parts.append("")
    parts.append(_gather_period_stats(30))

    return "\n".join(parts)


def save_reflection(
    cadence: str,
    period_start: str,
    period_end: str,
    content: str,
    proposed_actions: str | None = None,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> int:
    """Save a reflection entry to the database.

    Returns the reflection ID.
    """
    with db_transaction() as db:
        cursor = db.execute(
            "INSERT INTO reflections "
            "(cadence, period_start, period_end, content, proposed_actions, "
            " model, input_tokens, output_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cadence, period_start, period_end, content, proposed_actions,
             model, input_tokens, output_tokens),
        )
        reflection_id = cursor.lastrowid

    # Enqueue KB entry creation (outside transaction to avoid nested writes)
    try:
        from ..core.engine import work_queue
        work_queue.enqueue(
            "kb.reflection_summary",
            {"reflection_id": reflection_id},
            idempotency_key=f"refl-kb-{reflection_id}",
        )
    except (ImportError, sqlite3.Error) as _exc:
        pass  # Best-effort
    return reflection_id


def get_reflections(cadence: str, limit: int = 5) -> list[dict]:
    """Get recent reflections for a cadence, ordered by period_end DESC.

    Args:
        cadence: 'daily', 'weekly', or 'monthly'.
        limit: Max results.

    Returns:
        List of reflection dicts.
    """
    with db_connection() as db:
        rows = db.execute(
            "SELECT * FROM reflections WHERE cadence = ? "
            "ORDER BY period_end DESC LIMIT ?",
            (cadence, limit),
        ).fetchall()
        return [dict(r) for r in rows]
