"""Tests for carpenter.core.trigger_manager.

check_cron() now emits timer.fired events into the event pipeline instead
of creating work_queue items directly. The built-in _builtin.timer_forward
subscription routes them to work_queue items via process_subscriptions().
"""

import json
from datetime import datetime, timezone, timedelta

import pytest

from carpenter.core.engine import trigger_manager, subscriptions
from carpenter.core.engine.trigger_manager import TIMER_FIRED_EVENT
from carpenter.db import get_db


@pytest.fixture(autouse=True)
def _load_builtin_subs():
    """Ensure built-in subscriptions are loaded for timer forwarding."""
    subscriptions.load_builtin_subscriptions()
    yield
    subscriptions.reset()


def _process_timer_pipeline():
    """Run the event→subscription→work_queue pipeline.

    After check_cron() emits timer.fired events, this processes them
    through the subscription system to create work_queue items.
    """
    return subscriptions.process_subscriptions()


def test_add_cron_returns_id():
    """add_cron returns an integer cron entry ID."""
    cid = trigger_manager.add_cron("test-job", "*/5 * * * *", "cron.fire")
    assert isinstance(cid, int)
    assert cid > 0


def test_add_cron_invalid_expression():
    """add_cron raises ValueError for invalid cron expression."""
    with pytest.raises(ValueError, match="Invalid cron expression"):
        trigger_manager.add_cron("bad-job", "not a cron", "cron.fire")


def test_add_cron_calculates_next_fire():
    """add_cron stores a future next_fire_at time."""
    trigger_manager.add_cron("test-job", "*/5 * * * *", "cron.fire")
    entry = trigger_manager.get_cron("test-job")
    assert entry is not None
    fire_time = datetime.fromisoformat(entry["next_fire_at"])
    assert fire_time > datetime.now(timezone.utc)


def test_remove_cron():
    """remove_cron deletes the entry and returns True."""
    trigger_manager.add_cron("removable", "0 * * * *", "cron.fire")
    assert trigger_manager.remove_cron("removable") is True
    assert trigger_manager.get_cron("removable") is None


def test_remove_cron_nonexistent():
    """remove_cron returns False for nonexistent entry."""
    assert trigger_manager.remove_cron("nonexistent") is False


def test_check_cron_emits_timer_fired_events():
    """check_cron emits timer.fired events for due cron entries."""
    db = get_db()
    try:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        db.execute(
            "INSERT INTO cron_entries (name, cron_expr, event_type, next_fire_at, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("past-job", "*/5 * * * *", "cron.fire", past, True),
        )
        db.commit()
    finally:
        db.close()

    emitted = trigger_manager.check_cron()
    assert emitted == 1

    # Event should exist in the events table
    db = get_db()
    try:
        events = db.execute(
            "SELECT * FROM events WHERE event_type = ?",
            (TIMER_FIRED_EVENT,),
        ).fetchall()
        assert len(events) == 1
        payload = json.loads(events[0]["payload_json"])
        assert payload["cron_event_type"] == "cron.fire"
        assert payload["cron_name"] == "past-job"
    finally:
        db.close()


def test_check_cron_fires_to_work_queue_via_subscription():
    """check_cron -> timer.fired event -> subscription -> work_queue item."""
    db = get_db()
    try:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        db.execute(
            "INSERT INTO cron_entries (name, cron_expr, event_type, next_fire_at, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("past-job", "*/5 * * * *", "cron.fire", past, True),
        )
        db.commit()
    finally:
        db.close()

    trigger_manager.check_cron()
    _process_timer_pipeline()

    # Work item should exist via subscription forwarding
    db = get_db()
    try:
        items = db.execute(
            "SELECT * FROM work_queue WHERE status = 'pending'"
        ).fetchall()
        assert len(items) == 1
        assert items[0]["event_type"] == "cron.fire"
    finally:
        db.close()


def test_check_cron_idempotency():
    """check_cron does not create duplicate events for the same fire time."""
    db = get_db()
    try:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        db.execute(
            "INSERT INTO cron_entries (name, cron_expr, event_type, next_fire_at, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("idem-job", "*/5 * * * *", "cron.fire", past, True),
        )
        db.commit()
    finally:
        db.close()

    # First check emits the event
    assert trigger_manager.check_cron() == 1
    # Second check: entry has advanced, so 0 new events
    assert trigger_manager.check_cron() == 0


