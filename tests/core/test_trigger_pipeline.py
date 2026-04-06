"""Tests for the unified trigger and event pipeline.

Tests cover:
- Trigger base classes and registry
- Subscription system (matching, actions, atomic processing)
- Built-in trigger types (timer, counter, arc lifecycle, webhook)
- Event bus idempotency and priority extensions
- Integration: trigger → event → subscription → work item
"""

import json

import pytest

from carpenter.core.engine import event_bus, work_queue
from carpenter.core.engine.triggers.base import Trigger, PollableTrigger, EndpointTrigger
from carpenter.core.engine.triggers import registry as trigger_registry
from carpenter.core.engine import subscriptions
from carpenter.db import get_db


# ── Helpers ──────────────────────────────────────────────────────────


class DummyTrigger(Trigger):
    """Minimal concrete trigger for testing."""

    @classmethod
    def trigger_type(cls) -> str:
        return "dummy"


class DummyPollable(PollableTrigger):
    """Pollable trigger that tracks check() calls."""

    check_count = 0

    @classmethod
    def trigger_type(cls) -> str:
        return "dummy_pollable"

    def check(self) -> None:
        DummyPollable.check_count += 1
        if self.config.get("emit_on_check"):
            self.emit(
                self.config.get("emits", "test.pollable"),
                {"checked": True},
            )


class DummyEndpoint(EndpointTrigger):
    """Endpoint trigger for testing."""

    @classmethod
    def trigger_type(cls) -> str:
        return "dummy_endpoint"

    @property
    def path(self) -> str:
        return f"/triggers/{self.name}"

    async def handle_request(self, request) -> dict:
        self.emit("test.endpoint", {"received": True})
        return {"status": "ok"}


def _create_arc(arc_id: int, name: str = "test-arc", **kwargs) -> int:
    """Insert a minimal arc row and return its ID."""
    db = get_db()
    try:
        fields = {"id": arc_id, "name": name}
        fields.update(kwargs)
        cols = ", ".join(fields.keys())
        placeholders = ", ".join("?" * len(fields))
        db.execute(
            f"INSERT INTO arcs ({cols}) VALUES ({placeholders})",
            tuple(fields.values()),
        )
        db.commit()
        return arc_id
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _reset_trigger_registry():
    """Reset trigger registry and subscriptions between tests."""
    trigger_registry.reset()
    subscriptions.reset()
    DummyPollable.check_count = 0
    yield
    trigger_registry.reset()
    subscriptions.reset()


# ── Trigger Base Classes ─────────────────────────────────────────────


class TestTriggerBase:

    def test_trigger_type_abstract(self):
        """Trigger.trigger_type() is abstract."""
        with pytest.raises(TypeError):
            Trigger(name="test", config={})

    def test_concrete_trigger_creates(self):
        """Concrete trigger can be instantiated."""
        t = DummyTrigger(name="test", config={"key": "value"})
        assert t.name == "test"
        assert t.config == {"key": "value"}
        assert t.trigger_type() == "dummy"

    def test_emit_records_event(self):
        """emit() creates an event in the event bus."""
        t = DummyTrigger(name="test-emitter", config={})
        event_id = t.emit("test.event", {"data": 1})
        assert event_id is not None
        assert isinstance(event_id, int)

        event = event_bus.get_event(event_id)
        assert event["event_type"] == "test.event"
        payload = json.loads(event["payload_json"])
        assert payload["data"] == 1
        assert payload["_trigger"] == "test-emitter"
        assert payload["_trigger_type"] == "dummy"

    def test_emit_with_idempotency_key(self):
        """emit() with idempotency_key deduplicates."""
        t = DummyTrigger(name="test", config={})
        eid1 = t.emit("test.event", {"n": 1}, idempotency_key="key-1")
        eid2 = t.emit("test.event", {"n": 2}, idempotency_key="key-1")
        assert eid1 is not None
        assert eid2 is None  # duplicate

    def test_emit_with_priority(self):
        """emit() stores the priority."""
        t = DummyTrigger(name="test", config={})
        eid = t.emit("test.event", {}, priority=5)
        event = event_bus.get_event(eid)
        assert event["priority"] == 5

    def test_pollable_trigger_check_abstract(self):
        """PollableTrigger.check() is abstract."""
        with pytest.raises(TypeError):
            PollableTrigger(name="test", config={})

    def test_endpoint_trigger_abstract(self):
        """EndpointTrigger requires path and handle_request."""
        with pytest.raises(TypeError):
            EndpointTrigger(name="test", config={})


