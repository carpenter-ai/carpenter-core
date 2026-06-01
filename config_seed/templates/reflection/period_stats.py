"""Aggregated period stats used as context around a reflection.

Small markdown block summarising conversations / arc success rate /
top tools / token usage / cache hit rate over the last N days. Used
by :mod:`activity_gatherer` as the framing block sitting next to a
single reflected arc's recap.

Feature-specific; lives inside the reflection template package, not
on the carpenter platform.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from carpenter.db import db_connection


def gather_period_stats(days: int) -> str:
    """Return a markdown block of aggregate stats for the last ``days``."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db_connection() as db:
        conv_row = db.execute(
            "SELECT COUNT(*) as cnt FROM conversations WHERE started_at >= ?",
            (since,),
        ).fetchone()
        conv_count = conv_row["cnt"] if conv_row else 0

        arc_rows = db.execute(
            "SELECT status, COUNT(*) as cnt FROM arcs "
            "WHERE created_at >= ? AND status IN ('completed', 'failed') "
            "GROUP BY status",
            (since,),
        ).fetchall()
        arc_stats = {r["status"]: r["cnt"] for r in arc_rows}

        tool_rows = db.execute(
            "SELECT tool_name, COUNT(*) as cnt, AVG(duration_ms) as avg_ms "
            "FROM tool_calls WHERE created_at >= ? "
            "GROUP BY tool_name ORDER BY cnt DESC LIMIT 10",
            (since,),
        ).fetchall()

        token_row = db.execute(
            "SELECT SUM(input_tokens) as total_in, SUM(output_tokens) as total_out, "
            "SUM(cache_read_input_tokens) as cache_read, "
            "SUM(cache_creation_input_tokens) as cache_create "
            "FROM api_calls WHERE created_at >= ?",
            (since,),
        ).fetchone()

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
        lines.append(
            f"- Arcs: {completed} completed, {failed} failed "
            f"({completed / total_arcs * 100:.0f}% success)"
        )
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
        lines.append(
            f"- Tokens: {total_in + total_out:,} total "
            f"(in={total_in:,}, out={total_out:,})"
        )
        lines.append(f"- Cache: {hit_rate:.1f}% hit rate")

    error_count = error_row["cnt"] if error_row else 0
    if error_count:
        lines.append(f"- Work queue errors: {error_count}")

    return "\n".join(lines)
