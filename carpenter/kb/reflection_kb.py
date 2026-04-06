"""Reflection entries → KB entries.

When a reflection is saved, writes it as a KB entry under
reflections/{cadence}/{period_end_date}. Backfill function
creates entries for all existing reflections.
"""

import logging

from ..db import get_db, db_connection, db_transaction

logger = logging.getLogger(__name__)


def create_reflection_entry(reflection_id: int, store) -> str | None:
    """Read reflection from DB, write KB entry at reflections/{cadence}/{period_end_date}.

    Args:
        reflection_id: The reflection ID.
        store: KBStore instance.

    Returns:
        KB path if created, None otherwise.
    """
    with db_connection() as db:
        row = db.execute(
            "SELECT id, cadence, period_start, period_end, content, "
            "proposed_actions, model FROM reflections WHERE id = ?",
            (reflection_id,),
        ).fetchone()

    if row is None:
        return None

    content_text = row["content"]
    if not content_text:
        return None

    cadence = row["cadence"]
    period_end = row["period_end"] or "unknown"
    # Use date portion only for path
    date_str = period_end[:10] if len(period_end) >= 10 else period_end
    kb_path = f"reflections/{cadence}/{date_str}"

    title = f"{cadence.title()} Reflection — {date_str}"
    parts = [
        f"# {title}",
        f"",
        f"**Period**: {row['period_start']} to {row['period_end']}",
        f"**Model**: {row['model'] or 'unknown'}",
        f"",
        content_text,
    ]
    if row["proposed_actions"]:
        parts.append(f"\n## Proposed Actions\n\n{row['proposed_actions']}")
    parts.append("")

    content = "\n".join(parts)
    description = content_text.split(".")[0].strip() + "." if "." in content_text else content_text[:100]

    store.write_entry(
        path=kb_path,
        content=content,
        description=description,
        entry_type="reflection",
        validate_links=False,
    )

    logger.info("Created reflection KB entry: %s", kb_path)
    return kb_path


_HWM_KEY = "_kb_backfill_refl_max_id"


def backfill_reflections(store) -> int:
    """Backfill KB entries for reflections with non-empty content.

    Uses a high-water mark (stored in arc_state on sentinel arc 0) to
    only process reflections added since the last backfill. On first
    run the mark is 0, so all existing reflections are processed.

    Returns count of entries created.
    """
    with db_connection() as db:
        # Read high-water mark
        hwm_row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = 0 AND key = ?",
            (_HWM_KEY,),
        ).fetchone()
        hwm = int(hwm_row["value_json"]) if hwm_row else 0

        rows = db.execute(
            "SELECT id FROM reflections "
            "WHERE id > ? AND content IS NOT NULL AND content != '' "
            "ORDER BY id",
            (hwm,),
        ).fetchall()

    if not rows:
        return 0

    count = 0
    max_id = hwm
    for row in rows:
        result = create_reflection_entry(row["id"], store)
        if result:
            count += 1
        max_id = max(max_id, row["id"])

    # Update high-water mark
    if max_id > hwm:
        with db_transaction() as db:
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (0, ?, ?) "
                "ON CONFLICT(arc_id, key) DO UPDATE SET value_json = excluded.value_json",
                (_HWM_KEY, str(max_id)),
            )

    if count:
        logger.info("Backfilled %d reflection KB entries (hwm %d→%d)", count, hwm, max_id)
    return count