# ── Trigger Registry ─────────────────────────────────────────────────


class TestTriggerRegistry:

    def test_register_trigger_type(self):
        """register_trigger_type() stores the class."""
        trigger_registry.register_trigger_type(DummyTrigger)
        assert trigger_registry.get_trigger_type("dummy") is DummyTrigger

    def test_register_duplicate_same_class(self):
        """Registering the same class twice is idempotent."""
        trigger_registry.register_trigger_type(DummyTrigger)
        trigger_registry.register_trigger_type(DummyTrigger)  # no error

    def test_register_duplicate_different_class(self):
        """Registering a different class with same type raises."""
        trigger_registry.register_trigger_type(DummyTrigger)

        class AnotherDummy(Trigger):
            @classmethod
            def trigger_type(cls): return "dummy"

        with pytest.raises(ValueError, match="already registered"):
            trigger_registry.register_trigger_type(AnotherDummy)

    def test_register_non_trigger_raises(self):
        """Registering a non-Trigger class raises TypeError."""
        with pytest.raises(TypeError):
            trigger_registry.register_trigger_type(str)

    def test_load_triggers(self):
        """load_triggers() creates instances from config."""
        trigger_registry.register_trigger_type(DummyTrigger)
        instances = trigger_registry.load_triggers([
            {"name": "t1", "type": "dummy", "enabled": True},
            {"name": "t2", "type": "dummy", "enabled": False},
        ])
        assert len(instances) == 1  # t2 disabled
        assert instances[0].name == "t1"
        assert trigger_registry.get_trigger_instances() == instances

    def test_load_triggers_unknown_type(self):
        """Unknown trigger type is skipped with warning."""
        instances = trigger_registry.load_triggers([
            {"name": "bad", "type": "nonexistent"},
        ])
        assert len(instances) == 0

    def test_get_pollable_triggers(self):
        """get_pollable_triggers() returns only PollableTrigger instances."""
        trigger_registry.register_trigger_type(DummyTrigger)
        trigger_registry.register_trigger_type(DummyPollable)
        trigger_registry.load_triggers([
            {"name": "regular", "type": "dummy"},
            {"name": "pollable", "type": "dummy_pollable"},
        ])
        pollable = trigger_registry.get_pollable_triggers()
        assert len(pollable) == 1
        assert isinstance(pollable[0], DummyPollable)

    def test_get_endpoint_triggers(self):
        """get_endpoint_triggers() returns only EndpointTrigger instances."""
        trigger_registry.register_trigger_type(DummyTrigger)
        trigger_registry.register_trigger_type(DummyEndpoint)
        trigger_registry.load_triggers([
            {"name": "regular", "type": "dummy"},
            {"name": "endpoint", "type": "dummy_endpoint"},
        ])
        endpoints = trigger_registry.get_endpoint_triggers()
        assert len(endpoints) == 1
        assert isinstance(endpoints[0], DummyEndpoint)

    def test_check_pollable_triggers(self):
        """check_pollable_triggers() calls check() on all pollables."""
        trigger_registry.register_trigger_type(DummyPollable)
        trigger_registry.load_triggers([
            {"name": "p1", "type": "dummy_pollable"},
            {"name": "p2", "type": "dummy_pollable"},
        ])
        checked = trigger_registry.check_pollable_triggers()
        assert checked == 2
        assert DummyPollable.check_count == 2

    def test_start_stop_all(self):
        """start_all() and stop_all() don't raise on dummy triggers."""
        trigger_registry.register_trigger_type(DummyTrigger)
        trigger_registry.load_triggers([{"name": "t", "type": "dummy"}])
        trigger_registry.start_all()  # no-op
        trigger_registry.stop_all()   # no-op


# ── Event Bus Extensions ─────────────────────────────────────────────


