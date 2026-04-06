"""Tests for platform restart functionality and startup recovery."""

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ── Startup recovery ───────────────────────────────────────────────


def test_recover_on_startup_resets_claimed_work_items(test_db):
    """Claimed work items are reset to pending on startup."""
    from carpenter.db import get_db, _recover_on_startup

    db = get_db()
    db.execute(
        "INSERT INTO work_queue (event_type, payload_json, status, claimed_at, max_retries) "
        "VALUES ('test.event', '{}', 'claimed', CURRENT_TIMESTAMP, 3)"
    )
    db.commit()

    _recover_on_startup(db)

    row = db.execute("SELECT status FROM work_queue WHERE event_type='test.event'").fetchone()
    assert row["status"] == "pending"
    db.close()


def test_recover_on_startup_resets_active_arcs(test_db):
    """Active arcs are reset to pending on startup."""
    from carpenter.db import get_db, _recover_on_startup

    db = get_db()
    db.execute(
        "INSERT INTO arcs (name, goal, status) VALUES ('test-arc', 'test goal', 'active')"
    )
    db.commit()

    _recover_on_startup(db)

    row = db.execute("SELECT status FROM arcs WHERE name='test-arc'").fetchone()
    assert row["status"] == "pending"
    db.close()


def test_recover_on_startup_leaves_pending_work_alone(test_db):
    """Pending work items are not touched by recovery."""
    from carpenter.db import get_db, _recover_on_startup

    db = get_db()
    db.execute(
        "INSERT INTO work_queue (event_type, payload_json, status, max_retries) "
        "VALUES ('test.event', '{}', 'pending', 3)"
    )
    db.commit()

    _recover_on_startup(db)

    row = db.execute("SELECT status FROM work_queue WHERE event_type='test.event'").fetchone()
    assert row["status"] == "pending"
    db.close()


def test_recover_on_startup_leaves_completed_arcs_alone(test_db):
    """Completed arcs are not touched by recovery."""
    from carpenter.db import get_db, _recover_on_startup

    db = get_db()
    db.execute(
        "INSERT INTO arcs (name, goal, status) VALUES ('done-arc', 'goal', 'completed')"
    )
    db.commit()

    _recover_on_startup(db)

    row = db.execute("SELECT status FROM arcs WHERE name='done-arc'").fetchone()
    assert row["status"] == "completed"
    db.close()


# ── credential_registry sync ───────────────────────────────────────


def test_sync_credential_registry_creates_file(tmp_path):
    """_sync_credential_registry copies the bundled YAML to config/."""
    from carpenter.db import _sync_credential_registry
    _sync_credential_registry(str(tmp_path))

    registry_file = tmp_path / "config" / "credential_registry.yaml"
    assert registry_file.is_file()
    content = registry_file.read_text()
    assert "ANTHROPIC_API_KEY" in content


