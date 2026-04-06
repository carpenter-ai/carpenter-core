"""Tests for ConnectorRegistry lifecycle and initialization."""

import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from carpenter.channels.registry import (
    ConnectorRegistry,
    initialize_connector_registry,
    get_connector_registry,
    register_factory,
    _CONNECTOR_FACTORIES,
)
from carpenter.channels.base import Connector, HealthStatus


class DummyConnector(Connector):
    kind = "tool"

    def __init__(self, name, config):
        self.name = name
        self.enabled = config.get("enabled", True)
        self._started = False

    async def start(self, config):
        self._started = True

    async def stop(self):
        pass

    async def health_check(self):
        return HealthStatus(healthy=True, detail="ok")


class TestConnectorRegistryLifecycle:
    def test_start_all(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        registry = ConnectorRegistry({
            "test": {
                "kind": "tool",
                "enabled": True,
                "transport": "file_watch",
                "shared_folder": str(shared),
            },
        })
        asyncio.get_event_loop().run_until_complete(registry.start_all())

    def test_stop_all(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        registry = ConnectorRegistry({
            "test": {
                "kind": "tool",
                "enabled": True,
                "transport": "file_watch",
                "shared_folder": str(shared),
            },
        })
        asyncio.get_event_loop().run_until_complete(registry.stop_all())

    def test_managed_context_manager(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        registry = ConnectorRegistry({
            "test": {
                "kind": "tool",
                "enabled": True,
                "transport": "file_watch",
                "shared_folder": str(shared),
            },
        })

        async def _run():
            async with registry.managed():
                assert len(registry.connectors) == 1

        asyncio.get_event_loop().run_until_complete(_run())


class TestFactoryRegistration:
    def test_register_custom_factory(self):
        # Save and restore
        key = ("test_kind", "test_transport")
        original = _CONNECTOR_FACTORIES.get(key)
        try:
            register_factory("test_kind", "test_transport", DummyConnector)
            assert key in _CONNECTOR_FACTORIES

            registry = ConnectorRegistry({
                "custom": {
                    "kind": "test_kind",
                    "transport": "test_transport",
                    "enabled": True,
                },
            })
            assert "custom" in registry.connectors
        finally:
            if original is None:
                _CONNECTOR_FACTORIES.pop(key, None)
            else:
                _CONNECTOR_FACTORIES[key] = original


class TestInitializeConnectorRegistry:
    def test_initialize_with_empty_config(self, monkeypatch):
        monkeypatch.setattr("carpenter.channels.registry.config.CONFIG", {
            "connectors": {},
            "plugins_config": "",
            "base_dir": "",
        })

        with patch("carpenter.core.engine.main_loop.register_heartbeat_hook"):
            asyncio.get_event_loop().run_until_complete(
                initialize_connector_registry()
            )

        registry = get_connector_registry()
        assert registry is not None

    def test_initialize_auto_migrates(self, monkeypatch, tmp_path):
        import json
        shared = tmp_path / "shared"
        shared.mkdir()
        plugins_path = tmp_path / "plugins.json"
        plugins_path.write_text(json.dumps({
            "plugins": {
                "migrated": {
                    "enabled": True,
                    "transport": "file-watch",
                    "transport_config": {"shared_folder": str(shared)},
                },
            },
        }))

        cfg = {
            "connectors": {},
            "plugins_config": str(plugins_path),
            "base_dir": str(tmp_path),
        }
        monkeypatch.setattr("carpenter.channels.registry.config.CONFIG", cfg)

        with patch("carpenter.core.engine.main_loop.register_heartbeat_hook"):
            asyncio.get_event_loop().run_until_complete(
                initialize_connector_registry()
            )

        registry = get_connector_registry()
        assert registry is not None
        assert "migrated" in registry.connectors
