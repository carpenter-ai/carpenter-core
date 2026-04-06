"""Event bus for Carpenter.

Records events, evaluates matchers, and creates work items when matches occur.
Matchers are one-shot: they are deleted after matching.
Matchers can have timeouts: expired matchers generate timeout events.
"""

import json
import logging
from datetime import datetime, timezone

from ...db import get_db, db_connection, db_transaction
from ._utils import filter_matches

logger = logging.getLogger(__name__)


def record_event(
    event_type: str,
    payload: dict,
    source: str | None = None,
    priority: int = 0,
    idempotency_key: str | None = None,
) -> int | None:
    """Record an event. Returns the event ID, or None if duplicate.

    Args:
        event_type: Event type string.
        payload: Event payload dict (JSON-serializable).
        source: Optional source label for audit.
        priority: Event priority (higher = processed first). Default 0.
        idempotency_key: Optional unique key for dedup. If provided, uses
            INSERT OR IGNORE — duplicates are silently ignored and None
            is returned.
    """
    with db_transaction() as db:
        if idempotency_key is not None:
            cursor = db.execute(
                "INSERT OR IGNORE INTO events "
                "(event_type, payload_json, source, priority, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_type, json.dumps(payload), source, priority, idempotency_key),
            )
            if cursor.rowcount == 0:
                return None  # duplicate
        else:
            cursor = db.execute(
                "INSERT INTO events "
                "(event_type, payload_json, source, priority) "
                "VALUES (?, ?, ?, ?)",
                (event_type, json.dumps(payload), source, priority),
            )
        event_id = cursor.lastrowid
        return event_id


def register_matcher(
    event_type: str,
    arc_id: int | None = None,
    filter_json: dict | None = None,
    timeout_at: datetime | None = None,
) -> int:
    """Register a one-shot event matcher. Returns the matcher ID."""
    with db_transaction() as db:
        cursor = db.execute(
            "INSERT INTO event_matchers (arc_id, event_type, filter_json, timeout_at) "
            "VALUES (?, ?, ?, ?)",
            (
                arc_id,
                event_type,
                json.dumps(filter_json) if filter_json else None,
                timeout_at.isoformat() if timeout_at else None,
            ),
        )
        matcher_id = cursor.lastrowid
        return matcher_id


def _filter_matches(filter_json: str | None, payload: dict) -> bool:
    """Check if a matcher's filter matches an event payload.

    Deprecated: use ``filter_matches`` from ``._utils`` instead.
    Kept as a thin wrapper for backward compatibility.
    """
    return filter_matches(filter_json, payload)


def process_events() -> int:
    """Process unprocessed events against registered matchers.

    For each unprocessed event:
    1. Find all matchers for that event_type
    2. Check if the matcher's filter matches the event payload
    3. If match: create a work item and delete the matcher (one-shot)
    4. Mark the event as processed

    Returns the number of work items created.
    """
    db = get_db()
    work_items_created = 0
    try:
        events = db.execute(
            "SELECT id, event_type, payload_json FROM events "
            "WHERE processed = FALSE "
            "ORDER BY priority DESC, created_at ASC"
        ).fetchall()

        for event in events:
            payload = json.loads(event["payload_json"])
            matchers = db.execute(
                "SELECT id, arc_id, filter_json FROM event_matchers "
                "WHERE event_type = ?",
                (event["event_type"],),
            ).fetchall()

            for matcher in matchers:
                if filter_matches(matcher["filter_json"], payload):
                    # Create work item
                    work_payload = {
                        "event_id": event["id"],
                        "arc_id": matcher["arc_id"],
                        "event_type": event["event_type"],
                        "payload": payload,
                    }
                    db.execute(
                        "INSERT OR IGNORE INTO work_queue "
                        "(event_type, payload_json, idempotency_key) "
                        "VALUES (?, ?, ?)",
                        (
                            event["event_type"],
                            json.dumps(work_payload),
                            f"matcher-{matcher['id']}-event-{event['id']}",
                        ),
                    )
                    # Delete matcher (one-shot)
                    db.execute(
                        "DELETE FROM event_matchers WHERE id = ?",
                        (matcher["id"],),
                    )
                    work_items_created += 1

            # Mark event as processed
            db.execute(
                "UPDATE events SET processed = TRUE WHERE id = ?",
                (event["id"],),
            )

        db.commit()
        return work_items_created
    finally:
        db.close()


def check_timeouts() -> int:
    """Check for expired matchers and create timeout events.

    Expired matchers (timeout_at < now) generate a "matcher.timeout" event
    and are deleted.

    Returns the number of timeout events created.
    """
    db = get_db()
    timeouts = 0
    try:
        now = datetime.now(timezone.utc).isoformat()
        expired = db.execute(
            "SELECT id, arc_id, event_type FROM event_matchers "
            "WHERE timeout_at IS NOT NULL AND timeout_at < ?",
            (now,),
        ).fetchall()

        for matcher in expired:
            # Record timeout event
            db.execute(
                "INSERT INTO events (event_type, payload_json, source) "
                "VALUES (?, ?, ?)",
                (
                    "matcher.timeout",
                    json.dumps({
                        "matcher_id": matcher["id"],
                        "arc_id": matcher["arc_id"],
                        "original_event_type": matcher["event_type"],
                    }),
                    "system",
                ),
            )
            # Delete expired matcher
            db.execute(
                "DELETE FROM event_matchers WHERE id = ?",
                (matcher["id"],),
            )
            timeouts += 1

        db.commit()
        return timeouts
    finally:
        db.close()


def get_event(event_id: int) -> dict | None:
    """Get an event by ID."""
    with db_connection() as db:
        row = db.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        return dict(row) if row else None


def get_matchers(event_type: str | None = None) -> list[dict]:
    """Get all active matchers, optionally filtered by event_type."""
    with db_connection() as db:
        if event_type:
            rows = db.execute(
                "SELECT * FROM event_matchers WHERE event_type = ?",
                (event_type,),
            ).fetchall()
        else:
            rows = db.execute("SELECT * FROM event_matchers").fetchall()
        return [dict(row) for row in rows]
