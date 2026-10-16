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
    async def test_start_reads_token_from_config(self, monkeypatch):
        """A token from {base_dir}/.env arrives via config, not os.environ."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        c = TelegramChannelConnector(connector_config={"enabled": True})

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "ok": True, "result": {"username": "test_bot"},
        }
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("carpenter.channels.telegram_channel.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            await c.start({"telegram_bot_token": "123:FROMCONFIG"})

        assert c._bot_token == "123:FROMCONFIG"
        await c.stop()

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


class TestTelegramWebhookAuth:
    """X-Telegram-Bot-Api-Secret-Token verification on the webhook."""

    SECRET = "s3cret-Value_for-tests"
    HEADER = "X-Telegram-Bot-Api-Secret-Token"

    _UPDATE = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": 12345, "username": "alice"},
            "chat": {"id": 12345, "type": "private"},
            "text": "Forged?",
        },
    }

    def _client_for(self, secret: str):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        c = TelegramChannelConnector(connector_config={
            "bot_token": "123:ABC", "mode": "webhook",
        })
        c._webhook_secret = secret
        c._build_webhook_routes()
        return c, TestClient(Starlette(routes=c.routes))

    def test_correct_secret_is_processed(self):
        c, client = self._client_for(self.SECRET)
        with patch.object(c, "_handle_update", new_callable=AsyncMock) as mock_handle:
            resp = client.post(
                "/hooks/telegram", json=self._UPDATE,
                headers={self.HEADER: self.SECRET},
            )
        assert resp.status_code == 200
        mock_handle.assert_called_once_with(self._UPDATE)

    @pytest.mark.parametrize("headers", [
        {},                                                  # missing
        {"X-Telegram-Bot-Api-Secret-Token": ""},             # empty
        {"X-Telegram-Bot-Api-Secret-Token": "wrong"},        # wrong
        {"X-Telegram-Bot-Api-Secret-Token": "s3cret-Value_for-test"},    # prefix
        {"X-Telegram-Bot-Api-Secret-Token": "s3cret-Value_for-testsX"},  # suffix
        {"X-Webhook-Secret": "s3cret-Value_for-tests"},      # wrong header
    ])
    def test_bad_secret_rejected_and_not_processed(self, headers):
        c, client = self._client_for(self.SECRET)
        with patch.object(c, "_handle_update", new_callable=AsyncMock) as mock_handle, \
             patch.object(c, "deliver_inbound", new_callable=AsyncMock) as mock_deliver:
            resp = client.post("/hooks/telegram", json=self._UPDATE, headers=headers)
        assert resp.status_code == 401
        mock_handle.assert_not_called()
        mock_deliver.assert_not_called()

    def test_secret_in_query_string_not_accepted(self):
        """Telegram only sends the header; a query parameter is not a substitute."""
        c, client = self._client_for(self.SECRET)
        with patch.object(c, "_handle_update", new_callable=AsyncMock) as mock_handle:
            resp = client.post(
                f"/hooks/telegram?secret={self.SECRET}", json=self._UPDATE,
            )
        assert resp.status_code == 401
        mock_handle.assert_not_called()

    def test_rejected_before_body_is_parsed(self):
        c, client = self._client_for(self.SECRET)
        resp = client.post(
            "/hooks/telegram", content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 401

    def test_rejection_does_not_log_secret(self, caplog):
        c, client = self._client_for(self.SECRET)
        with caplog.at_level("DEBUG", logger="carpenter.channels.telegram_channel"):
            client.post(
                "/hooks/telegram", json=self._UPDATE,
                headers={self.HEADER: "wrong-guess"},
            )
        assert "Rejected Telegram webhook" in caplog.text
        assert self.SECRET not in caplog.text
        assert "wrong-guess" not in caplog.text

    def test_unset_secret_accepts_without_auth(self):
        """Backward compatibility: no secret configured = webhook open."""
        c, client = self._client_for("")
        with patch.object(c, "_handle_update", new_callable=AsyncMock) as mock_handle:
            resp = client.post("/hooks/telegram", json=self._UPDATE)
        assert resp.status_code == 200
        mock_handle.assert_called_once()

    def test_secret_ignored_in_connector_yaml(self):
        """The secret is a credential; connector YAML cannot set it."""
        c = TelegramChannelConnector(connector_config={
            "bot_token": "123:ABC", "mode": "webhook",
            "webhook_secret": "from-yaml", "secret_token": "from-yaml",
            "telegram_webhook_secret": "from-yaml",
        })
        assert c._webhook_secret == ""

    @staticmethod
    def _connector_with_api():
        c = TelegramChannelConnector(connector_config={
            "bot_token": "123:ABC", "mode": "webhook",
        })
        api = AsyncMock(return_value={})
        return c, api

    @pytest.mark.asyncio
    async def test_setup_registers_secret_token(self, caplog):
        c, api = self._connector_with_api()
        with patch.object(c, "_api", api), \
             caplog.at_level("DEBUG", logger="carpenter.channels.telegram_channel"):
            await c._setup_webhook({"telegram_webhook_secret": self.SECRET})
        api.assert_awaited_once()
        method, kwargs = api.await_args.args[0], api.await_args.kwargs
        assert method == "setWebhook"
        assert kwargs["secret_token"] == self.SECRET
        assert c._webhook_secret == self.SECRET
        assert c.routes
        assert "UNAUTHENTICATED" not in caplog.text
        assert self.SECRET not in caplog.text

    @pytest.mark.asyncio
    async def test_setup_warns_when_secret_unset(self, caplog):
        c, api = self._connector_with_api()
        with patch.object(c, "_api", api), \
             caplog.at_level("WARNING", logger="carpenter.channels.telegram_channel"):
            await c._setup_webhook({})
        assert "secret_token" not in api.await_args.kwargs
        assert "UNAUTHENTICATED" in caplog.text
        assert "TELEGRAM_WEBHOOK_SECRET" in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["has space", "x" * 257, "émoji", "a/b"])
    async def test_setup_rejects_secret_telegram_would_refuse(self, bad):
        c, api = self._connector_with_api()
        with patch.object(c, "_api", api):
            with pytest.raises(ValueError, match="TELEGRAM_WEBHOOK_SECRET") as exc:
                await c._setup_webhook({"telegram_webhook_secret": bad})
        assert bad not in str(exc.value)
        api.assert_not_awaited()
        assert c.routes is None


class TestTelegramWebhookSecretCredential:
    def test_env_var_maps_to_config_key(self):
        from carpenter.config import _CREDENTIAL_MAP, DEFAULTS
        assert _CREDENTIAL_MAP["TELEGRAM_WEBHOOK_SECRET"] == "telegram_webhook_secret"
        assert DEFAULTS["telegram_webhook_secret"] == ""

    def test_env_var_loaded(self, monkeypatch):
        from carpenter.config import _load_env
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "abc")
        assert _load_env()["telegram_webhook_secret"] == "abc"

    def test_refused_by_config_get_value(self, monkeypatch):
        from carpenter import config as carpenter_config
        from carpenter.tool_backends import config_tool
        monkeypatch.setitem(carpenter_config.CONFIG, "telegram_webhook_secret", "s")
        with pytest.raises(ValueError):
            config_tool.handle_get_value({"key": "telegram_webhook_secret"})
