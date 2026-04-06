"""Hash registry and trust store for verified flow analysis.

Verified code is hashed (SHA-256) and stored. On subsequent submissions,
the hash is checked before re-running verification. Hashes become stale
when security policies change (policy_version increments).
"""

from __future__ import annotations

import hashlib
import json
import logging

from ..db import get_db, db_connection, db_transaction

logger = logging.getLogger(__name__)


def compute_code_hash(code: str) -> str:
    """Compute SHA-256 hash of code."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def check_verified_hash(code_hash: str) -> dict | None:
    """Check if a code hash is verified and current.

    Returns the stored entry dict if found AND policy_version matches
    the current version. Returns None if stale or missing.
    """
    from ..security.policy_store import get_policy_version

    with db_connection() as db:
        row = db.execute(
            "SELECT code_hash, input_schemas_json, policy_version, verified_at "
            "FROM verified_code_hashes WHERE code_hash = ?",
            (code_hash,),
        ).fetchone()

        if row is None:
            return None

        current_version = get_policy_version()
        if row["policy_version"] != current_version:
            logger.debug(
                "Verified hash %s is stale (stored v%d, current v%d)",
                code_hash[:12], row["policy_version"], current_version,
            )
            return None

        return {
            "code_hash": row["code_hash"],
            "input_schemas_json": row["input_schemas_json"],
            "policy_version": row["policy_version"],
            "verified_at": row["verified_at"],
        }


def add_verified_hash(
    code_hash: str,
    input_schemas_json: str = "[]",
    policy_version: int | None = None,
) -> None:
    """Store a verified code hash.

    If policy_version is None, uses the current policy version.
    """
    if policy_version is None:
        from ..security.policy_store import get_policy_version
        policy_version = get_policy_version()

    with db_transaction() as db:
        db.execute(
            "INSERT OR REPLACE INTO verified_code_hashes "
            "(code_hash, input_schemas_json, policy_version) VALUES (?, ?, ?)",
            (code_hash, input_schemas_json, policy_version),
        )
        logger.debug("Stored verified hash %s (policy v%d)", code_hash[:12], policy_version)


def invalidate_stale_hashes() -> int:
    """Delete all verified hashes with policy_version < current.

    Returns the number of hashes invalidated.
    """
    from ..security.policy_store import get_policy_version

    current_version = get_policy_version()
    with db_transaction() as db:
        cursor = db.execute(
            "DELETE FROM verified_code_hashes WHERE policy_version < ?",
            (current_version,),
        )
        count = cursor.rowcount
        if count > 0:
            logger.info("Invalidated %d stale verified code hashes", count)
        return count
