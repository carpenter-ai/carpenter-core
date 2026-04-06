"""Tests for carpenter.db."""

import sqlite3

import pytest


def test_wal_mode(db):
    """Database uses WAL journal mode."""
    result = db.execute("PRAGMA journal_mode").fetchone()
    assert result[0] == "wal"


def test_foreign_keys_enabled(db):
    """Foreign keys are enabled."""
    result = db.execute("PRAGMA foreign_keys").fetchone()
    assert result[0] == 1


def test_row_factory(db):
    """Rows are accessible by column name."""
    db.execute("INSERT INTO events (event_type, payload_json) VALUES (?, ?)",
               ("test.event", '{"key": "value"}'))
    db.commit()
    row = db.execute("SELECT * FROM events LIMIT 1").fetchone()
    assert row["event_type"] == "test.event"
    assert row["payload_json"] == '{"key": "value"}'


def test_idempotent_init(test_db):
    """init_db can be called multiple times without error."""
    from carpenter.db import init_db
    # Second call should not raise
    init_db()
    init_db()


def test_directories_created(test_db, tmp_path):
    """init_db creates required directories."""
    from carpenter import config

    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "code").is_dir()
    assert (tmp_path / "workspaces").is_dir()


def test_all_tables_exist(db):
    """Schema creates all expected tables."""
    tables = {
        row[0] for row in
        db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    expected = {
        "arcs", "arc_activations", "arc_history",
        "code_files", "code_executions",
        "events", "event_matchers", "work_queue",
        "cron_entries", "workflow_templates",
        "conversations", "messages", "archived_arcs",
        "tool_calls", "api_calls", "conversation_arcs",
    }
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"


def test_foreign_key_enforcement(db):
    """Foreign key constraints are enforced."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
            (99999, "user", "hello"),
        )


def test_migration_idempotent(test_db):
    """Running _migrate() multiple times is safe."""
    from carpenter.db import get_db, _migrate
    conn = get_db()
    try:
        _migrate(conn)  # Already ran during init_db, should be a no-op
        _migrate(conn)
    finally:
        conn.close()
    # Verify column still exists
    conn = get_db()
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        assert "content_json" in cols
    finally:
        conn.close()


def test_sqlcipher_fallback_without_key():
    """get_db() without db_encryption_key uses plain sqlite3."""
    from carpenter.db import get_db
    conn = get_db()
    try:
        # Should work fine — plain sqlite3
        result = conn.execute("PRAGMA journal_mode").fetchone()
        assert result[0] == "wal"
    finally:
        conn.close()


def test_sqlcipher_fallback_warns_when_unavailable(monkeypatch, caplog):
    """get_db() with key but no pysqlcipher3 logs a warning and falls back."""
    import carpenter.db as db_module
    from carpenter import config

    monkeypatch.setattr(db_module, "_sqlcipher_module", None)
    monkeypatch.setitem(config.CONFIG, "db_encryption_key", "test-secret-key")

    import logging
    with caplog.at_level(logging.WARNING, logger="carpenter.db"):
        conn = db_module.get_db()
        try:
            # Should still work via plain sqlite3
            result = conn.execute("PRAGMA journal_mode").fetchone()
            assert result[0] == "wal"
        finally:
            conn.close()

    assert "pysqlcipher3 is not installed" in caplog.text
