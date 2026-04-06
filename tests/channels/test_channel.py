"""Tests for ChannelConnector and InvocationTracker."""

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from carpenter.channels.channel import (
    ChannelConnector,
    InvocationTracker,
    get_invocation_tracker,
)
from carpenter.channels.base import HealthStatus


class DummyChannel(ChannelConnector):
    """Concrete channel connector for testing."""

    channel_type = "test"

    def __init__(self, name="test-channel", enabled=True):
        self.name = name
        self.enabled = enabled
        self.sent_messages = []

    async def start(self, config):
        pass

    async def stop(self):
        pass

    async def health_check(self):
        return HealthStatus(healthy=True, detail="test")

    async def send_message(self, conversation_id, text, metadata=None):
        self.sent_messages.append((conversation_id, text, metadata))
        return True


class TestInvocationTracker:
    def test_is_pending_false_initially(self):
        tracker = InvocationTracker()
        assert tracker.is_pending(1) is False

    def test_track_makes_pending(self):
        tracker = InvocationTracker()
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        task = asyncio.ensure_future(future)
        tracker.track(1, task)
        assert tracker.is_pending(1) is True
        future.set_result(None)

    def test_done_callback_removes_pending(self):
        tracker = InvocationTracker()
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        task = asyncio.ensure_future(future)
        tracker.track(1, task)
        assert tracker.is_pending(1) is True

        # Complete the future and run callbacks
        future.set_result(None)
        loop.run_until_complete(asyncio.sleep(0))
        assert tracker.is_pending(1) is False

    def test_cancel_all(self):
        tracker = InvocationTracker()
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        task = asyncio.ensure_future(future)
        tracker.track(1, task)
        tracker.cancel_all()
        assert tracker.is_pending(1) is False
        assert task.cancelled()

    def test_clear(self):
        tracker = InvocationTracker()
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        task = asyncio.ensure_future(future)
        tracker.track(1, task)
        tracker.clear()
        assert tracker.is_pending(1) is False
        future.cancel()


class TestInvocationTrackerSingleton:
    def test_get_returns_same_instance(self):
        a = get_invocation_tracker()
        b = get_invocation_tracker()
        assert a is b


class TestChannelConnectorProperties:
    def test_kind_is_channel(self):
        c = DummyChannel()
        assert c.kind == "channel"

    def test_channel_type(self):
        c = DummyChannel()
        assert c.channel_type == "test"


class TestChannelConnectorDeliverInbound:
    def test_deliver_creates_conversation_and_message(self, db):
        """deliver_inbound saves the user message and returns a conv_id."""
        channel = DummyChannel()

        with patch("carpenter.agent.invocation.invoke_for_chat") as mock_invoke, \
             patch("carpenter.agent.conversation.get_or_create_conversation", return_value=1), \
             patch("carpenter.agent.conversation.get_conversation", return_value=None), \
             patch("carpenter.agent.conversation.add_message") as mock_add, \
             patch("carpenter.agent.conversation.get_messages", return_value=[]):
            mock_invoke.return_value = {}

            conv_id = asyncio.get_event_loop().run_until_complete(
                channel.deliver_inbound("user123", "Hello")
            )

        assert conv_id == 1
        mock_add.assert_called_once_with(1, "user", "Hello")

    def test_deliver_with_explicit_conversation_id(self, db):
        """deliver_inbound uses explicit conversation_id when provided."""
        channel = DummyChannel()

        with patch("carpenter.agent.invocation.invoke_for_chat") as mock_invoke, \
             patch("carpenter.agent.conversation.add_message") as mock_add, \
             patch("carpenter.agent.conversation.get_messages", return_value=[]):
            mock_invoke.return_value = {}

            conv_id = asyncio.get_event_loop().run_until_complete(
                channel.deliver_inbound("user123", "Hello", conversation_id=42)
            )

        assert conv_id == 42
        mock_add.assert_called_once_with(42, "user", "Hello")


class TestChannelBindingsSchema:
    def test_channel_bindings_table_exists(self, db):
        """The channel_bindings table should be created by schema."""
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "channel_bindings" in tables

    def test_channel_bindings_unique_constraint(self, db):
        """channel_type + channel_user_id is unique."""
        db.execute(
            "INSERT INTO channel_bindings (channel_type, channel_user_id, conversation_id) "
            "VALUES ('test', 'user1', 1)"
        )
        db.commit()

        # Second insert with same type+user should fail
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO channel_bindings (channel_type, channel_user_id, conversation_id) "
                "VALUES ('test', 'user1', 2)"
            )

    def test_conversations_has_channel_type_column(self, db):
        """conversations table should have channel_type column."""
        cols = {row[1] for row in db.execute("PRAGMA table_info(conversations)").fetchall()}
        assert "channel_type" in cols
