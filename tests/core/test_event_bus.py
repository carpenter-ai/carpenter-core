"""Tests for carpenter.core.event_bus."""

import json
from datetime import datetime, timezone, timedelta

import pytest

from carpenter.core.engine import event_bus
from carpenter.db import get_db


def _create_arc(arc_id: int, name: str = "test-arc") -> int:
    """Insert a minimal arc row and return its ID (satisfies FK constraints)."""
    db = get_db()
    try:
        db.execute(
            "INSERT INTO arcs (id, name) VALUES (?, ?)",
            (arc_id, name),
        )
        db.commit()
        return arc_id
    finally:
        db.close()


def test_record_event_returns_id():
    """record_event returns an integer event ID."""
    eid = event_bus.record_event("test.event", {"key": "value"})
    assert isinstance(eid, int)
    assert eid > 0


def test_record_event_stores_data():
    """Recorded event has correct type, payload, and source."""
    eid = event_bus.record_event("chat.message", {"text": "hello"}, source="user")
    event = event_bus.get_event(eid)
    assert event["event_type"] == "chat.message"
    assert json.loads(event["payload_json"]) == {"text": "hello"}
    assert event["source"] == "user"
    assert event["processed"] == 0


def test_register_matcher():
    """register_matcher creates a matcher record."""
    _create_arc(42)
    mid = event_bus.register_matcher("webhook.received", arc_id=42)
    matchers = event_bus.get_matchers("webhook.received")
    assert len(matchers) == 1
    assert matchers[0]["id"] == mid
    assert matchers[0]["arc_id"] == 42


def test_process_events_matches_and_creates_work():
    """Matching event + matcher creates a work item and deletes the matcher."""
    event_bus.register_matcher("test.event")
    event_bus.record_event("test.event", {"data": "payload"})

    created = event_bus.process_events()
    assert created == 1

    # Matcher should be deleted (one-shot)
    assert len(event_bus.get_matchers("test.event")) == 0

    # Work item should exist
    db = get_db()
    try:
        items = db.execute(
            "SELECT * FROM work_queue WHERE status = 'pending'"
        ).fetchall()
        assert len(items) == 1
        assert items[0]["event_type"] == "test.event"
    finally:
        db.close()


def test_process_events_filter_match():
    """Matcher with filter only matches events whose payload contains the filter keys."""
    event_bus.register_matcher(
        "webhook.received",
        filter_json={"path": "/hooks/email"},
    )
    # Non-matching event
    event_bus.record_event("webhook.received", {"path": "/hooks/other"})
    assert event_bus.process_events() == 0

    # Matching event
    event_bus.record_event("webhook.received", {"path": "/hooks/email", "body": "..."})
    assert event_bus.process_events() == 1


def test_process_events_marks_processed():
    """Events are marked as processed after processing."""
    eid = event_bus.record_event("test.event", {})
    event_bus.process_events()
    event = event_bus.get_event(eid)
    assert event["processed"] == 1


def test_process_events_no_double_processing():
    """Already-processed events are not processed again."""
    event_bus.register_matcher("test.event")
    event_bus.record_event("test.event", {})
    assert event_bus.process_events() == 1

    # Register another matcher — old event should not trigger it
    event_bus.register_matcher("test.event")
    assert event_bus.process_events() == 0


def test_check_timeouts_creates_timeout_event():
    """Expired matchers generate timeout events."""
    _create_arc(10)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    event_bus.register_matcher(
        "webhook.received", arc_id=10, timeout_at=past,
    )

    timeouts = event_bus.check_timeouts()
    assert timeouts == 1

    # Matcher should be deleted
    assert len(event_bus.get_matchers("webhook.received")) == 0

    # Timeout event should exist
    db = get_db()
    try:
        events = db.execute(
            "SELECT * FROM events WHERE event_type = 'matcher.timeout'"
        ).fetchall()
        assert len(events) == 1
        payload = json.loads(events[0]["payload_json"])
        assert payload["arc_id"] == 10
    finally:
        db.close()


def test_check_timeouts_skips_non_expired():
    """Non-expired matchers are not timed out."""
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    event_bus.register_matcher("test.event", timeout_at=future)

    assert event_bus.check_timeouts() == 0
    assert len(event_bus.get_matchers("test.event")) == 1
