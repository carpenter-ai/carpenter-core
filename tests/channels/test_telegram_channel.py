"""Tests for TelegramChannelConnector."""

import asyncio
import json
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from carpenter.channels.telegram_channel import (
    TelegramChannelConnector,
    TELEGRAM_MAX_LENGTH,
)
from carpenter.channels.base import HealthStatus


class TestTelegramChannelProperties:
    def test_kind_is_channel(self):
        c = TelegramChannelConnector()
        assert c.kind == "channel"

    def test_channel_type_is_telegram(self):
        c = TelegramChannelConnector()
        assert c.channel_type == "telegram"

    def test_default_disabled(self):
        c = TelegramChannelConnector()
        assert c.enabled is False

    def test_enabled_via_config(self):
        c = TelegramChannelConnector(connector_config={"enabled": True})
        assert c.enabled is True

    def test_default_mode_is_polling(self):
        c = TelegramChannelConnector()
        assert c._mode == "polling"

    def test_custom_name(self):
        c = TelegramChannelConnector(name="my-telegram")
        assert c.name == "my-telegram"

    def test_bot_token_from_config(self):
        c = TelegramChannelConnector(connector_config={"bot_token": "123:ABC"})
        assert c._bot_token == "123:ABC"

    def test_allowed_users_converted_to_strings(self):
        c = TelegramChannelConnector(connector_config={
            "allowed_users": [12345, "alice"],
        })
        assert c._allowed_users == ["12345", "alice"]


class TestTelegramAllowlist:
    def test_empty_allowlist_allows_all(self):
        c = TelegramChannelConnector(connector_config={"allowed_users": []})
        assert c._check_allowed("12345", "alice") is True

    def test_user_id_in_allowlist(self):
        c = TelegramChannelConnector(connector_config={"allowed_users": ["12345"]})
        assert c._check_allowed("12345", "alice") is True

    def test_username_in_allowlist(self):
        c = TelegramChannelConnector(connector_config={"allowed_users": ["alice"]})
        assert c._check_allowed("99999", "alice") is True

    def test_user_not_in_allowlist(self):
        c = TelegramChannelConnector(connector_config={"allowed_users": ["12345"]})
        assert c._check_allowed("99999", "bob") is False


