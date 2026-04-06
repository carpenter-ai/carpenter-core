"""DB-backed CRUD for security policies.

All mutations go through this module to keep the DB and in-memory
singleton in sync. A version counter is incremented on every change
(used by verified-flow-analysis to invalidate cached code hashes).
"""

import json
import logging

from ..db import get_db, db_connection, db_transaction
from .policies import get_policies, reload_policies, POLICY_TYPES, _validate_policy_type

logger = logging.getLogger(__name__)


def add_to_allowlist(policy_type: str, value: str) -> int:
    """Add a value to a policy allowlist. Returns the row ID.

    Raises ValueError for unknown policy types. Silently ignores duplicates.
    """
    _validate_policy_type(policy_type)
    with db_transaction() as db:
        cursor = db.execute(
            "INSERT OR IGNORE INTO security_policies (policy_type, value) VALUES (?, ?)",
            (policy_type, value),
        )
        if cursor.rowcount > 0:
            _increment_version(db)
        row_id = cursor.lastrowid

        # Update in-memory singleton
        policies = get_policies()
        policies.add(policy_type, value)

        return row_id


def remove_from_allowlist(policy_type: str, value: str) -> bool:
    """Remove a value from a policy allowlist. Returns True if it existed."""
    _validate_policy_type(policy_type)
    with db_transaction() as db:
        cursor = db.execute(
            "DELETE FROM security_policies WHERE policy_type = ? AND value = ?",
            (policy_type, value),
        )
        if cursor.rowcount > 0:
            _increment_version(db)
        removed = cursor.rowcount > 0

        # Update in-memory singleton
        policies = get_policies()
        policies.remove(policy_type, value)

        return removed


def get_allowlist(policy_type: str) -> list[str]:
    """Return all values in a policy allowlist from the database."""
    _validate_policy_type(policy_type)
    with db_connection() as db:
        rows = db.execute(
            "SELECT value FROM security_policies WHERE policy_type = ? ORDER BY value",
            (policy_type,),
        ).fetchall()
        return [row["value"] for row in rows]


def get_all_policies() -> dict[str, list[str]]:
    """Return all policies grouped by type."""
    with db_connection() as db:
        rows = db.execute(
            "SELECT policy_type, value FROM security_policies ORDER BY policy_type, value"
        ).fetchall()
        result: dict[str, list[str]] = {pt: [] for pt in sorted(POLICY_TYPES)}
        for row in rows:
            pt = row["policy_type"]
            if pt in result:
                result[pt].append(row["value"])
        return result


def get_policy_version() -> int:
    """Return the current policy version counter."""
    with db_connection() as db:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = 0 AND key = '_policy_version'"
        ).fetchone()
        if row is None:
            return 0
        return json.loads(row["value_json"])


def clear_allowlist(policy_type: str) -> int:
    """Remove all values from a policy allowlist. Returns count removed."""
    _validate_policy_type(policy_type)
    with db_transaction() as db:
        cursor = db.execute(
            "DELETE FROM security_policies WHERE policy_type = ?",
            (policy_type,),
        )
        if cursor.rowcount > 0:
            _increment_version(db)

        # Update in-memory singleton
        policies = get_policies()
        policies.clear(policy_type)

        return cursor.rowcount


def _increment_version(db) -> int:
    """Increment the policy version counter. Returns new version.

    Uses the sentinel arc (id=0) in arc_state to store the version counter.
    """
    row = db.execute(
        "SELECT value_json FROM arc_state WHERE arc_id = 0 AND key = '_policy_version'"
    ).fetchone()
    if row is None:
        new_version = 1
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (0, '_policy_version', ?)",
            (json.dumps(new_version),),
        )
    else:
        new_version = json.loads(row["value_json"]) + 1
        db.execute(
            "UPDATE arc_state SET value_json = ? WHERE arc_id = 0 AND key = '_policy_version'",
            (json.dumps(new_version),),
        )
    return new_version
