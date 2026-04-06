"""Tests for carpenter.core.trust_audit."""

from carpenter.core.trust.audit import log_trust_event, get_trust_events
from carpenter.core.arcs import manager as arc_manager


def test_log_trust_event_returns_id():
    entry_id = log_trust_event(None, "test_event", {"key": "value"})
    assert isinstance(entry_id, int)
    assert entry_id > 0


def test_log_trust_event_with_arc():
    arc_id = arc_manager.create_arc("test-arc")
    entry_id = log_trust_event(arc_id, "integrity_assigned", {"level": "untrusted"})
    assert entry_id > 0


def test_log_trust_event_no_details():
    entry_id = log_trust_event(None, "simple_event")
    assert entry_id > 0


def test_get_trust_events_returns_all():
    log_trust_event(None, "event_a", {"a": 1})
    log_trust_event(None, "event_b", {"b": 2})
    events = get_trust_events()
    # At least 2 (may have more from arc creation audit events)
    assert len(events) >= 2


def test_get_trust_events_filter_by_arc():
    arc1 = arc_manager.create_arc("arc-1")
    arc2 = arc_manager.create_arc("arc-2")
    log_trust_event(arc1, "access_denied", {"tool": "web.get"})
    log_trust_event(arc2, "access_granted", {"tool": "state.get"})

    events = get_trust_events(arc_id=arc1)
    # Should include integrity_assigned from create_arc + our access_denied
    assert all(e["arc_id"] == arc1 for e in events)
    types = [e["event_type"] for e in events]
    assert "access_denied" in types


def test_get_trust_events_filter_by_type():
    log_trust_event(None, "special_type", {"special": True})
    events = get_trust_events(event_type="special_type")
    assert len(events) >= 1
    assert all(e["event_type"] == "special_type" for e in events)


def test_get_trust_events_with_limit():
    for i in range(5):
        log_trust_event(None, "bulk_event", {"i": i})
    events = get_trust_events(event_type="bulk_event", limit=3)
    assert len(events) == 3


def test_get_trust_events_details_parsed():
    log_trust_event(None, "parsed_event", {"nested": {"a": 1}})
    events = get_trust_events(event_type="parsed_event")
    assert events[0]["details"]["nested"]["a"] == 1


def test_get_trust_events_isolation():
    """Events for one arc don't appear when filtering for another."""
    arc1 = arc_manager.create_arc("iso-1")
    arc2 = arc_manager.create_arc("iso-2")
    log_trust_event(arc1, "only_arc1")

    events = get_trust_events(arc_id=arc2, event_type="only_arc1")
    assert len(events) == 0
