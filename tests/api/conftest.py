"""Shared test helpers for tests/api/."""

from datetime import datetime, timezone, timedelta

from carpenter.db import get_db


def _create_reviewed_session(session_id="test-session", conversation_id=None,
                              execution_context="reviewed"):
    """Create a valid reviewed execution session and return the session ID."""
    db = get_db()
    try:
        # Create a code file
        cursor = db.execute(
            "INSERT INTO code_files (file_path, source, review_status) VALUES (?, ?, ?)",
            ("/tmp/test.py", "test", "approved")
        )
        code_file_id = cursor.lastrowid

        # Create execution record
        cursor = db.execute(
            "INSERT INTO code_executions (code_file_id, execution_status, started_at) "
            "VALUES (?, 'running', ?)",
            (code_file_id, datetime.now(timezone.utc).isoformat())
        )
        execution_id = cursor.lastrowid

        # Create valid session
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        db.execute(
            "INSERT INTO execution_sessions "
            "(session_id, code_file_id, execution_id, reviewed, conversation_id, "
            "execution_context, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, code_file_id, execution_id, True,
             conversation_id, execution_context, expires_at.isoformat())
        )
        db.commit()
    finally:
        db.close()
    return session_id