def test_check_cron_skips_disabled():
    """check_cron skips disabled entries."""
    db = get_db()
    try:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        db.execute(
            "INSERT INTO cron_entries (name, cron_expr, event_type, next_fire_at, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("disabled-job", "*/5 * * * *", "cron.fire", past, False),
        )
        db.commit()
    finally:
        db.close()

    assert trigger_manager.check_cron() == 0


def test_enable_cron():
    """enable_cron toggles the enabled state."""
    trigger_manager.add_cron("toggle-job", "0 * * * *", "cron.fire")
    trigger_manager.enable_cron("toggle-job", False)
    entry = trigger_manager.get_cron("toggle-job")
    assert entry["enabled"] == 0

    trigger_manager.enable_cron("toggle-job", True)
    entry = trigger_manager.get_cron("toggle-job")
    assert entry["enabled"] == 1


def test_list_cron():
    """list_cron returns all entries."""
    trigger_manager.add_cron("job-a", "0 * * * *", "cron.fire")
    trigger_manager.add_cron("job-b", "*/10 * * * *", "cron.fire")
    entries = trigger_manager.list_cron()
    assert len(entries) == 2
    names = {e["name"] for e in entries}
    assert names == {"job-a", "job-b"}


# ── add_once (one-shot scheduling) ─────────────────────────────────


