"""Tests for Connector ABC and HealthStatus."""

import asyncio
from datetime import datetime

import pytest

from carpenter.channels.base import Connector, HealthStatus


class DummyConnector(Connector):
    """Concrete implementation for testing the ABC."""

    def __init__(self, name, kind="tool", enabled=True):
        self.name = name
        self.kind = kind
        self.enabled = enabled
        self._started = False
        self._stopped = False

    async def start(self, config):
        self._started = True

    async def stop(self):
        self._stopped = True

    async def health_check(self):
        return HealthStatus(healthy=True, detail="ok")


class TestHealthStatus:
    def test_defaults(self):
        h = HealthStatus(healthy=True)
        assert h.healthy is True
        assert h.detail == ""
        assert h.last_seen is None

    def test_all_fields(self):
        now = datetime.now()
        h = HealthStatus(healthy=False, detail="down", last_seen=now)
        assert h.healthy is False
        assert h.detail == "down"
        assert h.last_seen == now


class TestConnectorABC:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            Connector()

    def test_concrete_subclass(self):
        c = DummyConnector("test", kind="tool", enabled=True)
        assert c.name == "test"
        assert c.kind == "tool"
        assert c.enabled is True

    def test_start_stop(self):
        c = DummyConnector("test")
        asyncio.get_event_loop().run_until_complete(c.start({}))
        assert c._started
        asyncio.get_event_loop().run_until_complete(c.stop())
        assert c._stopped

    def test_health_check(self):
        c = DummyConnector("test")
        result = asyncio.get_event_loop().run_until_complete(c.health_check())
        assert result.healthy is True
        assert result.detail == "ok"
