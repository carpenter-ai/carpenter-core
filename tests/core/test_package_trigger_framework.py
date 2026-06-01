"""Tests for the package-trigger framework extensions (D24 / Phase 3a, PR-B).

Covers:

* Backward-compatible ``Trigger.__init__`` — legacy ``(name, config)``
  signature still works for platform-builtin and user-defined
  subclasses; new ``source_package`` / ``package_state`` kwargs default
  to ``None``.
* :class:`Trigger.emit` stamps ``_source_package`` only when the
  trigger was given one (platform triggers leave it absent).
* Constructor cross-check: ``package_state`` handle bound to a
  different package than ``source_package`` is rejected.
* :func:`registry.unregister_for_package` drops both live instances
  AND type registrations contributed by the named package, and calls
  ``stop()`` on each instance.
* :func:`registry.load_package_triggers` threads ``source_package``
  and ``package_state`` into each instantiation.
* :func:`subscriptions._source_package_matches` enforces I9 only when
  the subscription is tagged; permissive otherwise.
* End-to-end isolation: a subscription tagged for package X does NOT
  match an event emitted by package Y's trigger.
"""

from __future__ import annotations

import pytest

from carpenter.core.engine import subscriptions
from carpenter.core.engine.triggers import registry as trigger_registry
from carpenter.core.engine.triggers.base import (
    EndpointTrigger,
    PollableTrigger,
    Trigger,
)
from carpenter.packages.state import PackageStateHandle


# ── Helpers ──────────────────────────────────────────────────────────


class _DummyTrigger(Trigger):
    """Minimal concrete Trigger that does not override __init__."""

    @classmethod
    def trigger_type(cls) -> str:
        return "_pr_b_dummy"


class _LegacyTrigger(Trigger):
    """Trigger subclass that overrides __init__ with the OLD 2-arg
    signature.  Should still construct via the back-compat code path."""

    def __init__(self, name, config):
        super().__init__(name, config)
        self.legacy_constructed = True

    @classmethod
    def trigger_type(cls) -> str:
        return "_pr_b_legacy"


class _StopRecording(Trigger):
    """Trigger that records whether ``stop()`` was invoked."""

    stopped_names: list[str] = []

    def __init__(self, name, config, **kwargs):
        super().__init__(name, config, **kwargs)
        self._stopped = False

    @classmethod
    def trigger_type(cls) -> str:
        return "_pr_b_stop_recording"

    def stop(self) -> None:
        self._stopped = True
        _StopRecording.stopped_names.append(self.name)