def test_add_once_inserts_one_shot_entry():
    """add_once creates a cron entry with one_shot=TRUE."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    cid = trigger_manager.add_once("one-shot-1", future, "arc.dispatch", {"arc_id": 42})
    assert isinstance(cid, int)

    db = get_db()
    try:
        row = db.execute("SELECT * FROM cron_entries WHERE id = ?", (cid,)).fetchone()
    finally:
        db.close()

    assert row is not None
    assert row["one_shot"] == 1
    assert row["name"] == "one-shot-1"
    assert row["event_type"] == "arc.dispatch"
    assert json.loads(row["event_payload_json"]) == {"arc_id": 42}


def test_add_once_fires_via_pipeline():
    """add_once entry fires through event pipeline when next_fire_at is past."""
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    trigger_manager.add_once("one-shot-fire", past, "arc.dispatch", {"arc_id": 99})

    emitted = trigger_manager.check_cron()
    assert emitted == 1

    _process_timer_pipeline()

    # Work item should exist via subscription forwarding
    db = get_db()
    try:
        items = db.execute(
            "SELECT * FROM work_queue WHERE status = 'pending' AND event_type = 'arc.dispatch'"
        ).fetchall()
        assert len(items) == 1
        payload = json.loads(items[0]["payload_json"])
        assert payload["event_payload"] == {"arc_id": 99}
    finally:
        db.close()


def test_add_once_deletes_after_firing():
    """One-shot entry is deleted from cron_entries after check_cron fires it."""
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    cid = trigger_manager.add_once("one-shot-del", past, "test.event")

    trigger_manager.check_cron()

    # Entry should be gone
    db = get_db()
    try:
        row = db.execute("SELECT * FROM cron_entries WHERE id = ?", (cid,)).fetchone()
    finally:
        db.close()

    assert row is None


def test_add_once_does_not_refire():
    """Calling check_cron twice creates only 1 event for a one-shot entry."""
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    trigger_manager.add_once("one-shot-nodup", past, "test.event")

    assert trigger_manager.check_cron() == 1
    assert trigger_manager.check_cron() == 0  # Entry deleted, nothing to fire


def test_check_cron_recurring_unaffected_by_one_shot():
    """Recurring entries still work correctly alongside one-shot changes."""
    db = get_db()
    try:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        db.execute(
            "INSERT INTO cron_entries (name, cron_expr, event_type, next_fire_at, enabled, one_shot) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("recurring-job", "*/5 * * * *", "cron.fire", past, True, False),
        )
        db.commit()
    finally:
        db.close()

    assert trigger_manager.check_cron() == 1

    # Recurring entry should still exist (not deleted)
    entry = trigger_manager.get_cron("recurring-job")
    assert entry is not None
    # next_fire_at should have advanced
    fire_time = datetime.fromisoformat(entry["next_fire_at"])
    assert fire_time > datetime.now(timezone.utc)


def test_add_once_invalid_iso_raises():
    """add_once raises ValueError for non-ISO timestamp."""
    with pytest.raises(ValueError, match="Invalid ISO datetime"):
        trigger_manager.add_once("bad-time", "not-a-date", "test.event")


def test_add_once_normalizes_naive_to_utc():
    """add_once converts naive local-time ISO strings to UTC-aware format."""
    # Use a time in the future (naive, no timezone)
    naive_iso = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    cid = trigger_manager.add_once("naive-tz-test", naive_iso, "test.event")

    db = get_db()
    try:
        row = db.execute("SELECT next_fire_at FROM cron_entries WHERE id = ?", (cid,)).fetchone()
    finally:
        db.close()

    stored = row["next_fire_at"]
    # Must contain timezone offset (UTC-aware)
    assert "+00:00" in stored, f"Expected UTC-aware ISO, got: {stored}"
    # Parse and verify it's a valid UTC datetime
    dt = datetime.fromisoformat(stored)
    assert dt.tzinfo is not None


def test_add_once_naive_time_fires_correctly():
    """A naive local-time ISO string should fire at the correct UTC time."""
    # Create a one-shot 1 minute in the past using naive local time
    naive_past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    trigger_manager.add_once("naive-fire-test", naive_past, "test.event")

    # check_cron should detect it as due (it's in the past)
    emitted = trigger_manager.check_cron()
    assert emitted == 1


def test_add_once_utc_aware_time_preserved():
    """add_once with an already UTC-aware ISO string stores it correctly."""
    utc_future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    cid = trigger_manager.add_once("utc-aware-test", utc_future, "test.event")

    db = get_db()
    try:
        row = db.execute("SELECT next_fire_at FROM cron_entries WHERE id = ?", (cid,)).fetchone()
    finally:
        db.close()

    stored = row["next_fire_at"]
    assert "+00:00" in stored


# ── Timer event pipeline integration ──────────────────────────────


def test_timer_event_carries_cron_metadata():
    """timer.fired events include cron_id, cron_name, cron_event_type, fire_time."""
    db = get_db()
    try:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        db.execute(
            "INSERT INTO cron_entries (name, cron_expr, event_type, event_payload_json, "
            "next_fire_at, enabled) VALUES (?, ?, ?, ?, ?, ?)",
            ("meta-job", "*/5 * * * *", "cron.message",
             json.dumps({"message": "hello"}), past, True),
        )
        db.commit()
    finally:
        db.close()

    trigger_manager.check_cron()

    db = get_db()
    try:
        events = db.execute(
            "SELECT * FROM events WHERE event_type = ?",
            (TIMER_FIRED_EVENT,),
        ).fetchall()
        assert len(events) == 1

        payload = json.loads(events[0]["payload_json"])
        assert "cron_id" in payload
        assert payload["cron_name"] == "meta-job"
        assert payload["cron_event_type"] == "cron.message"
        assert payload["event_payload"] == {"message": "hello"}
        assert "fire_time" in payload
        assert events[0]["source"] == "cron:meta-job"
    finally:
        db.close()


def test_forward_timer_preserves_work_payload_format():
    """forward_timer subscription creates work items with pre-migration payload format."""
    db = get_db()
    try:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        db.execute(
            "INSERT INTO cron_entries (name, cron_expr, event_type, event_payload_json, "
            "next_fire_at, enabled) VALUES (?, ?, ?, ?, ?, ?)",
            ("payload-job", "*/5 * * * *", "cron.message",
             json.dumps({"message": "test", "conversation_id": 1}), past, True),
        )
        db.commit()
    finally:
        db.close()

    trigger_manager.check_cron()
    _process_timer_pipeline()

    db = get_db()
    try:
        items = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'cron.message' AND status = 'pending'"
        ).fetchall()
        assert len(items) == 1
        payload = json.loads(items[0]["payload_json"])
        # Should have the same structure as the old direct-insert format
        assert "cron_id" in payload
        assert payload["cron_name"] == "payload-job"
        assert payload["event_payload"]["message"] == "test"
        assert payload["event_payload"]["conversation_id"] == 1
    finally:
        db.close()
