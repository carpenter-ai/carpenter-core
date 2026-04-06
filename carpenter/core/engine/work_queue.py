"""Work queue for Carpenter.

Central dispatch mechanism. Work items represent units of work to be processed.
State machine: pending -> claimed -> complete | failed | dead_letter

Key invariants:
- Claiming is atomic (UPDATE WHERE status='pending')
- Idempotency keys prevent duplicate work items
- Failed items retry with exponential backoff up to max_retries
- Items exceeding max_retries go to dead_letter
"""

import json
import logging
from datetime import datetime, timezone

from ...db import get_db, db_connection, db_transaction

logger = logging.getLogger(__name__)

# Use as max_retries when work queue retries should be disabled (arc_retry handles retries)
SINGLE_ATTEMPT = 1


def enqueue(
    event_type: str,
    payload: dict,
    idempotency_key: str | None = None,
    max_retries: int = 3,
    scheduled_at: str | None = None,
) -> int | None:
    """Add a work item to the queue.

    Args:
        event_type: Type of work to perform
        payload: Work payload (will be JSON-encoded)
        idempotency_key: Optional key to prevent duplicate work.
            Items with the same key that are ``pending`` or ``claimed``
            block the insert (deduplication).  Completed or dead-lettered
            items with the same key are removed first so the work can be
            re-enqueued (e.g. arc re-dispatch after server restart).
        max_retries: Maximum retry attempts for this work item
        scheduled_at: Optional ISO8601 timestamp for delayed execution

    Returns the work item ID, or None if idempotency_key already exists
    in a pending/claimed state.
    """
    with db_transaction() as db:
        # Clear finished items with the same idempotency key so re-enqueue
        # is possible.  Without this, arcs that were dispatched, completed
        # their work item, but remained in 'pending' status (e.g. due to
        # server restart) could never be re-dispatched.
        if idempotency_key is not None:
            db.execute(
                "DELETE FROM work_queue "
                "WHERE idempotency_key = ? "
                "AND status IN ('complete', 'dead_letter')",
                (idempotency_key,),
            )

        cursor = db.execute(
            "INSERT OR IGNORE INTO work_queue "
            "(event_type, payload_json, idempotency_key, max_retries, scheduled_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_type, json.dumps(payload), idempotency_key, max_retries, scheduled_at),
        )
        if cursor.rowcount == 0:
            db.commit()  # Commit the DELETE even if INSERT was ignored
            return None
        work_id = cursor.lastrowid
        return work_id


def claim() -> dict | None:
    """Atomically claim the next pending work item that is ready to execute.

    Only claims items that are past their scheduled_at time (or have no schedule).

    Returns the work item as a dict, or None if queue is empty.
    """
    with db_transaction() as db:
        # Only claim items that are ready (scheduled_at is NULL or in the past)
        row = db.execute(
            "SELECT id FROM work_queue "
            "WHERE status = 'pending' "
            "AND (scheduled_at IS NULL OR datetime(scheduled_at) <= datetime('now')) "
            "ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None

        cursor = db.execute(
            "UPDATE work_queue SET status = 'claimed', "
            "claimed_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'pending'",
            (row["id"],),
        )

        # CAS check: if rowcount is 0, another thread already claimed this item
        if cursor.rowcount == 0:
            db.commit()
            return None


        item = db.execute(
            "SELECT * FROM work_queue WHERE id = ?", (row["id"],)
        ).fetchone()
        return dict(item) if item else None


def complete(work_id: int):
    """Mark a work item as successfully completed."""
    with db_transaction() as db:
        db.execute(
            "UPDATE work_queue SET status = 'complete', "
            "completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (work_id,),
        )


def fail(work_id: int, error: str):
    """Mark a work item as failed. Retries if under max_retries, otherwise dead_letter."""
    with db_transaction() as db:
        item = db.execute(
            "SELECT retry_count, max_retries FROM work_queue WHERE id = ?",
            (work_id,),
        ).fetchone()
        if not item:
            return

        if item["retry_count"] + 1 >= item["max_retries"]:
            db.execute(
                "UPDATE work_queue SET status = 'dead_letter', "
                "error = ?, completed_at = CURRENT_TIMESTAMP, "
                "retry_count = retry_count + 1 WHERE id = ?",
                (error, work_id),
            )
        else:
            db.execute(
                "UPDATE work_queue SET status = 'pending', "
                "error = ?, retry_count = retry_count + 1, "
                "claimed_at = NULL WHERE id = ?",
                (error, work_id),
            )


def get_item(work_id: int) -> dict | None:
    """Get a work item by ID."""
    with db_connection() as db:
        row = db.execute(
            "SELECT * FROM work_queue WHERE id = ?", (work_id,)
        ).fetchone()
        return dict(row) if row else None


def get_dead_letter_items() -> list[dict]:
    """Get all items in the dead letter queue."""
    with db_connection() as db:
        rows = db.execute(
            "SELECT * FROM work_queue WHERE status = 'dead_letter' "
            "ORDER BY completed_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]
