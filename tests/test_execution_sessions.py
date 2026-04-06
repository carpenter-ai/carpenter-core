"""Tests for execution session security model.

Tests the session validation logic used by the dispatch bridge.
"""
import pytest
from datetime import datetime, timezone, timedelta

from carpenter.db import get_db
from carpenter.api.callbacks import validate_execution_session
from carpenter.executor.dispatch_bridge import validate_and_dispatch, DispatchError


def _create_session(db, *, reviewed=True, expired=False, arc_id=None):
    """Create a code_file, code_execution, and execution_session in one call.

    Args:
        db: Database connection (caller manages open/close/commit).
        reviewed: If True, code file is "approved" and session reviewed=True.
                  If False, code file is "pending" and session reviewed=False.
        expired: If True, session expires 1 hour in the past.
                 If False, session expires 1 hour in the future.
        arc_id: If set, creates an arc with this ID before the session rows.

    Returns:
        dict with keys: session_id, code_file_id, execution_id.
    """
    if arc_id is not None:
        db.execute("INSERT INTO arcs (id, name) VALUES (?, ?)", (arc_id, "test-arc"))

    review_status = "approved" if reviewed else "pending"
    cursor = db.execute(
        "INSERT INTO code_files (file_path, source, review_status) VALUES (?, ?, ?)",
        ("/tmp/test.py", "test", review_status),
    )
    code_file_id = cursor.lastrowid

    cursor = db.execute(
        "INSERT INTO code_executions (code_file_id, execution_status, started_at) "
        "VALUES (?, 'running', ?)",
        (code_file_id, datetime.now(timezone.utc).isoformat()),
    )
    execution_id = cursor.lastrowid

    if expired:
        expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    # Deterministic session ID from the execution_id for easy tracing
    session_id = f"test-session-{execution_id}"
    db.execute(
        "INSERT INTO execution_sessions "
        "(session_id, code_file_id, execution_id, reviewed, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, code_file_id, execution_id, reviewed, expires_at.isoformat()),
    )
    db.commit()

    return {
        "session_id": session_id,
        "code_file_id": code_file_id,
        "execution_id": execution_id,
    }


def test_validate_session_with_valid_reviewed_session():
    """Valid reviewed session passes validation."""
    db = get_db()
    try:
        info = _create_session(db, reviewed=True, expired=False)
    finally:
        db.close()

    assert validate_execution_session(info["session_id"]) is True


def test_validate_session_with_expired_session():
    """Expired session fails validation."""
    db = get_db()
    try:
        info = _create_session(db, reviewed=True, expired=True)
    finally:
        db.close()

    assert validate_execution_session(info["session_id"]) is False


def test_validate_session_with_missing_session():
    """Missing session ID fails validation."""
    assert validate_execution_session(None) is False
    assert validate_execution_session("") is False
    assert validate_execution_session("nonexistent-session") is False


def test_validate_session_with_unreviewed_session():
    """Unreviewed session fails validation."""
    db = get_db()
    try:
        info = _create_session(db, reviewed=False, expired=False)
    finally:
        db.close()

    assert validate_execution_session(info["session_id"]) is False


def test_session_persists_after_expiry():
    """Session records remain in DB for audit even after expiry."""
    db = get_db()
    try:
        info = _create_session(db, reviewed=True, expired=True)
        session_id = info["session_id"]

        # Verify it's rejected for validation
        assert validate_execution_session(session_id) is False

        # But verify it still exists in the database
        row = db.execute(
            "SELECT * FROM execution_sessions WHERE session_id = ?",
            (session_id,)
        ).fetchone()
        assert row is not None
        assert row["session_id"] == session_id
        assert row["code_file_id"] == info["code_file_id"]
        assert row["execution_id"] == info["execution_id"]
    finally:
        db.close()


def test_dispatch_with_valid_session_allows_action_tools():
    """Reviewed session can invoke action tools via dispatch bridge."""
    db = get_db()
    try:
        info = _create_session(db, reviewed=True, expired=False, arc_id=1)
    finally:
        db.close()

    result = validate_and_dispatch(
        "state.set",
        {"arc_id": 1, "key": "test", "value": "value"},
        session_id=info["session_id"],
    )
    assert result["success"] is True


def test_dispatch_without_session_rejects_action_tools():
    """Missing session ID cannot invoke action tools via dispatch bridge."""
    db = get_db()
    try:
        db.execute("INSERT INTO arcs (id, name) VALUES (?, ?)", (1, "test-arc"))
        db.commit()
    finally:
        db.close()

    with pytest.raises(DispatchError, match="valid reviewed execution session"):
        validate_and_dispatch(
            "state.set",
            {"arc_id": 1, "key": "test", "value": "value"},
        )


def test_dispatch_with_expired_session_rejects_action_tools():
    """Expired session cannot invoke action tools via dispatch bridge."""
    db = get_db()
    try:
        info = _create_session(db, reviewed=True, expired=True, arc_id=1)
    finally:
        db.close()

    with pytest.raises(DispatchError):
        validate_and_dispatch(
            "state.set",
            {"arc_id": 1, "key": "test", "value": "value"},
            session_id=info["session_id"],
        )


def test_dispatch_with_unreviewed_session_rejects_action_tools():
    """Unreviewed session cannot invoke action tools via dispatch bridge."""
    db = get_db()
    try:
        info = _create_session(db, reviewed=False, expired=False, arc_id=1)
    finally:
        db.close()

    with pytest.raises(DispatchError):
        validate_and_dispatch(
            "state.set",
            {"arc_id": 1, "key": "test", "value": "value"},
            session_id=info["session_id"],
        )


def test_read_tools_work_without_session():
    """Read-only tools don't require session validation."""
    db = get_db()
    try:
        db.execute("INSERT INTO arcs (id, name) VALUES (?, ?)", (1, "test-arc"))
        db.commit()
    finally:
        db.close()

    result = validate_and_dispatch(
        "state.get",
        {"arc_id": 1, "key": "test"},
    )
    assert isinstance(result, dict)