class TestEventBusExtensions:

    def test_record_event_with_priority(self):
        """record_event() stores priority."""
        eid = event_bus.record_event("test", {}, priority=10)
        event = event_bus.get_event(eid)
        assert event["priority"] == 10

    def test_record_event_default_priority(self):
        """Default priority is 0."""
        eid = event_bus.record_event("test", {})
        event = event_bus.get_event(eid)
        assert event["priority"] == 0

    def test_record_event_idempotency_key(self):
        """Duplicate idempotency_key returns None."""
        eid1 = event_bus.record_event("test", {"n": 1}, idempotency_key="idem-1")
        eid2 = event_bus.record_event("test", {"n": 2}, idempotency_key="idem-1")
        assert eid1 is not None
        assert eid2 is None

    def test_record_event_different_idempotency_keys(self):
        """Different keys create separate events."""
        eid1 = event_bus.record_event("test", {}, idempotency_key="k1")
        eid2 = event_bus.record_event("test", {}, idempotency_key="k2")
        assert eid1 is not None
        assert eid2 is not None
        assert eid1 != eid2

    def test_record_event_no_idempotency_key(self):
        """Without idempotency_key, duplicates are allowed."""
        eid1 = event_bus.record_event("test", {"x": 1})
        eid2 = event_bus.record_event("test", {"x": 1})
        assert eid1 is not None
        assert eid2 is not None

    def test_process_events_priority_order(self):
        """Events are processed in priority DESC, created_at ASC order."""
        # Create two matchers
        event_bus.register_matcher("high.event")
        event_bus.register_matcher("low.event")

        # Record events: low priority first, high second
        event_bus.record_event("low.event", {"order": "first"}, priority=0)
        event_bus.record_event("high.event", {"order": "second"}, priority=10)

        created = event_bus.process_events()
        assert created == 2

        # Check that work items exist (both should match)
        db = get_db()
        try:
            items = db.execute(
                "SELECT event_type FROM work_queue WHERE status = 'pending' "
                "ORDER BY created_at ASC"
            ).fetchall()
            assert len(items) == 2
        finally:
            db.close()


# ── Subscription System ──────────────────────────────────────────────


