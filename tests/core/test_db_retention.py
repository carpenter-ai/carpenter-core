"""Tests for the daily platform-DB retention sweep.

Covers the invariants documented in ``carpenter/core/engine/retention.py``:

- Only prunes old + terminal rows; young / non-terminal rows are spared.
- ``dry_run`` reports projected counts but deletes nothing.
- Global ``enabled=False`` short-circuits everything.
- Per-table ``enabled=False`` skips just that table.
- ``retention_days = 0`` is treated as disabled.
- Batching: rows > batch_size are deleted across multiple rounds.
- FK integrity: ``tool_calls`` are deleted before ``messages``.
- Trigger wiring: cron entry seeded, handler registered, idempotent.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from carpenter import config as _cfg
from carpenter.core.engine import (
    event_bus,
    main_loop,
    retention,
    subscriptions,
    trigger_manager,
)
from carpenter.core.engine.retention import (
    RETENTION_COMPLETED_EVENT_TYPE,
    RETENTION_CRON_SCHEDULE,
    RETENTION_EVENT_TYPE,
    RETENTION_TRIGGER_NAME,
    register_daily_retention,
    run_retention_sweep,
)
from carpenter.db import db_connection, db_transaction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _enable_retention(monkeypatch, **overrides):
    """Install a full ``db_retention`` config for the test.

    All tables default to enabled with retention_days=30; individual
    keys can be overridden via ``overrides``.  For nested overrides
    (per-table), pass ``tables={"events": {"enabled": False, ...}, ...}``.
    """
    base = {
        "enabled": True,
        "dry_run": False,
        "batch_size": 1000,
        "vacuum_after": False,
        "tables": {
            t: {"enabled": True, "retention_days": 30}
            for t in retention.KNOWN_TABLES
        },
    }
    # Shallow merge; nested "tables" is replaced wholesale if provided.
    for k, v in overrides.items():
        base[k] = v
    monkeypatch.setitem(_cfg.CONFIG, "db_retention", base)
    return base


def _insert_event(created_at: datetime, processed: bool):
    with db_transaction() as db:
        cur = db.execute(
            "INSERT INTO events (event_type, payload_json, processed, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("test.event", "{}", 1 if processed else 0, _iso(created_at)),
        )
        return cur.lastrowid


def _insert_work(status: str, completed_at: datetime | None):
    with db_transaction() as db:
        cur = db.execute(
            "INSERT INTO work_queue "
            "(event_type, payload_json, status, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "test.work",
                "{}",
                status,
                _iso(_now() - timedelta(days=365)),  # very old created
                _iso(completed_at) if completed_at else None,
            ),
        )
        return cur.lastrowid


def _insert_arc(status: str, updated_at: datetime) -> int:
    with db_transaction() as db:
        cur = db.execute(
            "INSERT INTO arcs (name, status, updated_at) VALUES (?, ?, ?)",
            ("test-arc", status, _iso(updated_at)),
        )
        return cur.lastrowid


def _insert_arc_history(arc_id: int) -> int:
    with db_transaction() as db:
        cur = db.execute(
            "INSERT INTO arc_history (arc_id, entry_type, content_json) "
            "VALUES (?, ?, ?)",
            (arc_id, "note", "{}"),
        )
        return cur.lastrowid


def _insert_arc_state(arc_id: int, key: str = "k") -> int:
    with db_transaction() as db:
        cur = db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
            (arc_id, key, "{}"),
        )
        return cur.lastrowid


def _insert_conversation(archived: bool, last_message_at: datetime) -> int:
    with db_transaction() as db:
        cur = db.execute(
            "INSERT INTO conversations (title, archived, last_message_at, started_at) "
            "VALUES (?, ?, ?, ?)",
            (
                "test-conv",
                1 if archived else 0,
                _iso(last_message_at),
                _iso(last_message_at),
            ),
        )
        return cur.lastrowid


def _insert_message(conv_id: int, created_at: datetime) -> int:
    with db_transaction() as db:
        cur = db.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (conv_id, "user", "hi", _iso(created_at)),
        )
        return cur.lastrowid


def _insert_tool_call(conv_id: int, message_id: int, created_at: datetime) -> int:
    with db_transaction() as db:
        cur = db.execute(
            "INSERT INTO tool_calls "
            "(conversation_id, message_id, tool_use_id, tool_name, input_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conv_id, message_id, "tuid", "t", "{}", _iso(created_at)),
        )
        return cur.lastrowid


def _count(table: str) -> int:
    with db_connection() as db:
        row = db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# Global on/off
# ---------------------------------------------------------------------------


def test_globally_disabled_short_circuits(monkeypatch):
    _enable_retention(monkeypatch, enabled=False)
    _insert_event(_now() - timedelta(days=90), processed=True)

    summary = run_retention_sweep()

    assert summary["enabled"] is False
    assert summary["total_deleted"] == 0
    assert summary["tables"] == {}
    assert _count("events") == 1


def test_missing_config_is_disabled(monkeypatch):
    # Remove any inherited db_retention config entirely.
    if "db_retention" in _cfg.CONFIG:
        monkeypatch.delitem(_cfg.CONFIG, "db_retention")
    _insert_event(_now() - timedelta(days=90), processed=True)

    summary = run_retention_sweep()

    assert summary["enabled"] is False
    assert _count("events") == 1


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def test_events_old_processed_pruned(monkeypatch):
    _enable_retention(monkeypatch)
    old_processed = _insert_event(_now() - timedelta(days=90), processed=True)
    young_processed = _insert_event(_now() - timedelta(days=1), processed=True)
    old_unprocessed = _insert_event(_now() - timedelta(days=90), processed=False)

    summary = run_retention_sweep()

    assert summary["tables"]["events"]["deleted"] == 1
    assert summary["tables"]["events"]["projected"] == 1

    with db_connection() as db:
        remaining = {r["id"] for r in db.execute("SELECT id FROM events").fetchall()}
    assert old_processed not in remaining
    assert young_processed in remaining
    assert old_unprocessed in remaining, "unprocessed events must never be deleted"


# ---------------------------------------------------------------------------
# work_queue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    ["complete", "completed", "failed", "cancelled", "dead_letter"],
)
def test_work_queue_terminal_status_pruned(monkeypatch, status):
    _enable_retention(monkeypatch)
    old_terminal = _insert_work(status, _now() - timedelta(days=90))
    young_terminal = _insert_work(status, _now() - timedelta(days=1))
    pending = _insert_work("pending", None)
    claimed = _insert_work("claimed", None)

    summary = run_retention_sweep()

    assert summary["tables"]["work_queue"]["deleted"] == 1

    with db_connection() as db:
        remaining = {r["id"] for r in db.execute("SELECT id FROM work_queue").fetchall()}
    assert old_terminal not in remaining
    assert young_terminal in remaining
    assert pending in remaining
    assert claimed in remaining


# ---------------------------------------------------------------------------
# arc_history / arc_state
# ---------------------------------------------------------------------------


def test_arc_history_only_pruned_for_frozen_old_arcs(monkeypatch):
    _enable_retention(monkeypatch)
    old_completed_arc = _insert_arc("completed", _now() - timedelta(days=90))
    young_completed_arc = _insert_arc("completed", _now() - timedelta(days=1))
    old_running_arc = _insert_arc("running", _now() - timedelta(days=90))

    h_old = _insert_arc_history(old_completed_arc)
    h_young = _insert_arc_history(young_completed_arc)
    h_running = _insert_arc_history(old_running_arc)

    summary = run_retention_sweep()

    assert summary["tables"]["arc_history"]["deleted"] == 1

    with db_connection() as db:
        remaining = {r["id"] for r in db.execute("SELECT id FROM arc_history").fetchall()}
    assert h_old not in remaining
    assert h_young in remaining
    assert h_running in remaining

    # Arcs themselves must be untouched (they are lineage tombstones).
    with db_connection() as db:
        arc_ids = {r["id"] for r in db.execute("SELECT id FROM arcs").fetchall()}
    assert {old_completed_arc, young_completed_arc, old_running_arc}.issubset(arc_ids)


def test_arc_state_only_pruned_for_frozen_old_arcs(monkeypatch):
    _enable_retention(monkeypatch)
    old_failed = _insert_arc("failed", _now() - timedelta(days=90))
    old_running = _insert_arc("running", _now() - timedelta(days=90))

    s_old = _insert_arc_state(old_failed)
    s_running = _insert_arc_state(old_running)

    summary = run_retention_sweep()

    assert summary["tables"]["arc_state"]["deleted"] == 1

    with db_connection() as db:
        remaining = {r["id"] for r in db.execute("SELECT id FROM arc_state").fetchall()}
    assert s_old not in remaining
    assert s_running in remaining


# ---------------------------------------------------------------------------
# messages + tool_calls (FK order)
# ---------------------------------------------------------------------------


def test_messages_only_pruned_under_archived_conversation(monkeypatch):
    _enable_retention(monkeypatch)
    # Old unarchived — must NOT be pruned.
    conv_live = _insert_conversation(archived=False, last_message_at=_now() - timedelta(days=90))
    m_live = _insert_message(conv_live, _now() - timedelta(days=90))

    # Old archived — should be pruned.
    conv_dead = _insert_conversation(archived=True, last_message_at=_now() - timedelta(days=90))
    m_dead = _insert_message(conv_dead, _now() - timedelta(days=90))

    # Recently archived — must NOT be pruned (last_message_at not past cutoff).
    conv_recent = _insert_conversation(archived=True, last_message_at=_now() - timedelta(days=1))
    m_recent = _insert_message(conv_recent, _now() - timedelta(days=1))

    summary = run_retention_sweep()

    assert summary["tables"]["messages"]["deleted"] == 1

    with db_connection() as db:
        remaining = {r["id"] for r in db.execute("SELECT id FROM messages").fetchall()}
    assert m_live in remaining
    assert m_recent in remaining
    assert m_dead not in remaining


def test_tool_calls_deleted_before_messages(monkeypatch):
    """FK integrity: no tool_calls row should point at a deleted message id."""
    _enable_retention(monkeypatch)
    conv = _insert_conversation(archived=True, last_message_at=_now() - timedelta(days=90))
    msg = _insert_message(conv, _now() - timedelta(days=90))
    tc = _insert_tool_call(conv, msg, _now() - timedelta(days=90))

    summary = run_retention_sweep()

    assert summary["tables"]["tool_calls"]["deleted"] == 1
    assert summary["tables"]["messages"]["deleted"] == 1

    # After sweep both must be gone with no dangling FK complaints.
    with db_connection() as db:
        assert db.execute("SELECT id FROM tool_calls WHERE id = ?", (tc,)).fetchone() is None
        assert db.execute("SELECT id FROM messages WHERE id = ?", (msg,)).fetchone() is None

        # Fire an FK integrity check.
        row = db.execute("PRAGMA foreign_key_check").fetchone()
    assert row is None, f"orphan FK detected after sweep: {dict(row) if row else None}"


# ---------------------------------------------------------------------------
# dry_run
# ---------------------------------------------------------------------------


def test_dry_run_reports_but_does_not_delete(monkeypatch):
    _enable_retention(monkeypatch, dry_run=True)
    _insert_event(_now() - timedelta(days=90), processed=True)
    _insert_event(_now() - timedelta(days=90), processed=True)

    summary = run_retention_sweep()

    assert summary["dry_run"] is True
    assert summary["tables"]["events"]["projected"] == 2
    assert summary["tables"]["events"]["deleted"] == 0
    assert summary["total_deleted"] == 0
    assert _count("events") == 2


def test_dry_run_override_forces_dry_pass(monkeypatch):
    """The CLI/operator override wins over the config flag."""
    _enable_retention(monkeypatch, dry_run=False)
    _insert_event(_now() - timedelta(days=90), processed=True)

    summary = run_retention_sweep(dry_run_override=True)

    assert summary["dry_run"] is True
    assert summary["tables"]["events"]["deleted"] == 0
    assert _count("events") == 1


# ---------------------------------------------------------------------------
# Per-table enabled + retention_days=0
# ---------------------------------------------------------------------------


def test_per_table_disable_skips_only_that_table(monkeypatch):
    cfg = _enable_retention(monkeypatch)
    cfg["tables"]["events"] = {"enabled": False, "retention_days": 30}

    _insert_event(_now() - timedelta(days=90), processed=True)
    old_arc = _insert_arc("completed", _now() - timedelta(days=90))
    _insert_arc_history(old_arc)

    summary = run_retention_sweep()

    assert summary["tables"]["events"]["enabled"] is False
    assert summary["tables"]["events"]["deleted"] == 0
    assert summary["tables"]["arc_history"]["deleted"] == 1
    assert _count("events") == 1


def test_retention_days_zero_is_disabled(monkeypatch):
    cfg = _enable_retention(monkeypatch)
    cfg["tables"]["events"] = {"enabled": True, "retention_days": 0}

    _insert_event(_now() - timedelta(days=90), processed=True)

    summary = run_retention_sweep()

    # retention_days=0 is treated as disabled — projected and deleted are 0.
    assert summary["tables"]["events"]["deleted"] == 0
    assert summary["tables"]["events"]["projected"] == 0
    assert _count("events") == 1


def test_non_int_retention_days_is_disabled(monkeypatch):
    cfg = _enable_retention(monkeypatch)
    cfg["tables"]["events"] = {"enabled": True, "retention_days": "junk"}

    _insert_event(_now() - timedelta(days=90), processed=True)

    summary = run_retention_sweep()

    assert summary["tables"]["events"]["deleted"] == 0
    assert _count("events") == 1


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------


def test_batched_delete_handles_more_rows_than_batch_size(monkeypatch):
    _enable_retention(monkeypatch, batch_size=5)
    old = _now() - timedelta(days=90)
    for _ in range(23):
        _insert_event(old, processed=True)

    summary = run_retention_sweep()

    assert summary["tables"]["events"]["projected"] == 23
    assert summary["tables"]["events"]["deleted"] == 23
    assert _count("events") == 0


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_pipeline():
    subscriptions.reset()
    main_loop._handlers.clear()
    yield
    subscriptions.reset()
    main_loop._handlers.clear()


def test_cron_and_handler_registered(fresh_pipeline):
    register_daily_retention(main_loop.register_handler)

    entry = trigger_manager.get_cron(RETENTION_TRIGGER_NAME)
    assert entry is not None
    assert entry["cron_expr"] == RETENTION_CRON_SCHEDULE
    assert entry["event_type"] == RETENTION_EVENT_TYPE
    assert entry["enabled"]

    handler = main_loop.get_handler(RETENTION_EVENT_TYPE)
    assert handler is retention.handle_retention_work_item


def test_register_is_idempotent(fresh_pipeline):
    register_daily_retention(main_loop.register_handler)
    register_daily_retention(main_loop.register_handler)

    entries = [e for e in trigger_manager.list_cron() if e["name"] == RETENTION_TRIGGER_NAME]
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_handler_runs_sweep_and_emits_completed_event(fresh_pipeline, monkeypatch):
    _enable_retention(monkeypatch)
    _insert_event(_now() - timedelta(days=90), processed=True)

    register_daily_retention(main_loop.register_handler)
    handler = main_loop.get_handler(RETENTION_EVENT_TYPE)

    await handler(work_id=1, payload={})

    # events row deleted
    assert _count("events") >= 1  # the completed event itself is in the table now
    with db_connection() as db:
        ev = db.execute(
            "SELECT payload_json FROM events "
            "WHERE event_type = ? ORDER BY id DESC LIMIT 1",
            (RETENTION_COMPLETED_EVENT_TYPE,),
        ).fetchone()
    assert ev is not None
    payload = json.loads(ev["payload_json"])
    assert payload["enabled"] is True
    assert payload["tables"]["events"]["deleted"] == 1