def test_sync_credential_registry_does_not_overwrite(tmp_path):
    """_sync_credential_registry skips if the file already exists."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    registry_file = config_dir / "credential_registry.yaml"
    registry_file.write_text("CUSTOM_KEY:\n  config_key: custom\n")

    from carpenter.db import _sync_credential_registry
    _sync_credential_registry(str(tmp_path))

    # File should be unchanged (not overwritten with bundled version)
    assert "CUSTOM_KEY" in registry_file.read_text()


# ── Main loop restart state ────────────────────────────────────────


def test_set_restart_pending_sets_flag():
    """set_restart_pending sets _restart_pending and _restart_mode."""
    import carpenter.core.engine.main_loop as ml

    original_pending = ml._restart_pending
    original_mode = ml._restart_mode
    try:
        ml._restart_pending = False
        ml.set_restart_pending(mode="opportunistic", reason="test")
        assert ml._restart_pending is True
        assert ml._restart_mode == "opportunistic"
    finally:
        ml._restart_pending = original_pending
        ml._restart_mode = original_mode


def test_set_restart_pending_wakes_loop():
    """set_restart_pending calls wake_signal.set()."""
    import carpenter.core.engine.main_loop as ml

    original_pending = ml._restart_pending
    try:
        ml._restart_pending = False
        ml.wake_signal.clear()
        ml.set_restart_pending(mode="graceful")
        assert ml.wake_signal.is_set()
    finally:
        ml._restart_pending = original_pending
        ml.wake_signal.clear()


def test_check_restart_noop_when_not_pending():
    """_check_restart does nothing when _restart_pending is False."""
    import carpenter.core.engine.main_loop as ml

    original = ml._restart_pending
    try:
        ml._restart_pending = False
        # Should not raise or call os.execv
        with patch("os.execv") as mock_execv:
            ml._check_restart()
            mock_execv.assert_not_called()
    finally:
        ml._restart_pending = original


def test_check_restart_graceful_sets_shutdown_event():
    """_check_restart in graceful mode sets the shutdown_event."""
    import carpenter.core.engine.main_loop as ml

    original_pending = ml._restart_pending
    original_mode = ml._restart_mode
    original_shutdown = ml._shutdown_event

    shutdown_event = asyncio.Event()
    ml._restart_pending = True
    ml._restart_mode = "graceful"
    ml._shutdown_event = shutdown_event
    try:
        ml._check_restart()
        assert shutdown_event.is_set()
    finally:
        ml._restart_pending = original_pending
        ml._restart_mode = original_mode
        ml._shutdown_event = original_shutdown


def test_check_restart_opportunistic_waits_for_idle(test_db):
    """_check_restart in opportunistic mode does not restart when arcs are active."""
    from carpenter.db import get_db
    import carpenter.core.engine.main_loop as ml

    # Insert an active arc into the test DB
    db = get_db()
    db.execute(
        "INSERT INTO arcs (name, goal, status) VALUES ('busy-arc', 'goal', 'active')"
    )
    db.commit()
    db.close()

    original_pending = ml._restart_pending
    original_mode = ml._restart_mode
    ml._restart_pending = True
    ml._restart_mode = "opportunistic"

    try:
        with patch("os.execv") as mock_execv:
            ml._check_restart()
        mock_execv.assert_not_called()
    finally:
        ml._restart_pending = original_pending
        ml._restart_mode = original_mode


# ── platform_handler ───────────────────────────────────────────────


def test_platform_handler_registers():
    """register_handlers registers platform.restart event type."""
    from carpenter.core.workflows.platform_handler import register_handlers

    registry = {}
    register_handlers(lambda event_type, fn: registry.update({event_type: fn}))
    assert "platform.restart" in registry


@pytest.mark.asyncio
async def test_handle_platform_restart_calls_set_restart_pending():
    """handle_platform_restart delegates to main_loop.set_restart_pending."""
    from carpenter.core.workflows.platform_handler import handle_platform_restart
    import carpenter.core.engine.main_loop as ml

    original_pending = ml._restart_pending
    try:
        ml._restart_pending = False
        await handle_platform_restart(work_id=1, payload={"mode": "opportunistic", "reason": "unit-test"})
        assert ml._restart_pending is True
        assert ml._restart_mode == "opportunistic"
    finally:
        ml._restart_pending = original_pending


# ── _tool_get_platform_status ──────────────────────────────────────


def test_tool_get_platform_status_returns_string(test_db):
    """get_platform_status returns a non-empty string."""
    from carpenter.chat_tool_loader import get_handler

    result = get_handler("get_platform_status")({})
    assert isinstance(result, str)
    assert "Active arcs" in result
    assert "Restart pending" in result


# ── _enqueue_restart ───────────────────────────────────────────────


def test_enqueue_restart_inserts_work_item(tmp_path):
    """_enqueue_restart inserts a platform.restart work item."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "platform.db"

    # Minimal schema
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE work_queue (
            id INTEGER PRIMARY KEY,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            max_retries INTEGER NOT NULL DEFAULT 3,
            retry_count INTEGER NOT NULL DEFAULT 0,
            claimed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

    from carpenter import config as cfg_module
    original_config = dict(cfg_module.CONFIG)
    cfg_module.CONFIG["database_path"] = str(db_path)
    try:
        from carpenter.__main__ import _enqueue_restart
        result = _enqueue_restart("test reason")
        assert result is True

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT event_type, payload_json FROM work_queue"
        ).fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0][0] == "platform.restart"
        payload = json.loads(rows[0][1])
        assert payload["mode"] == "opportunistic"
    finally:
        cfg_module.CONFIG.clear()
        cfg_module.CONFIG.update(original_config)


def test_enqueue_restart_no_crash_if_db_missing(tmp_path, monkeypatch):
    """_enqueue_restart returns False gracefully if DB doesn't exist."""
    from carpenter import config as cfg_module
    original_config = dict(cfg_module.CONFIG)
    cfg_module.CONFIG["database_path"] = str(tmp_path / "nonexistent.db")
    try:
        from carpenter.__main__ import _enqueue_restart
        result = _enqueue_restart("test")
        assert result is False
    finally:
        cfg_module.CONFIG.clear()
        cfg_module.CONFIG.update(original_config)
