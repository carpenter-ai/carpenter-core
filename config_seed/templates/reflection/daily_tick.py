"""Daily-cadence reflection batching — replaces the per-arc trigger.

A daily cron emits ``reflection.daily_tick``; :func:`handle_reflection_tick`
collects the root arcs that completed since the last tick (excluding the
reflection pipeline's own meta-templates so reflections never reflect on
reflections), splits them into batches of at most ``reflection.batch_size``,
and creates one ``period`` reflection arc per batch.

Being cadence-bounded (once/day, bounded batches) and never fired by an arc
completion, this cannot form the feedback loop the per-arc trigger could.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Watermark of the last processed tick, stored on the sentinel arc (id 0).
WATERMARK_KEY = "reflection_last_tick"

# Reflection-pipeline meta-templates whose completions must never seed a new
# reflection (the recursion/loop guard, now enforced at selection time).
EXCLUDED_TEMPLATES = ("reflection", "skill-kb-review")


def _eligible_root_arcs(db, since_iso: str, until_iso: str) -> list[int]:
    rows = db.execute(
        "SELECT a.id FROM arcs a "
        "LEFT JOIN workflow_templates t ON a.template_id = t.id "
        "WHERE a.parent_id IS NULL "
        "  AND a.status = 'completed' "
        "  AND a.id != 0 "
        "  AND a.updated_at > ? AND a.updated_at <= ? "
        "  AND (t.name IS NULL OR t.name NOT IN (?, ?)) "
        "ORDER BY a.updated_at ASC",
        (since_iso, until_iso, *EXCLUDED_TEMPLATES),
    ).fetchall()
    return [r["id"] for r in rows]


async def handle_reflection_tick(work_id: int, payload: dict) -> None:
    """Create batched ``period`` reflections for arcs completed since last tick."""
    from carpenter import config
    from carpenter.core.arcs import manager as arc_manager
    from carpenter.core.engine import template_manager
    from carpenter.core.workflows._arc_state import get_arc_state, set_arc_state
    from carpenter.db import db_connection

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    last = get_arc_state(0, WATERMARK_KEY)
    if not last:
        # First run: look back one day so we don't reflect on all of history.
        last = (now - timedelta(days=1)).isoformat()

    with db_connection() as db:
        arc_ids = _eligible_root_arcs(db, last, now_iso)

    if not arc_ids:
        set_arc_state(0, WATERMARK_KEY, now_iso)
        logger.info("reflection daily tick: no eligible arcs since %s", last)
        return

    reflection_tmpl = template_manager.get_template_by_name("reflection")
    if reflection_tmpl is None:
        logger.error("reflection daily tick: 'reflection' template not found")
        return

    batch_size = int(config.CONFIG.get("reflection", {}).get("batch_size", 20))
    date_str = now_iso[:10]
    n_batches = (len(arc_ids) + batch_size - 1) // batch_size
    created = 0

    for idx in range(n_batches):
        batch = arc_ids[idx * batch_size:(idx + 1) * batch_size]
        # When a single day needs multiple batches, disambiguate the KB key
        # so they don't collide on reflections/by-day/{date}.
        date_key = date_str if n_batches == 1 else f"{date_str}-{idx + 1}"
        subject = {
            "kind": "period",
            "refs": batch,
            "window": {"from": last, "to": now_iso, "date": date_key},
        }
        arc_id = arc_manager.create_arc(
            name="reflection",
            template_id=reflection_tmpl["id"],
            origin_kind="reflection",
            origin_ref=json.dumps({"cadence": "daily", "batch": idx + 1}),
            priority=1000,
            agent_type="SUPERVISOR",
        )
        set_arc_state(arc_id, "reflection_subject", subject)
        template_manager.instantiate_template(reflection_tmpl["id"], arc_id)
        created += 1

    set_arc_state(0, WATERMARK_KEY, now_iso)
    logger.info(
        "reflection daily tick: created %d batch(es) over %d arc(s) since %s",
        created, len(arc_ids), last,
    )