@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset the trigger registry between tests."""
    trigger_registry.reset()
    subscriptions.reset()
    _StopRecording.stopped_names = []
    yield
    trigger_registry.reset()
    subscriptions.reset()


# ── Backward-compatible __init__ ─────────────────────────────────────


class TestTriggerInitBackcompat:
    def test_default_kwargs_are_none(self):
        t = _DummyTrigger(name="t1", config={"a": 1})
        assert t.name == "t1"
        assert t.config == {"a": 1}
        assert t.source_package is None
        assert t.package_state is None

    def test_legacy_subclass_still_works(self):
        # Subclass that uses the original 2-arg signature.
        t = _LegacyTrigger(name="legacy", config={})
        assert t.legacy_constructed is True
        # Inherited attrs default to None.
        assert t.source_package is None
        assert t.package_state is None

    def test_accepts_source_package_kwarg(self):
        t = _DummyTrigger(name="t", config={}, source_package="pkg-x")
        assert t.source_package == "pkg-x"
        assert t.package_state is None

    def test_accepts_package_state_kwarg(self):
        handle = PackageStateHandle("pkg-x")
        t = _DummyTrigger(
            name="t", config={},
            source_package="pkg-x",
            package_state=handle,
        )
        assert t.package_state is handle

    def test_rejects_mismatched_handle(self):
        bad_handle = PackageStateHandle("pkg-y")
        with pytest.raises(ValueError, match="does not match source_package"):
            _DummyTrigger(
                name="t", config={},
                source_package="pkg-x",
                package_state=bad_handle,
            )

    def test_allows_handle_without_source_package(self):
        # Defensive: if the trigger doesn't claim a source_package, we
        # don't enforce the cross-check (loader convention; the
        # installer always sets both together).
        handle = PackageStateHandle("pkg-x")
        t = _DummyTrigger(name="t", config={}, package_state=handle)
        assert t.package_state is handle
        assert t.source_package is None


# ── emit() stamps _source_package ────────────────────────────────────


class TestEmitStamping:
    def test_emit_stamps_source_package_when_set(self, db):
        # Register a one-shot bus listener via the actual event_bus.
        t = _DummyTrigger(name="t", config={}, source_package="pkg-x")
        eid = t.emit("pr_b.test", {"k": "v"})
        from carpenter.core.engine import event_bus
        event = event_bus.get_event(eid)
        import json as _json
        payload = _json.loads(event["payload_json"])
        assert payload["_source_package"] == "pkg-x"
        assert payload["_trigger"] == "t"
        assert payload["_trigger_type"] == "_pr_b_dummy"

    def test_emit_omits_source_package_when_unset(self, db):
        t = _DummyTrigger(name="t", config={})
        eid = t.emit("pr_b.test_unset", {})
        from carpenter.core.engine import event_bus
        event = event_bus.get_event(eid)
        import json as _json
        payload = _json.loads(event["payload_json"])
        assert "_source_package" not in payload


# ── load_triggers signature-detection ────────────────────────────────


class TestLoadTriggersBackcompat:
    def test_load_triggers_threads_kwargs_for_modern_subclass(self):
        trigger_registry.register_trigger_type(_DummyTrigger)
        handle = PackageStateHandle("pkg-x")
        instances = trigger_registry.load_triggers(
            [{"name": "t1", "type": "_pr_b_dummy"}],
            source_package="pkg-x",
            package_state=handle,
        )
        assert len(instances) == 1
        assert instances[0].source_package == "pkg-x"
        assert instances[0].package_state is handle

    def test_load_triggers_omits_kwargs_for_legacy_subclass(self):
        # _LegacyTrigger overrides __init__ with (name, config) only,
        # so the loader must not pass the new kwargs through (would
        # otherwise raise TypeError).
        trigger_registry.register_trigger_type(_LegacyTrigger)
        instances = trigger_registry.load_triggers(
            [{"name": "lg", "type": "_pr_b_legacy"}],
        )
        assert len(instances) == 1
        assert isinstance(instances[0], _LegacyTrigger)
        # Inherited defaults from the base __init__.
        assert instances[0].source_package is None
        assert instances[0].package_state is None

    def test_load_package_triggers_requires_source_package(self):
        with pytest.raises(ValueError):
            trigger_registry.load_package_triggers(
                [{"name": "x", "type": "_pr_b_dummy"}],
                source_package="",
            )


# ── unregister_for_package ───────────────────────────────────────────


class TestUnregisterForPackage:
    def test_drops_instances_and_types_tagged_to_package(self):
        trigger_registry.register_trigger_type(
            _DummyTrigger, source_package="pkg-x",
        )
        trigger_registry.load_triggers(
            [{"name": "t-x", "type": "_pr_b_dummy"}],
            source_package="pkg-x",
        )
        assert trigger_registry.get_trigger_type("_pr_b_dummy") is _DummyTrigger
        assert len(trigger_registry.get_trigger_instances()) == 1

        removed = trigger_registry.unregister_for_package("pkg-x")
        # One instance + one type.
        assert removed == 2
        assert trigger_registry.get_trigger_type("_pr_b_dummy") is None
        assert trigger_registry.get_trigger_instances() == []

    def test_leaves_other_packages_alone(self):
        # _DummyTrigger contributed by pkg-x; _StopRecording by pkg-y.
        trigger_registry.register_trigger_type(
            _DummyTrigger, source_package="pkg-x",
        )
        trigger_registry.register_trigger_type(
            _StopRecording, source_package="pkg-y",
        )
        trigger_registry.load_triggers(
            [{"name": "t-x", "type": "_pr_b_dummy"}],
            source_package="pkg-x",
        )
        trigger_registry.load_triggers(
            [{"name": "t-y", "type": "_pr_b_stop_recording"}],
            source_package="pkg-y",
        )

        trigger_registry.unregister_for_package("pkg-x")

        # pkg-y survives.
        assert trigger_registry.get_trigger_type("_pr_b_stop_recording") is _StopRecording
        survivors = trigger_registry.get_trigger_instances()
        assert len(survivors) == 1
        assert survivors[0].name == "t-y"
        assert survivors[0].source_package == "pkg-y"

    def test_calls_stop_on_each_instance(self):
        trigger_registry.register_trigger_type(
            _StopRecording, source_package="pkg-z",
        )
        trigger_registry.load_triggers(
            [
                {"name": "z1", "type": "_pr_b_stop_recording"},
                {"name": "z2", "type": "_pr_b_stop_recording"},
            ],
            source_package="pkg-z",
        )
        trigger_registry.unregister_for_package("pkg-z")
        assert sorted(_StopRecording.stopped_names) == ["z1", "z2"]

    def test_idempotent_on_empty(self):
        assert trigger_registry.unregister_for_package("pkg-nonexistent") == 0

    def test_empty_name_is_noop(self):
        assert trigger_registry.unregister_for_package("") == 0


# ── Subscription source_package cross-check ──────────────────────────


class TestSubscriptionCrossCheck:
    def test_untagged_sub_matches_anything(self):
        sub = subscriptions.Subscription(
            name="any", event_type="x", source_package=None,
        )
        assert subscriptions._source_package_matches(sub, {}) is True
        assert subscriptions._source_package_matches(
            sub, {"_source_package": "pkg-z"},
        ) is True

    def test_tagged_sub_matches_same_package_event(self):
        sub = subscriptions.Subscription(
            name="x-only", event_type="e",
            source_package="pkg-x",
        )
        assert subscriptions._source_package_matches(
            sub, {"_source_package": "pkg-x"},
        ) is True

    def test_tagged_sub_rejects_other_package_event(self):
        sub = subscriptions.Subscription(
            name="x-only", event_type="e",
            source_package="pkg-x",
        )
        assert subscriptions._source_package_matches(
            sub, {"_source_package": "pkg-y"},
        ) is False

    def test_tagged_sub_permissive_on_untagged_event(self):
        # Permissive: a packaged subscription can still match events
        # that originate from non-package sources (HTTP webhooks, raw
        # event_bus calls).  This preserves the B-full
        # trigger_subscriptions "respond to external event" pattern.
        sub = subscriptions.Subscription(
            name="x-only", event_type="e",
            source_package="pkg-x",
        )
        assert subscriptions._source_package_matches(sub, {}) is True


# ── End-to-end cross-package isolation ───────────────────────────────


class TestEndToEndIsolation:
    def test_subscription_for_pkg_x_does_not_fire_on_pkg_y_event(self, db):
        # Two packages, each with a packaged subscription tagged with
        # its source_package, both subscribing to the same event_type.
        # Then pkg-y's trigger fires a tagged event.  Only pkg-y's
        # subscription should match.
        sub_x = subscriptions.Subscription(
            name="_pkg.pkg-x.0",
            event_type="cross.test",
            action_type="enqueue_work",
            action_config={"event_type": "x.received"},
            source_package="pkg-x",
        )
        sub_y = subscriptions.Subscription(
            name="_pkg.pkg-y.0",
            event_type="cross.test",
            action_type="enqueue_work",
            action_config={"event_type": "y.received"},
            source_package="pkg-y",
        )
        subscriptions._subscriptions.append(sub_x)
        subscriptions._subscriptions.append(sub_y)

        # pkg-y's trigger emits a tagged event.
        trigger_registry.register_trigger_type(
            _DummyTrigger, source_package="pkg-y",
        )
        trig = _DummyTrigger(
            name="t-y", config={}, source_package="pkg-y",
        )
        trig.emit("cross.test", {"data": 1})

        created = subscriptions.process_subscriptions()
        # Exactly one subscription (pkg-y's) should have fired.
        assert created == 1

        from carpenter.db import get_db
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT event_type FROM work_queue "
                "WHERE idempotency_key LIKE 'sub-%'"
            ).fetchall()
        finally:
            conn.close()
        event_types = [r["event_type"] for r in rows]
        assert event_types == ["y.received"]
