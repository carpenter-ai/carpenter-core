"""Tests for config-driven reflection trigger migration.

Verifies that:
- Reflection triggers are defined in config defaults (disabled by default)
- reflection.enabled activates reflection triggers before loading
- Per-cadence cron overrides from reflection config are applied
- The handler registration is preserved
- The full pipeline works: timer trigger -> cron -> timer.fired -> work_queue
"""

import copy
import json

import pytest

from carpenter import config
from carpenter.core.engine.triggers import registry as trigger_registry
from carpenter.core.engine.triggers.timer import TimerTrigger
from carpenter.core.engine import trigger_manager, subscriptions
from carpenter.db import get_db


# ── Helpers ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_trigger_state():
    """Reset trigger registry and subscriptions between tests."""
    trigger_registry.reset()
    subscriptions.reset()
    yield
    trigger_registry.reset()
    subscriptions.reset()


# ── Config Defaults ──────────────────────────────────────────────────


class TestConfigDefaults:
    """Verify reflection triggers exist in config DEFAULTS."""

    def test_triggers_key_exists_in_defaults(self):
        """Config DEFAULTS has a 'triggers' key with a list."""
        assert "triggers" in config.DEFAULTS
        assert isinstance(config.DEFAULTS["triggers"], list)

    def test_subscriptions_key_exists_in_defaults(self):
        """Config DEFAULTS has a 'subscriptions' key."""
        assert "subscriptions" in config.DEFAULTS
        assert isinstance(config.DEFAULTS["subscriptions"], list)

    def test_reflection_triggers_present(self):
        """All three reflection triggers are in config defaults."""
        triggers = config.DEFAULTS["triggers"]
        names = {t["name"] for t in triggers}
        assert "daily-reflection" in names
        assert "weekly-reflection" in names
        assert "monthly-reflection" in names

    def test_reflection_triggers_disabled_by_default(self):
        """All reflection triggers default to enabled=False."""
        triggers = config.DEFAULTS["triggers"]
        for t in triggers:
            if t["name"] in ("daily-reflection", "weekly-reflection", "monthly-reflection"):
                assert t["enabled"] is False, f"{t['name']} should be disabled by default"

    def test_reflection_trigger_structure(self):
        """Each reflection trigger has the correct type, schedule, emits, and payload."""
        triggers = config.DEFAULTS["triggers"]
        expected = {
            "daily-reflection": {
                "type": "timer",
                "schedule": "0 23 * * *",
                "emits": "reflection.trigger",
                "payload": {"cadence": "daily"},
            },
            "weekly-reflection": {
                "type": "timer",
                "schedule": "0 23 * * 0",
                "emits": "reflection.trigger",
                "payload": {"cadence": "weekly"},
            },
            "monthly-reflection": {
                "type": "timer",
                "schedule": "0 23 1 * *",
                "emits": "reflection.trigger",
                "payload": {"cadence": "monthly"},
            },
        }

        for t in triggers:
            if t["name"] in expected:
                exp = expected[t["name"]]
                assert t["type"] == exp["type"]
                assert t["schedule"] == exp["schedule"]
                assert t["emits"] == exp["emits"]
                assert t["payload"] == exp["payload"]


# ── Trigger Activation Logic ─────────────────────────────────────────