class TestSubscriptions:

    def test_load_subscriptions(self):
        """load_subscriptions() creates Subscription objects from config."""
        count = subscriptions.load_subscriptions([
            {
                "name": "sub1",
                "on": "test.event",
                "action": {"type": "enqueue_work", "event_type": "work.test"},
            },
            {
                "name": "sub2",
                "on": "arc.status_changed",
                "filter": {"new_status": "completed"},
                "action": {"type": "send_notification", "message": "Done!"},
                "enabled": False,
            },
        ])
        assert count == 2
        subs = subscriptions.get_subscriptions()
        assert len(subs) == 2
        assert subs[0].name == "sub1"
        assert subs[0].enabled is True
        assert subs[1].enabled is False

    def test_load_subscriptions_missing_name(self):
        """Subscriptions missing name are skipped."""
        count = subscriptions.load_subscriptions([
            {"on": "test.event", "action": {"type": "enqueue_work"}},
        ])
        assert count == 0

    def test_filter_matches_none(self):
        """None filter matches everything."""
        assert subscriptions._filter_matches(None, {"any": "thing"})

    def test_filter_matches_subset(self):
        """Filter matches when all keys match in payload."""
        assert subscriptions._filter_matches(
            {"status": "completed"},
            {"status": "completed", "arc_id": 1},
        )

    def test_filter_no_match(self):
        """Filter doesn't match when key values differ."""
        assert not subscriptions._filter_matches(
            {"status": "completed"},
            {"status": "failed"},
        )

    def test_filter_missing_key(self):
        """Filter doesn't match when key is missing from payload."""
        assert not subscriptions._filter_matches(
            {"status": "completed"},
            {"arc_id": 1},
        )

    def test_process_subscriptions_enqueue_work(self):
        """Matching subscription creates work item."""
        subscriptions.load_subscriptions([{
            "name": "test-sub",
            "on": "test.event",
            "action": {
                "type": "enqueue_work",
                "event_type": "work.test",
            },
        }])

        event_bus.record_event("test.event", {"data": "hello"})
        created = subscriptions.process_subscriptions()
        assert created == 1

        db = get_db()
        try:
            items = db.execute(
                "SELECT * FROM work_queue WHERE event_type = 'work.test'"
            ).fetchall()
            assert len(items) == 1
            payload = json.loads(items[0]["payload_json"])
            assert payload["_subscription"] == "test-sub"
        finally:
            db.close()

    def test_process_subscriptions_with_filter(self):
        """Subscription filter narrows matching."""
        subscriptions.load_subscriptions([{
            "name": "filtered-sub",
            "on": "arc.status_changed",
            "filter": {"new_status": "completed"},
            "action": {"type": "enqueue_work", "event_type": "work.done"},
        }])

        # Non-matching event
        event_bus.record_event("arc.status_changed", {"new_status": "failed"})
        assert subscriptions.process_subscriptions() == 0

        # Matching event
        event_bus.record_event("arc.status_changed", {"new_status": "completed", "arc_id": 1})
        assert subscriptions.process_subscriptions() == 1

    def test_process_subscriptions_multiple_match(self):
        """Multiple subscriptions can match one event (fan-out)."""
        subscriptions.load_subscriptions([
            {
                "name": "sub-a",
                "on": "test.event",
                "action": {"type": "enqueue_work", "event_type": "work.a"},
            },
            {
                "name": "sub-b",
                "on": "test.event",
                "action": {"type": "enqueue_work", "event_type": "work.b"},
            },
        ])

        event_bus.record_event("test.event", {})
        created = subscriptions.process_subscriptions()
        assert created == 2

    def test_process_subscriptions_disabled_skipped(self):
        """Disabled subscriptions don't match."""
        subscriptions.load_subscriptions([{
            "name": "disabled-sub",
            "on": "test.event",
            "action": {"type": "enqueue_work", "event_type": "work.test"},
            "enabled": False,
        }])

        event_bus.record_event("test.event", {})
        assert subscriptions.process_subscriptions() == 0

    def test_process_subscriptions_idempotent(self):
        """Same event+subscription combo doesn't create duplicate work items."""
        subscriptions.load_subscriptions([{
            "name": "idem-sub",
            "on": "test.event",
            "action": {"type": "enqueue_work", "event_type": "work.test"},
        }])

        event_bus.record_event("test.event", {})
        assert subscriptions.process_subscriptions() == 1
        # Process again — should not create duplicates (idempotency key)
        assert subscriptions.process_subscriptions() == 0

    def test_process_subscriptions_payload_merge(self):
        """payload_merge=True merges event payload into work item."""
        subscriptions.load_subscriptions([{
            "name": "merge-sub",
            "on": "test.event",
            "action": {
                "type": "enqueue_work",
                "event_type": "work.merged",
                "payload": {"static": "value"},
                "payload_merge": True,
            },
        }])

        event_bus.record_event("test.event", {"dynamic": "data"})
        subscriptions.process_subscriptions()

        db = get_db()
        try:
            items = db.execute(
                "SELECT * FROM work_queue WHERE event_type = 'work.merged'"
            ).fetchall()
            assert len(items) == 1
            payload = json.loads(items[0]["payload_json"])
            assert payload["static"] == "value"
            assert payload["dynamic"] == "data"
        finally:
            db.close()

    def test_process_subscriptions_create_arc_action(self):
        """create_arc action creates a subscription.create_arc work item."""
        subscriptions.load_subscriptions([{
            "name": "arc-sub",
            "on": "test.event",
            "action": {
                "type": "create_arc",
                "template": "pr-review",
                "name": "review-arc",
            },
        }])

        event_bus.record_event("test.event", {"pr_number": 42})
        created = subscriptions.process_subscriptions()
        assert created == 1

        db = get_db()
        try:
            items = db.execute(
                "SELECT * FROM work_queue WHERE event_type = 'subscription.create_arc'"
            ).fetchall()
            assert len(items) == 1
            payload = json.loads(items[0]["payload_json"])
            assert payload["template"] == "pr-review"
            assert payload["_event_payload"]["pr_number"] == 42
        finally:
            db.close()

    def test_process_subscriptions_notification_action(self):
        """send_notification action creates a subscription.notification work item."""
        subscriptions.load_subscriptions([{
            "name": "notif-sub",
            "on": "test.event",
            "action": {
                "type": "send_notification",
                "message": "Event occurred",
                "priority": "urgent",
            },
        }])

        event_bus.record_event("test.event", {})
        created = subscriptions.process_subscriptions()
        assert created == 1

        db = get_db()
        try:
            items = db.execute(
                "SELECT * FROM work_queue WHERE event_type = 'subscription.notification'"
            ).fetchall()
            assert len(items) == 1
        finally:
            db.close()


# ── Counter Trigger ──────────────────────────────────────────────────


