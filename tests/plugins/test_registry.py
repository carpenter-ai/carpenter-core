"""Tests for the connector registry (replaces old plugin registry tests)."""

import json
import time
from pathlib import Path

import pytest

from carpenter.channels.registry import ConnectorRegistry


def _make_connector_config(shared_folder, enabled=True):
    return {
        "kind": "tool",
        "enabled": enabled,
        "transport": "file_watch",
        "description": "Test plugin",
        "shared_folder": str(shared_folder),
        "timeout_seconds": 60,
    }


class TestConnectorRegistry:
    def test_load_empty_config(self):
        registry = ConnectorRegistry({})
        assert registry.connectors == {}
        assert registry.list_connectors() == []

    def test_load_enabled_connector(self, tmp_path):
        shared = tmp_path / "shared" / "test-plugin"
        shared.mkdir(parents=True)
        registry = ConnectorRegistry({
            "test-plugin": _make_connector_config(shared),
        })
        assert "test-plugin" in registry.connectors
        connector = registry.get("test-plugin")
        assert connector is not None
        assert connector.enabled is True
        assert connector.transport is not None

    def test_skip_disabled_connector(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        registry = ConnectorRegistry({
            "disabled": _make_connector_config(shared, enabled=False),
        })
        assert registry.get("disabled") is None
        assert registry.list_connectors() == []

    def test_multiple_connectors(self, tmp_path):
        for name in ("plugin-a", "plugin-b"):
            (tmp_path / name).mkdir()
        registry = ConnectorRegistry({
            "plugin-a": _make_connector_config(tmp_path / "plugin-a"),
            "plugin-b": _make_connector_config(tmp_path / "plugin-b"),
        })
        assert len(registry.connectors) == 2
        assert registry.get("plugin-a") is not None
        assert registry.get("plugin-b") is not None

    def test_get_nonexistent_connector(self):
        registry = ConnectorRegistry({})
        assert registry.get("doesnt-exist") is None

    def test_list_by_kind(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        registry = ConnectorRegistry({
            "tool-conn": _make_connector_config(shared),
        })
        tools = registry.list_connectors(kind="tool")
        assert len(tools) == 1
        channels = registry.list_connectors(kind="channel")
        assert len(channels) == 0

    def test_check_for_config_changes(self, tmp_path, monkeypatch):
        shared = tmp_path / "shared"
        shared.mkdir()
        registry = ConnectorRegistry({})
        assert len(registry.connectors) == 0

        # Simulate config change
        monkeypatch.setattr("carpenter.channels.registry.config.CONFIG", {
            "connectors": {
                "new-plugin": _make_connector_config(shared),
            },
        })
        registry.check_for_config_changes()
        assert len(registry.connectors) == 1

    def test_check_for_config_changes_no_change(self, monkeypatch):
        monkeypatch.setattr("carpenter.channels.registry.config.CONFIG", {
            "connectors": {},
        })
        registry = ConnectorRegistry({})
        # Should not rebuild since config hasn't changed
        registry.check_for_config_changes()
        assert len(registry.connectors) == 0

    def test_unknown_factory_does_not_crash(self):
        registry = ConnectorRegistry({
            "bad": {
                "kind": "unknown",
                "transport": "unknown",
                "enabled": True,
            },
        })
        assert len(registry.connectors) == 0
