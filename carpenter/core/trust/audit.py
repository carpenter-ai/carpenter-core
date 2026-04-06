"""Trust audit log for recording trust boundary decisions.

Separate from arc_history to avoid redundancy — this tracks security-relevant
events across the trust boundary system.
"""

import json
import logging

from ...db import get_db, db_connection, db_transaction

logger = logging.getLogger(__name__)


def log_trust_event(
    arc_id: int | None,
    event_type: str,
    details: dict | None = None,
) -> int:
    """Record a trust boundary event.

    Args:
        arc_id: The arc involved (None for system-wide events).
        event_type: Event category (e.g. 'taint_assigned', 'access_denied').
        details: Additional context as a dict.

    Returns:
        The trust_audit_log entry ID.
    """
    with db_transaction() as db:
        cursor = db.execute(
            "INSERT INTO trust_audit_log (arc_id, event_type, details_json) "
            "VALUES (?, ?, ?)",
            (arc_id, event_type, json.dumps(details) if details else None),
        )
        entry_id = cursor.lastrowid
        return entry_id


def get_trust_events(
    arc_id: int | None = None,
    event_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query trust audit log with optional filters.

    Args:
        arc_id: Filter by arc ID (None = all arcs).
        event_type: Filter by event type (None = all types).
        limit: Maximum results to return.

    Returns:
        List of event dicts ordered by created_at DESC.
    """
    with db_connection() as db:
        conditions = []
        params = []
        if arc_id is not None:
            conditions.append("arc_id = ?")
            params.append(arc_id)
        if event_type is not None:
            conditions.append("event_type = ?")
            params.append(event_type)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)

        rows = db.execute(
            f"SELECT * FROM trust_audit_log {where} "
            f"ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()

        result = []
        for row in rows:
            d = dict(row)
            if d.get("details_json"):
                try:
                    d["details"] = json.loads(d["details_json"])
                except (json.JSONDecodeError, TypeError):
                    d["details"] = None
            else:
                d["details"] = None
            result.append(d)
        return result