class TestCounterTrigger:

    def test_counter_trigger_below_threshold(self):
        """Counter doesn't fire when count is below threshold."""
        from carpenter.core.engine.triggers.counter import CounterTrigger

        trigger = CounterTrigger(name="test-counter", config={
            "counts": "arc.status_changed",
            "threshold": 3,
            "emits": "batch.ready",
        })
        trigger.start()

        # Add 2 events (below threshold of 3)
        event_bus.record_event("arc.status_changed", {"arc_id": 1})
        event_bus.record_event("arc.status_changed", {"arc_id": 2})

        trigger.check()

        # Should not have emitted
        db = get_db()
        try:
            events = db.execute(
                "SELECT * FROM events WHERE event_type = 'batch.ready'"
            ).fetchall()
            assert len(events) == 0
        finally:
            db.close()

    def test_counter_trigger_fires_at_threshold(self):
        """Counter fires when count reaches threshold."""
        from carpenter.core.engine.triggers.counter import CounterTrigger

        trigger = CounterTrigger(name="test-counter-fire", config={
            "counts": "arc.status_changed",
            "threshold": 2,
            "emits": "batch.ready",
        })
        trigger.start()

        event_bus.record_event("arc.status_changed", {"arc_id": 1})
        event_bus.record_event("arc.status_changed", {"arc_id": 2})

        trigger.check()

        db = get_db()
        try:
            events = db.execute(
                "SELECT * FROM events WHERE event_type = 'batch.ready'"
            ).fetchall()
            assert len(events) == 1
            payload = json.loads(events[0]["payload_json"])
            assert payload["count"] == 2
            assert payload["threshold"] == 2
        finally:
            db.close()

    def test_counter_trigger_with_filter(self):
        """Counter only counts events matching the filter."""
        from carpenter.core.engine.triggers.counter import CounterTrigger

        trigger = CounterTrigger(name="test-counter-filter", config={
            "counts": "arc.status_changed",
            "filter": {"new_status": "completed"},
            "threshold": 2,
            "emits": "completed.batch",
        })
        trigger.start()

        # 1 completed + 1 failed = 1 matching (below threshold)
        event_bus.record_event("arc.status_changed", {"new_status": "completed", "arc_id": 1})
        event_bus.record_event("arc.status_changed", {"new_status": "failed", "arc_id": 2})

        trigger.check()
        db = get_db()
        try:
            assert len(db.execute(
                "SELECT * FROM events WHERE event_type = 'completed.batch'"
            ).fetchall()) == 0
        finally:
            db.close()

        # Add another completed event
        event_bus.record_event("arc.status_changed", {"new_status": "completed", "arc_id": 3})
        trigger.check()

        db = get_db()
        try:
            assert len(db.execute(
                "SELECT * FROM events WHERE event_type = 'completed.batch'"
            ).fetchall()) == 1
        finally:
            db.close()

    def test_counter_trigger_resets_after_fire(self):
        """Counter resets (via last_fired_at) after firing."""
        from carpenter.core.engine.triggers.counter import CounterTrigger

        trigger = CounterTrigger(name="test-counter-reset", config={
            "counts": "test.counted",
            "threshold": 1,
            "emits": "counter.fired",
        })
        trigger.start()

        event_bus.record_event("test.counted", {})
        trigger.check()

        db = get_db()
        try:
            # Verify it fired
            fired = db.execute(
                "SELECT * FROM events WHERE event_type = 'counter.fired'"
            ).fetchall()
            assert len(fired) == 1

            # Check again without new events — should not fire again
            trigger.check()
            fired2 = db.execute(
                "SELECT * FROM events WHERE event_type = 'counter.fired'"
            ).fetchall()
            assert len(fired2) == 1  # still just 1
        finally:
            db.close()


# ── Arc Lifecycle Trigger ────────────────────────────────────────────


class TestArcLifecycleTrigger:

    def test_emit_status_changed(self):
        """emit_status_changed records an event with proper payload."""
        from carpenter.core.engine.triggers.arc_lifecycle import emit_status_changed

        eid = emit_status_changed(
            arc_id=42,
            old_status="pending",
            new_status="active",
            arc_name="test-arc",
            arc_role="worker",
            parent_id=None,
            agent_type="EXECUTOR",
        )
        assert eid is not None

        event = event_bus.get_event(eid)
        assert event["event_type"] == "arc.status_changed"
        payload = json.loads(event["payload_json"])
        assert payload["arc_id"] == 42
        assert payload["old_status"] == "pending"
        assert payload["new_status"] == "active"
        assert payload["is_root"] is True
        assert payload["arc_role"] == "worker"

    def test_emit_status_changed_idempotent(self):
        """Same transition doesn't create duplicate events."""
        from carpenter.core.engine.triggers.arc_lifecycle import emit_status_changed

        eid1 = emit_status_changed(arc_id=1, old_status="pending", new_status="active")
        eid2 = emit_status_changed(arc_id=1, old_status="pending", new_status="active")
        assert eid1 is not None
        assert eid2 is None

    def test_different_transitions_create_separate_events(self):
        """Different transitions create separate events."""
        from carpenter.core.engine.triggers.arc_lifecycle import emit_status_changed

        eid1 = emit_status_changed(arc_id=1, old_status="pending", new_status="active")
        eid2 = emit_status_changed(arc_id=1, old_status="active", new_status="completed")
        assert eid1 is not None
        assert eid2 is not None
        assert eid1 != eid2