class TestTelegramStartStop:
    @pytest.mark.asyncio
    async def test_start_validates_token(self):
        """start() should call getMe to validate the bot token."""
        c = TelegramChannelConnector(connector_config={
            "bot_token": "123:ABC",
            "enabled": True,
        })

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "result": {"username": "test_bot"},
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("carpenter.channels.telegram_channel.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            await c.start({})

        assert c._bot_username == "test_bot"
        assert c._poll_task is not None

        # Clean up
        await c.stop()

    @pytest.mark.asyncio
    async def test_start_raises_without_token(self):
        """start() should raise ValueError if bot_token is empty."""
        c = TelegramChannelConnector(connector_config={"enabled": True})
        with pytest.raises(ValueError, match="bot_token"):
            await c.start({})

    @pytest.mark.asyncio
    async def test_stop_cancels_poll_task(self):
        """stop() should cancel the polling task."""
        c = TelegramChannelConnector(connector_config={
            "bot_token": "123:ABC",
            "enabled": True,
        })

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        def make_response(ok=True, result=None):
            r = MagicMock()
            r.raise_for_status = MagicMock()
            r.json.return_value = {"ok": ok, "result": result or {}}
            return r

        call_count = 0

        async def mock_post(url, json=None):
            nonlocal call_count
            call_count += 1
            if "getMe" in url:
                return make_response(result={"username": "bot"})
            if "deleteWebhook" in url:
                return make_response()
            if "getUpdates" in url:
                # Simulate slow poll that will be cancelled
                await asyncio.sleep(100)
                return make_response(result=[])
            return make_response()

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.aclose = AsyncMock()

        with patch("carpenter.channels.telegram_channel.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            await c.start({})
            assert c._poll_task is not None
            # Give poll task time to start
            await asyncio.sleep(0.05)
            await c.stop()

        assert c._poll_task is None


class TestTelegramHealthCheck:
    @pytest.mark.asyncio
    async def test_not_started(self):
        c = TelegramChannelConnector()
        status = await c.health_check()
        assert status.healthy is False
        assert "not started" in status.detail

    @pytest.mark.asyncio
    async def test_too_many_errors(self):
        c = TelegramChannelConnector()
        c._client = MagicMock()  # Pretend started
        c._consecutive_errors = 11
        status = await c.health_check()
        assert status.healthy is False
        assert "consecutive errors" in status.detail


class TestTelegramHandleUpdate:
    @pytest.mark.asyncio
    async def test_text_message_calls_deliver_inbound(self):
        """A text message update should call deliver_inbound."""
        c = TelegramChannelConnector(connector_config={
            "bot_token": "123:ABC",
            "enabled": True,
        })

        update = {
            "update_id": 1,
            "message": {
                "text": "Hello bot",
                "from": {"id": 12345, "first_name": "Alice", "username": "alice"},
                "chat": {"id": 12345},
            },
        }

        with patch.object(c, "deliver_inbound", new_callable=AsyncMock) as mock_deliver:
            await c._handle_update(update)

        mock_deliver.assert_called_once_with(
            channel_user_id="12345",
            text="Hello bot",
            display_name="Alice",
        )

    @pytest.mark.asyncio
    async def test_non_text_message_ignored(self):
        """Updates without text should be silently ignored."""
        c = TelegramChannelConnector()
        update = {
            "update_id": 1,
            "message": {
                "from": {"id": 12345},
                "photo": [{"file_id": "abc"}],
                "chat": {"id": 12345},
            },
        }

        with patch.object(c, "deliver_inbound", new_callable=AsyncMock) as mock_deliver:
            await c._handle_update(update)

        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_message_in_update_ignored(self):
        """Updates without message field should be silently ignored."""
        c = TelegramChannelConnector()
        update = {"update_id": 1, "edited_message": {"text": "edited"}}

        with patch.object(c, "deliver_inbound", new_callable=AsyncMock) as mock_deliver:
            await c._handle_update(update)

        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_unauthorized_user_gets_rejection(self):
        """Unauthorized users should get a rejection message."""
        c = TelegramChannelConnector(connector_config={
            "bot_token": "123:ABC",
            "allowed_users": ["99999"],
        })
        c._client = AsyncMock()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {}}
        c._client.post = AsyncMock(return_value=mock_response)

        update = {
            "update_id": 1,
            "message": {
                "text": "Hello",
                "from": {"id": 12345, "username": "stranger"},
                "chat": {"id": 12345},
            },
        }

        with patch.object(c, "deliver_inbound", new_callable=AsyncMock) as mock_deliver:
            await c._handle_update(update)

        mock_deliver.assert_not_called()
        # Should have called sendMessage with rejection
        c._client.post.assert_called_once()
        call_args = c._client.post.call_args
        assert "sendMessage" in call_args[0][0]


class TestTelegramSendMessage:
    @pytest.mark.asyncio
    async def test_send_message_calls_api(self, db):
        """send_message should call sendMessage on the Telegram API."""
        c = TelegramChannelConnector(connector_config={"bot_token": "123:ABC"})
        c._client = AsyncMock()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {}}
        c._client.post = AsyncMock(return_value=mock_response)

        # Insert a channel binding
        from carpenter.db import get_db
        conn = get_db()
        conn.execute(
            "INSERT INTO channel_bindings (channel_type, channel_user_id, conversation_id) "
            "VALUES ('telegram', '12345', 1)"
        )
        conn.commit()
        conn.close()

        result = await c.send_message(1, "Hello back!")
        assert result is True

        c._client.post.assert_called_once()
        call_url = c._client.post.call_args[0][0]
        assert "sendMessage" in call_url

    @pytest.mark.asyncio
    async def test_send_message_no_binding_returns_false(self, db):
        """send_message returns False when no channel binding exists."""
        c = TelegramChannelConnector(connector_config={"bot_token": "123:ABC"})
        c._client = AsyncMock()

        result = await c.send_message(999, "Hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_message_not_started(self):
        """send_message returns False when connector not started."""
        c = TelegramChannelConnector()
        result = await c.send_message(1, "Hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_message_splits_long_messages(self, db):
        """Long messages should be split into multiple sendMessage calls."""
        c = TelegramChannelConnector(connector_config={"bot_token": "123:ABC"})
        c._client = AsyncMock()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {}}
        c._client.post = AsyncMock(return_value=mock_response)

        # Insert a channel binding
        from carpenter.db import get_db
        conn = get_db()
        conn.execute(
            "INSERT INTO channel_bindings (channel_type, channel_user_id, conversation_id) "
            "VALUES ('telegram', '12345', 1)"
        )
        conn.commit()
        conn.close()

        # Create a message longer than TELEGRAM_MAX_LENGTH
        long_text = "A" * (TELEGRAM_MAX_LENGTH + 100)
        result = await c.send_message(1, long_text)
        assert result is True
        # Should have sent multiple messages
        assert c._client.post.call_count > 1


class TestTelegramResolveChatId:
    def test_resolves_from_channel_bindings(self, db):
        """_resolve_chat_id should find the Telegram user from channel_bindings."""
        c = TelegramChannelConnector()

        from carpenter.db import get_db
        conn = get_db()
        conn.execute(
            "INSERT INTO channel_bindings (channel_type, channel_user_id, conversation_id) "
            "VALUES ('telegram', '12345', 42)"
        )
        conn.commit()
        conn.close()

        assert c._resolve_chat_id(42) == "12345"

    def test_returns_none_for_unknown_conversation(self, db):
        c = TelegramChannelConnector()
        assert c._resolve_chat_id(999) is None
