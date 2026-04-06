"""Tests for WebChannelConnector."""

import asyncio

import pytest

from carpenter.channels.web_channel import WebChannelConnector
from carpenter.channels.channel import get_invocation_tracker


class TestWebChannelConnector:
    def test_kind_is_channel(self):
        c = WebChannelConnector()
        assert c.kind == "channel"

    def test_channel_type_is_web(self):
        c = WebChannelConnector()
        assert c.channel_type == "web"

    def test_always_enabled(self):
        c = WebChannelConnector()
        assert c.enabled is True

    def test_start_is_noop(self):
        c = WebChannelConnector()
        asyncio.get_event_loop().run_until_complete(c.start({}))

    def test_stop_cancels_pending(self):
        c = WebChannelConnector()
        tracker = get_invocation_tracker()

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        task = asyncio.ensure_future(future)
        tracker.track(1, task)

        asyncio.get_event_loop().run_until_complete(c.stop())
        assert not tracker.is_pending(1)
        future.cancel()

    def test_health_check_always_healthy(self):
        c = WebChannelConnector()
        result = asyncio.get_event_loop().run_until_complete(c.health_check())
        assert result.healthy is True
        assert result.detail == "web"

    def test_send_message_returns_true(self):
        c = WebChannelConnector()
        result = asyncio.get_event_loop().run_until_complete(
            c.send_message(1, "Hello")
        )
        assert result is True

    def test_default_name(self):
        c = WebChannelConnector()
        assert c.name == "web"

    def test_custom_name(self):
        c = WebChannelConnector(name="custom-web")
        assert c.name == "custom-web"