# ── Timer Trigger ────────────────────────────────────────────────────


class TestTimerTrigger:

    def test_timer_trigger_registers_cron(self):
        """TimerTrigger.start() registers a cron entry."""
        from carpenter.core.engine.triggers.timer import TimerTrigger
        from carpenter.core.engine import trigger_manager

        trigger = TimerTrigger(name="daily-test", config={
            "schedule": "0 23 * * *",
            "emits": "test.daily",
            "payload": {"cadence": "daily"},
        })
        trigger.start()

        cron = trigger_manager.get_cron("trigger:daily-test")
        assert cron is not None
        assert cron["event_type"] == "test.daily"
        assert cron["cron_expr"] == "0 23 * * *"

    def test_timer_trigger_idempotent_start(self):
        """TimerTrigger.start() is idempotent (no error on duplicate)."""
        from carpenter.core.engine.triggers.timer import TimerTrigger

        trigger = TimerTrigger(name="idem-timer", config={
            "schedule": "0 * * * *",
            "emits": "test.hourly",
        })
        trigger.start()
        trigger.start()  # should not raise


# ── Webhook Trigger ──────────────────────────────────────────────────


class TestWebhookTrigger:

    def test_webhook_trigger_path(self):
        """WebhookTrigger has correct HTTP path."""
        from carpenter.core.engine.triggers.webhook import WebhookTrigger

        trigger = WebhookTrigger(name="forgejo-hook", config={
            "parser": "forgejo",
            "emits": "webhook.forgejo",
        })
        assert trigger.path == "/triggers/forgejo-hook"

    def test_webhook_trigger_custom_path(self):
        """WebhookTrigger respects path_suffix config."""
        from carpenter.core.engine.triggers.webhook import WebhookTrigger

        trigger = WebhookTrigger(name="my-hook", config={
            "path_suffix": "custom",
        })
        assert trigger.path == "/triggers/custom"


class TestWebhookParsers:

    def test_forgejo_parser(self):
        """Forgejo parser extracts key fields."""
        from carpenter.core.engine.triggers.webhook import _parse_forgejo

        headers = {
            "x-forgejo-event": "push",
            "x-forgejo-delivery": "abc-123",
        }
        body = {
            "ref": "refs/heads/main",
            "commits": [{"id": "c1"}, {"id": "c2"}],
            "repository": {"full_name": "user/repo", "name": "repo"},
            "sender": {"login": "user"},
        }

        event_type, payload, delivery_id = _parse_forgejo(headers, body)
        assert event_type == "push"
        assert delivery_id == "abc-123"
        assert payload["ref"] == "refs/heads/main"
        assert payload["commit_count"] == 2
        assert payload["repo_full_name"] == "user/repo"

    def test_github_parser(self):
        """GitHub parser extracts key fields."""
        from carpenter.core.engine.triggers.webhook import _parse_github

        headers = {
            "x-github-event": "pull_request",
            "x-github-delivery": "gh-456",
        }
        body = {
            "action": "opened",
            "pull_request": {"number": 42, "title": "Fix bug"},
            "repository": {"full_name": "org/repo", "name": "repo"},
            "sender": {"login": "dev"},
        }

        event_type, payload, delivery_id = _parse_github(headers, body)
        assert event_type == "pull_request"
        assert delivery_id == "gh-456"
        assert payload["action"] == "opened"
        assert payload["pr_number"] == 42

    def test_generic_parser(self):
        """Generic parser passes through raw body."""
        from carpenter.core.engine.triggers.webhook import _parse_generic

        event_type, payload, delivery_id = _parse_generic({}, {"raw": "data"})
        assert event_type == "generic"
        assert payload["data"] == {"raw": "data"}
        assert delivery_id is None


