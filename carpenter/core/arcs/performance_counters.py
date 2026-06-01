"""Ancestor performance counter updates for arcs.

Provides helpers to walk the ancestor chain of an arc and increment
aggregate counters (descendant_arc_count, descendant_executions,
descendant_tokens) on each ancestor.
"""

from datetime import datetime, timezone

from ...db import get_db, db_transaction


def _walk_ancestors(db, arc_id: int) -> list[int]:
    """Return list of ancestor arc IDs (parent, grandparent, ...).

    Walks up the parent chain from the given arc.
    Does not include the arc itself.
    """
    ancestors = []
    current_id = arc_id
    while True:
        row = db.execute(
            "SELECT parent_id FROM arcs WHERE id = ?", (current_id,)
        ).fetchone()
        if row is None or row["parent_id"] is None:
            break
        ancestors.append(row["parent_id"])
        current_id = row["parent_id"]
    return ancestors


def increment_ancestor_arc_count(arc_id: int, _db_conn=None) -> None:
    """Increment descendant_arc_count for all ancestors of the given arc.

    Called after a new arc is created as a child.
    """
    owns_connection = _db_conn is None
    db = _db_conn if _db_conn else get_db()
    try:
        ancestors = _walk_ancestors(db, arc_id)
        now = datetime.now(timezone.utc).isoformat()
        for ancestor_id in ancestors:
            db.execute(
                "UPDATE arcs SET descendant_arc_count = descendant_arc_count + 1, "
                "updated_at = ? WHERE id = ?",
                (now, ancestor_id),
            )
        if owns_connection:
            db.commit()
    finally:
        if owns_connection:
            db.close()


def increment_ancestor_executions(arc_id: int) -> None:
    """Increment descendant_executions for the arc and all its ancestors.

    Called after a code execution completes for the given arc.
    The arc itself also gets incremented (it is its own ancestor for
    counting purposes when viewed from a parent).
    """
    with db_transaction() as db:
        now = datetime.now(timezone.utc).isoformat()
        ancestors = _walk_ancestors(db, arc_id)
        for ancestor_id in ancestors:
            db.execute(
                "UPDATE arcs SET descendant_executions = descendant_executions + 1, "
                "updated_at = ? WHERE id = ?",
                (now, ancestor_id),
            )


def increment_ancestor_tokens(arc_id: int, tokens: int) -> None:
    """Increment descendant_tokens for all ancestors of the given arc.

    Called after an API call completes for work associated with the given arc.

    Args:
        arc_id: The arc that consumed tokens.
        tokens: Total tokens (input + output) to add.
    """
    if tokens <= 0:
        return
    with db_transaction() as db:
        now = datetime.now(timezone.utc).isoformat()
        ancestors = _walk_ancestors(db, arc_id)
        for ancestor_id in ancestors:
            db.execute(
                "UPDATE arcs SET descendant_tokens = descendant_tokens + ?, "
                "updated_at = ? WHERE id = ?",
                (tokens, now, ancestor_id),
            )
