"""Tests for enhanced startup recovery (_recover_on_startup)."""

import json
from datetime import datetime, timezone, timedelta

import pytest


def test_recover_resets_active_arcs_to_pending(test_db):
    """Active arcs are reset to pending on startup recovery."""
    from carpenter.db import get_db, _recover_on_startup

    db = get_db()
    db.execute(
        "INSERT INTO arcs (name, goal, status) VALUES ('test-arc', 'goal', 'active')"
    )
    db.commit()

    _recover_on_startup(db)

    row = db.execute("SELECT status FROM arcs WHERE name='test-arc'").fetchone()
    assert row["status"] == "pending"
    db.close()


def test_recover_resets_claimed_work_items(test_db):
    """Claimed work items are reset to pending with cleared claimed_at."""
    from carpenter.db import get_db, _recover_on_startup

    db = get_db()
    db.execute(
        "INSERT INTO work_queue (event_type, payload_json, status, claimed_at, max_retries) "
        "VALUES ('test.event', '{}', 'claimed', CURRENT_TIMESTAMP, 3)"
    )
    db.commit()

    _recover_on_startup(db)

    row = db.execute("SELECT status, claimed_at FROM work_queue WHERE event_type='test.event'").fetchone()
    assert row["status"] == "pending"
    assert row["claimed_at"] is None
    db.close()


def test_recover_marks_running_executions_crashed(test_db):
    """Running code_executions are marked as crashed."""
    from carpenter.db import get_db, _recover_on_startup

    db = get_db()
    db.execute(
        "INSERT INTO code_files (file_path, source) VALUES ('/tmp/test.py', 'test')"
    )
    db.execute(
        "INSERT INTO code_executions (code_file_id, execution_status, started_at) "
        "VALUES (1, 'running', CURRENT_TIMESTAMP)"
    )
    db.commit()

    _recover_on_startup(db)

    row = db.execute("SELECT execution_status, completed_at FROM code_executions WHERE id=1").fetchone()
    assert row["execution_status"] == "crashed"
    assert row["completed_at"] is not None
    db.close()


def test_recover_cleans_expired_sessions(test_db):
    """Expired execution_sessions are deleted."""
    from carpenter.db import get_db, _recover_on_startup

    db = get_db()
    db.execute(
        "INSERT INTO code_files (file_path, source) VALUES ('/tmp/test.py', 'test')"
    )
    db.execute(
        "INSERT INTO code_executions (code_file_id, execution_status) VALUES (1, 'success')"
    )
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    db.execute(
        "INSERT INTO execution_sessions (session_id, code_file_id, execution_id, reviewed, expires_at) "
        "VALUES ('expired-session', 1, 1, 0, ?)",
        (past,),
    )
    db.execute(
        "INSERT INTO execution_sessions (session_id, code_file_id, execution_id, reviewed, expires_at) "
        "VALUES ('valid-session', 1, 1, 0, ?)",
        (future,),
    )
    db.commit()

    _recover_on_startup(db)

    rows = db.execute("SELECT session_id FROM execution_sessions").fetchall()
    session_ids = [r["session_id"] for r in rows]
    assert "expired-session" not in session_ids
    assert "valid-session" in session_ids
    db.close()


def test_recover_logs_arc_history(test_db):
    """Recovery inserts arc_history entries for each recovered arc."""
    from carpenter.db import get_db, _recover_on_startup

    db = get_db()
    db.execute(
        "INSERT INTO arcs (name, goal, status) VALUES ('arc-1', 'goal', 'active')"
    )
    db.execute(
        "INSERT INTO arcs (name, goal, status) VALUES ('arc-2', 'goal', 'active')"
    )
    db.commit()

    arc1_id = db.execute("SELECT id FROM arcs WHERE name='arc-1'").fetchone()["id"]
    arc2_id = db.execute("SELECT id FROM arcs WHERE name='arc-2'").fetchone()["id"]

    _recover_on_startup(db)

    history = db.execute(
        "SELECT arc_id, entry_type, content_json, actor FROM arc_history "
        "WHERE actor='startup_recovery'"
    ).fetchall()

    recovered_ids = {row["arc_id"] for row in history}
    assert arc1_id in recovered_ids
    assert arc2_id in recovered_ids

    for row in history:
        assert row["entry_type"] == "status_change"
        content = json.loads(row["content_json"])
        assert content["from"] == "active"
        assert content["to"] == "pending"
        assert row["actor"] == "startup_recovery"
    db.close()


def test_recover_is_idempotent(test_db):
    """Running recovery twice has no additional effect."""
    from carpenter.db import get_db, _recover_on_startup

    db = get_db()
    db.execute(
        "INSERT INTO arcs (name, goal, status) VALUES ('arc-idem', 'goal', 'active')"
    )
    db.execute(
        "INSERT INTO work_queue (event_type, payload_json, status, claimed_at, max_retries) "
        "VALUES ('test.idem', '{}', 'claimed', CURRENT_TIMESTAMP, 3)"
    )
    db.commit()

    _recover_on_startup(db)
    _recover_on_startup(db)

    # Arc should still be pending (not double-changed)
    row = db.execute("SELECT status FROM arcs WHERE name='arc-idem'").fetchone()
    assert row["status"] == "pending"

    # Only one arc_history entry from startup_recovery
    arc_id = db.execute("SELECT id FROM arcs WHERE name='arc-idem'").fetchone()["id"]
    history_count = db.execute(
        "SELECT COUNT(*) as cnt FROM arc_history WHERE arc_id=? AND actor='startup_recovery'",
        (arc_id,),
    ).fetchone()["cnt"]
    assert history_count == 1

    # Work item should still be pending
    row = db.execute("SELECT status FROM work_queue WHERE event_type='test.idem'").fetchone()
    assert row["status"] == "pending"
    db.close()