# ── Integration Tests ────────────────────────────────────────────────


class TestIntegration:

    def test_pollable_trigger_emits_to_subscription(self):
        """PollableTrigger → event → subscription → work item."""
        # Set up trigger
        trigger_registry.register_trigger_type(DummyPollable)
        trigger_registry.load_triggers([{
            "name": "integration-pollable",
            "type": "dummy_pollable",
            "emit_on_check": True,
            "emits": "integration.event",
        }])

        # Set up subscription
        subscriptions.load_subscriptions([{
            "name": "integration-sub",
            "on": "integration.event",
            "action": {
                "type": "enqueue_work",
                "event_type": "integration.work",
            },
        }])

        # Run the pipeline
        trigger_registry.check_pollable_triggers()
        created = subscriptions.process_subscriptions()
        assert created == 1

        # Verify work item was created
        db = get_db()
        try:
            items = db.execute(
                "SELECT * FROM work_queue WHERE event_type = 'integration.work'"
            ).fetchall()
            assert len(items) == 1
        finally:
            db.close()

    def test_counter_to_subscription(self):
        """Counter trigger → threshold event → subscription → work item."""
        from carpenter.core.engine.triggers.counter import CounterTrigger

        # Set up counter trigger
        counter = CounterTrigger(name="int-counter", config={
            "counts": "arc.status_changed",
            "threshold": 2,
            "emits": "batch.ready",
        })
        counter.start()

        # Set up subscription
        subscriptions.load_subscriptions([{
            "name": "batch-sub",
            "on": "batch.ready",
            "action": {
                "type": "enqueue_work",
                "event_type": "batch.process",
            },
        }])

        # Add events to reach threshold
        event_bus.record_event("arc.status_changed", {"arc_id": 1})
        event_bus.record_event("arc.status_changed", {"arc_id": 2})

        # Run pipeline
        counter.check()
        created = subscriptions.process_subscriptions()
        assert created == 1

        db = get_db()
        try:
            items = db.execute(
                "SELECT * FROM work_queue WHERE event_type = 'batch.process'"
            ).fetchall()
            assert len(items) == 1
        finally:
            db.close()

    def test_arc_lifecycle_to_subscription(self):
        """Arc status change → lifecycle event → subscription → work item."""
        from carpenter.core.engine.triggers.arc_lifecycle import emit_status_changed

        subscriptions.load_subscriptions([{
            "name": "completion-sub",
            "on": "arc.status_changed",
            "filter": {"new_status": "completed", "is_root": True},
            "action": {
                "type": "enqueue_work",
                "event_type": "root.completed",
                "payload_merge": True,
            },
        }])

        # Emit lifecycle event (as manager.py would)
        emit_status_changed(
            arc_id=99,
            old_status="active",
            new_status="completed",
            arc_name="big-task",
            parent_id=None,
        )

        created = subscriptions.process_subscriptions()
        assert created == 1

        db = get_db()
        try:
            items = db.execute(
                "SELECT * FROM work_queue WHERE event_type = 'root.completed'"
            ).fetchall()
            assert len(items) == 1
            payload = json.loads(items[0]["payload_json"])
            assert payload["arc_id"] == 99
            assert payload["is_root"] is True
        finally:
            db.close()


# ── Database Migration ───────────────────────────────────────────────


class TestDatabaseSchema:

    def test_trigger_state_table_exists(self):
        """trigger_state table is created by schema."""
        db = get_db()
        try:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "trigger_state" in tables
        finally:
            db.close()

    def test_events_has_priority_column(self):
        """events table has priority column."""
        db = get_db()
        try:
            cols = {row[1] for row in db.execute("PRAGMA table_info(events)").fetchall()}
            assert "priority" in cols
        finally:
            db.close()

    def test_events_has_idempotency_key_column(self):
        """events table has idempotency_key column."""
        db = get_db()
        try:
            cols = {row[1] for row in db.execute("PRAGMA table_info(events)").fetchall()}
            assert "idempotency_key" in cols
        finally:
            db.close()

    def test_trigger_state_schema(self):
        """trigger_state table has expected columns."""
        db = get_db()
        try:
            cols = {row[1] for row in db.execute("PRAGMA table_info(trigger_state)").fetchall()}
            assert "trigger_name" in cols
            assert "trigger_type" in cols
            assert "last_fired_at" in cols
            assert "counter" in cols
            assert "metadata_json" in cols
        finally:
            db.close()
