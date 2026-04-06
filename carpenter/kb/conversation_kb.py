"""Conversation summaries → KB entries.

When a conversation summary is generated, writes it as a KB entry
under conversations/{id}-{sanitized_title}. Backfill function
creates entries for all existing conversations with summaries.
"""

import logging
import re

from ..db import get_db, db_connection, db_transaction

logger = logging.getLogger(__name__)


def _sanitize_title(title: str) -> str:
    """Convert title to filesystem-safe KB path segment (lowercase, hyphens, max 50 chars)."""
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:50] if s else "untitled"


def create_conversation_entry(conversation_id: int, store) -> str | None:
    """Read conversation from DB, write KB entry at conversations/{id}-{sanitized_title}.

    Args:
        conversation_id: The conversation ID.
        store: KBStore instance.

    Returns:
        KB path if created, None otherwise.
    """
    with db_connection() as db:
        row = db.execute(
            "SELECT id, title, summary, started_at FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()

    if row is None:
        return None

    summary = row["summary"]
    if not summary:
        return None

    title = row["title"] or "Untitled conversation"
    safe_title = _sanitize_title(title)
    kb_path = f"conversations/{conversation_id}-{safe_title}"

    date_str = (row["started_at"] or "")[:10]
    content = f"# {title}\n\n**Date**: {date_str}\n**Conversation ID**: {conversation_id}\n\n{summary}\n"
    description = summary.split(".")[0].strip() + "." if "." in summary else summary[:100]

    store.write_entry(
        path=kb_path,
        content=content,
        description=description,
        entry_type="conversation_summary",
        validate_links=False,
    )

    logger.info("Created conversation KB entry: %s", kb_path)
    return kb_path


_HWM_KEY = "_kb_backfill_conv_max_id"


def backfill_conversations(store) -> int:
    """Backfill KB entries for conversations with non-empty summaries.

    Uses a high-water mark (stored in arc_state on sentinel arc 0) to
    only process conversations added since the last backfill. On first
    run the mark is 0, so all existing conversations are processed.

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
            "SELECT id FROM conversations "
            "WHERE id > ? AND summary IS NOT NULL AND summary != '' "
            "ORDER BY id",
            (hwm,),
        ).fetchall()

    if not rows:
        return 0

    count = 0
    max_id = hwm
    for row in rows:
        result = create_conversation_entry(row["id"], store)
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
        logger.info("Backfilled %d conversation KB entries (hwm %d→%d)", count, hwm, max_id)
    return count
