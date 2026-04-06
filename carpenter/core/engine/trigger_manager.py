"""Trigger manager for Carpenter.

Manages cron-based triggers using croniter. Checks for due cron entries
and emits events into the event pipeline with idempotency keys to prevent
double-execution. Events are routed to work items via subscriptions.

Double-execution protection:
- idempotency_key = f"cron-{cron_id}-{fire_time_iso}"
- INSERT OR IGNORE on the UNIQUE idempotency_key column in the events table
- Even if check_cron() runs multiple times, only one event per fire time

Event flow:
  cron_entries (due) → events (timer.fired) → subscriptions → work_queue
"""

import json
import logging
from datetime import datetime, timezone

from croniter import croniter

from ...db import get_db, db_connection, db_transaction

logger = logging.getLogger(__name__)

# Event type emitted when a cron entry fires.
# Subscriptions match this to route to the appropriate work_queue handler.
TIMER_FIRED_EVENT = "timer.fired"


def add_cron(
    name: str,
    cron_expr: str,
    event_type: str,
    event_payload: dict | None = None,
) -> int:
    """Add a cron entry. Returns the cron entry ID.

    Validates the cron expression and calculates the first fire time.
    Raises ValueError if the cron expression is invalid.
    """
    if not croniter.is_valid(cron_expr):
        raise ValueError(f"Invalid cron expression: {cron_expr}")

    now = datetime.now(timezone.utc)
    cron = croniter(cron_expr, now)
    next_fire = cron.get_next(datetime)

    with db_transaction() as db:
        cursor = db.execute(
            "INSERT INTO cron_entries "
            "(name, cron_expr, event_type, event_payload_json, next_fire_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                name,
                cron_expr,
                event_type,
                json.dumps(event_payload) if event_payload else None,
                next_fire.isoformat(),
            ),
        )
        cron_id = cursor.lastrowid
        return cron_id


def _normalize_to_utc(dt: datetime) -> datetime:
    """Normalize a datetime to UTC.

    If the datetime is naive (no tzinfo), assume it represents local time
    and attach the system's local timezone before converting to UTC.
    If already timezone-aware, simply convert to UTC.
    """
    if dt.tzinfo is None:
        # Naive datetime — assume local time
        dt = dt.astimezone()  # attaches local tz
    return dt.astimezone(timezone.utc)


def add_once(
    name: str,
    at_iso: str,
    event_type: str,
    event_payload: dict | None = None,
) -> int:
    """Add a one-shot trigger that fires once at the given ISO timestamp.

    After firing, the entry is automatically deleted (not rescheduled).
    Returns the cron entry ID.

    If the timestamp is naive (no timezone), it is interpreted as local time
    and converted to UTC for consistent comparison in check_cron().

    Raises ValueError if at_iso is not a valid ISO datetime string.
    """
    try:
        dt = datetime.fromisoformat(at_iso)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid ISO datetime: {at_iso}") from exc

    # Normalize to UTC so check_cron() string comparison works correctly
    dt_utc = _normalize_to_utc(dt)

    # Use a sentinel cron_expr that is never evaluated for one-shot entries
    sentinel_expr = "0 0 31 2 *"

    with db_transaction() as db:
        cursor = db.execute(
            "INSERT INTO cron_entries "
            "(name, cron_expr, event_type, event_payload_json, next_fire_at, one_shot) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                name,
                sentinel_expr,
                event_type,
                json.dumps(event_payload) if event_payload else None,
                dt_utc.isoformat(),
                True,
            ),
        )
        cron_id = cursor.lastrowid
        return cron_id


def remove_cron(name: str) -> bool:
    """Remove a cron entry by name. Returns True if found and removed."""
    with db_transaction() as db:
        cursor = db.execute(
            "DELETE FROM cron_entries WHERE name = ?", (name,)
        )
        return cursor.rowcount > 0


def enable_cron(name: str, enabled: bool = True) -> bool:
    """Enable or disable a cron entry. Returns True if found."""
    with db_transaction() as db:
        cursor = db.execute(
            "UPDATE cron_entries SET enabled = ? WHERE name = ?",
            (enabled, name),
        )
        return cursor.rowcount > 0


def check_cron() -> int:
    """Check for due cron entries and emit events into the event pipeline.

    For each enabled cron entry whose next_fire_at <= now:
    1. Record an event (timer.fired) with idempotency key (prevents double-emission)
    2. Calculate and store the next fire time (recurring) or delete (one-shot)

    The emitted events carry the cron entry's target ``event_type`` in
    the payload so that subscriptions can route them to the correct
    work_queue handler (e.g., ``cron.message`` or ``arc.dispatch``).

    Returns the number of events emitted.
    """
    from . import event_bus

    db = get_db()
    events_emitted = 0
    try:
        now = datetime.now(timezone.utc)
        due_entries = db.execute(
            "SELECT * FROM cron_entries "
            "WHERE enabled = TRUE AND next_fire_at <= ?",
            (now.isoformat(),),
        ).fetchall()

        for entry in due_entries:
            fire_time = entry["next_fire_at"]
            idempotency_key = f"cron-{entry['id']}-{fire_time}"

            payload = {
                "cron_id": entry["id"],
                "cron_name": entry["name"],
                "cron_event_type": entry["event_type"],
                "fire_time": fire_time,
            }
            if entry["event_payload_json"]:
                payload["event_payload"] = json.loads(entry["event_payload_json"])

            # Record the event via the event bus (idempotent)
            cursor = db.execute(
                "INSERT OR IGNORE INTO events "
                "(event_type, payload_json, source, priority, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    TIMER_FIRED_EVENT,
                    json.dumps(payload),
                    f"cron:{entry['name']}",
                    0,
                    idempotency_key,
                ),
            )
            if cursor.rowcount > 0:
                events_emitted += 1

            # One-shot entries: delete after firing; recurring: compute next fire
            if entry["one_shot"]:
                db.execute(
                    "DELETE FROM cron_entries WHERE id = ?",
                    (entry["id"],),
                )
            else:
                # Calculate next fire time
                cron = croniter(entry["cron_expr"], now)
                next_fire = cron.get_next(datetime)
                db.execute(
                    "UPDATE cron_entries SET next_fire_at = ? WHERE id = ?",
                    (next_fire.isoformat(), entry["id"]),
                )

        db.commit()
        return events_emitted
    finally:
        db.close()


def get_cron(name: str) -> dict | None:
    """Get a cron entry by name."""
    with db_connection() as db:
        row = db.execute(
            "SELECT * FROM cron_entries WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None


def list_cron() -> list[dict]:
    """List all cron entries."""
    with db_connection() as db:
        rows = db.execute(
            "SELECT * FROM cron_entries ORDER BY name"
        ).fetchall()
        return [dict(row) for row in rows]