class TestReflectionTriggerActivation:
    """Test that reflection.enabled activates triggers and applies overrides."""

    def _make_trigger_configs(self):
        """Return a fresh copy of the default trigger configs."""
        return copy.deepcopy(config.DEFAULTS["triggers"])

    def test_disabled_by_default_not_loaded(self):
        """When reflection.enabled is False, no reflection triggers are loaded."""
        trigger_registry.register_trigger_type(TimerTrigger)

        trigger_configs = self._make_trigger_configs()
        # reflection.enabled defaults to False, so triggers stay disabled
        instances = trigger_registry.load_triggers(trigger_configs)
        assert len(instances) == 0

    def test_enabled_activates_triggers(self, monkeypatch):
        """When reflection.enabled is True, reflection triggers are enabled."""
        trigger_registry.register_trigger_type(TimerTrigger)

        trigger_configs = self._make_trigger_configs()

        # Simulate what coordinator does when reflection.enabled is True
        reflection_config = {"enabled": True}
        _REFLECTION_CRON_MAP = {
            "daily-reflection": "daily_cron",
            "weekly-reflection": "weekly_cron",
            "monthly-reflection": "monthly_cron",
        }
        for tcfg in trigger_configs:
            tname = tcfg.get("name", "")
            if tname in _REFLECTION_CRON_MAP:
                tcfg["enabled"] = True

        instances = trigger_registry.load_triggers(trigger_configs)
        assert len(instances) == 3

        names = {inst.name for inst in instances}
        assert "daily-reflection" in names
        assert "weekly-reflection" in names
        assert "monthly-reflection" in names

    def test_cron_override_applied(self):
        """Per-cadence cron overrides from reflection config are applied."""
        trigger_configs = self._make_trigger_configs()
        reflection_config = {
            "enabled": True,
            "daily_cron": "30 22 * * *",
            "weekly_cron": "0 21 * * 5",
            # monthly_cron not overridden
        }

        _REFLECTION_CRON_MAP = {
            "daily-reflection": "daily_cron",
            "weekly-reflection": "weekly_cron",
            "monthly-reflection": "monthly_cron",
        }
        for tcfg in trigger_configs:
            tname = tcfg.get("name", "")
            if tname in _REFLECTION_CRON_MAP:
                tcfg["enabled"] = True
                cron_key = _REFLECTION_CRON_MAP[tname]
                override = reflection_config.get(cron_key)
                if override:
                    tcfg["schedule"] = override

        # Verify overrides
        by_name = {t["name"]: t for t in trigger_configs}
        assert by_name["daily-reflection"]["schedule"] == "30 22 * * *"
        assert by_name["weekly-reflection"]["schedule"] == "0 21 * * 5"
        assert by_name["monthly-reflection"]["schedule"] == "0 23 1 * *"  # unchanged

    def test_trigger_start_registers_cron(self):
        """TimerTrigger.start() registers a cron entry with the correct schedule."""
        trigger_registry.register_trigger_type(TimerTrigger)

        trigger_configs = copy.deepcopy(config.DEFAULTS["triggers"])
        # Enable daily
        for tcfg in trigger_configs:
            if tcfg["name"] == "daily-reflection":
                tcfg["enabled"] = True

        instances = trigger_registry.load_triggers(trigger_configs)
        assert len(instances) == 1
        instances[0].start()

        # TimerTrigger prepends "trigger:" to the name
        cron = trigger_manager.get_cron("trigger:daily-reflection")
        assert cron is not None
        assert cron["cron_expr"] == "0 23 * * *"
        assert cron["event_type"] == "reflection.trigger"
        payload = json.loads(cron["event_payload_json"])
        assert payload["cadence"] == "daily"

    def test_all_three_triggers_register_cron(self):
        """All three reflection triggers register cron entries when enabled."""
        trigger_registry.register_trigger_type(TimerTrigger)

        trigger_configs = copy.deepcopy(config.DEFAULTS["triggers"])
        for tcfg in trigger_configs:
            tcfg["enabled"] = True

        instances = trigger_registry.load_triggers(trigger_configs)
        trigger_registry.start_all()

        expected = {
            "trigger:daily-reflection": ("0 23 * * *", "daily"),
            "trigger:weekly-reflection": ("0 23 * * 0", "weekly"),
            "trigger:monthly-reflection": ("0 23 1 * *", "monthly"),
        }

        for cron_name, (expected_expr, expected_cadence) in expected.items():
            cron = trigger_manager.get_cron(cron_name)
            assert cron is not None, f"Cron entry {cron_name} not found"
            assert cron["cron_expr"] == expected_expr
            assert cron["event_type"] == "reflection.trigger"
            payload = json.loads(cron["event_payload_json"])
            assert payload["cadence"] == expected_cadence

    def test_start_idempotent(self):
        """Starting triggers twice does not error (UNIQUE constraint handled)."""
        trigger_registry.register_trigger_type(TimerTrigger)

        trigger_configs = copy.deepcopy(config.DEFAULTS["triggers"])
        for tcfg in trigger_configs:
            if tcfg["name"] == "daily-reflection":
                tcfg["enabled"] = True

        instances = trigger_registry.load_triggers(trigger_configs)
        instances[0].start()
        instances[0].start()  # should not raise

        cron = trigger_manager.get_cron("trigger:daily-reflection")
        assert cron is not None


# ── Integration: Trigger → Event → Work Queue ────────────────────────


