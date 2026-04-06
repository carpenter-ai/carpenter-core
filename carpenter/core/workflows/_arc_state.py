"""Shared arc-state helpers for workflow handlers.

Provides ``get_arc_state()`` and ``set_arc_state()`` -- thin wrappers around
the ``arc_state`` table that serialise values as JSON.  Previously each
workflow handler defined its own identical copy; this module is the single
canonical implementation.
"""

import json

from ...db import get_db, db_connection, db_transaction


def get_arc_state(arc_id: int, key: str, default=None):
    """Get a value from arc_state."""
    with db_connection() as db:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = ?",
            (arc_id, key),
        ).fetchone()
        return json.loads(row["value_json"]) if row else default


def set_arc_state(arc_id: int, key: str, value):
    """Set a value in arc_state."""
    with db_transaction() as db:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?) "
            "ON CONFLICT(arc_id, key) DO UPDATE SET value_json = excluded.value_json, "
            "updated_at = CURRENT_TIMESTAMP",
            (arc_id, key, json.dumps(value)),
        )