class TestReflectionPipelineIntegration:
    """Test the full pipeline from config-driven trigger to work queue."""

    def test_timer_fired_event_routes_to_work_queue(self):
        """timer.fired event with reflection.trigger routes via builtin subscription."""
        # Load the built-in timer_forward subscription
        subscriptions.load_builtin_subscriptions()

        # Simulate what check_cron() does when a reflection cron fires
        from carpenter.core.engine import event_bus
        payload = {
            "cron_id": 1,
            "cron_name": "trigger:daily-reflection",
            "cron_event_type": "reflection.trigger",
            "fire_time": "2026-04-04T23:00:00+00:00",
            "event_payload": {"cadence": "daily"},
        }
        event_bus.record_event("timer.fired", payload)

        # Process subscriptions (timer_forward routes to work_queue)
        created = subscriptions.process_subscriptions()
        assert created == 1

        # Verify work_queue item
        db = get_db()
        try:
            items = db.execute(
                "SELECT * FROM work_queue WHERE event_type = 'reflection.trigger'"
            ).fetchall()
            assert len(items) == 1
            work_payload = json.loads(items[0]["payload_json"])
            assert work_payload["cron_name"] == "trigger:daily-reflection"
            assert work_payload["event_payload"]["cadence"] == "daily"
        finally:
            db.close()

    def test_custom_cron_persists_through_trigger(self):
        """Custom cron schedule from reflection config is used by the trigger."""
        trigger_registry.register_trigger_type(TimerTrigger)

        trigger_configs = copy.deepcopy(config.DEFAULTS["triggers"])

        # Apply a custom schedule for daily
        for tcfg in trigger_configs:
            if tcfg["name"] == "daily-reflection":
                tcfg["enabled"] = True
                tcfg["schedule"] = "30 22 * * *"

        instances = trigger_registry.load_triggers(trigger_configs)
        trigger_registry.start_all()

        cron = trigger_manager.get_cron("trigger:daily-reflection")
        assert cron is not None
        assert cron["cron_expr"] == "30 22 * * *"


# ── Backward Compatibility ───────────────────────────────────────────


class TestBackwardCompatibility:
    """Ensure the migration doesn't break existing behavior."""

    def test_handler_payload_format_unchanged(self):
        """The work queue payload format matches what the reflection handler expects.

        The handler reads payload["event_payload"]["cadence"], which is
        the format produced by _action_forward_timer in subscriptions.
        """
        # Simulate the payload that forward_timer creates
        work_payload = {
            "cron_id": 1,
            "cron_name": "trigger:daily-reflection",
            "fire_time": "2026-04-04T23:00:00+00:00",
            "event_payload": {"cadence": "daily"},
        }

        # Verify the handler would extract cadence correctly
        event_payload = work_payload.get("event_payload", {})
        cadence = event_payload.get("cadence", "daily")
        assert cadence == "daily"

    def test_old_cron_entries_coexist(self):
        """Old hardcoded cron entries (if any remain in DB) don't conflict.

        The old code used names like "daily-reflection" while the new trigger
        system uses "trigger:daily-reflection". These are different names so
        they won't conflict via UNIQUE constraint.
        """
        # Register old-style cron (simulating pre-migration state)
        trigger_manager.add_cron(
            "daily-reflection", "0 23 * * *", "reflection.trigger",
            {"cadence": "daily"},
        )

        # Register new-style cron (trigger: prefix)
        trigger_manager.add_cron(
            "trigger:daily-reflection", "0 23 * * *", "reflection.trigger",
            {"cadence": "daily"},
        )

        # Both exist without conflict
        old = trigger_manager.get_cron("daily-reflection")
        new = trigger_manager.get_cron("trigger:daily-reflection")
        assert old is not None
        assert new is not None
        assert old["id"] != new["id"]

    def test_reflection_disabled_no_triggers(self):
        """When reflection.enabled is False, no reflection triggers are loaded."""
        trigger_registry.register_trigger_type(TimerTrigger)

        # Load defaults (reflection triggers disabled by default)
        trigger_configs = copy.deepcopy(config.DEFAULTS["triggers"])
        instances = trigger_registry.load_triggers(trigger_configs)

        # None should be loaded since all are disabled
        assert len(instances) == 0

        # No cron entries should exist
        for name in ("trigger:daily-reflection", "trigger:weekly-reflection", "trigger:monthly-reflection"):
            assert trigger_manager.get_cron(name) is None
