"""Tests for SignalChannelConnector — subprocess mode."""

import asyncio
import json
import os
from unittest.mock import patch, AsyncMock, MagicMock, PropertyMock

import httpx
import pytest

from carpenter.channels.signal_channel import (
    SignalChannelConnector,
    SIGNAL_MAX_LENGTH,
)
from carpenter.channels.base import HealthStatus


class TestSignalChannelProperties:
    def test_kind_is_channel(self):
        c = SignalChannelConnector()
        assert c.kind == "channel"

    def test_channel_type_is_signal(self):
        c = SignalChannelConnector()
        assert c.channel_type == "signal"

    def test_default_disabled(self):
        c = SignalChannelConnector()
        assert c.enabled is False

    def test_enabled_via_config(self):
        c = SignalChannelConnector(connector_config={"enabled": True})
        assert c.enabled is True

    def test_custom_name(self):
        c = SignalChannelConnector(name="my-signal")
        assert c.name == "my-signal"

    def test_account_from_config(self):
        c = SignalChannelConnector(connector_config={"account": "+1234567890"})
        assert c._account == "+1234567890"

    def test_allowed_numbers_converted_to_strings(self):
        c = SignalChannelConnector(connector_config={
            "allowed_numbers": ["+1234567890", "+0987654321"],
        })
        assert c._allowed_numbers == ["+1234567890", "+0987654321"]


class TestSignalAllowlist:
    def test_empty_allowlist_allows_all(self):
        c = SignalChannelConnector(connector_config={"allowed_numbers": []})
        assert c._check_allowed("+1234567890") is True

    def test_number_in_allowlist(self):
        c = SignalChannelConnector(connector_config={
            "allowed_numbers": ["+1234567890"],
        })
        assert c._check_allowed("+1234567890") is True

    def test_number_not_in_allowlist(self):
        c = SignalChannelConnector(connector_config={
            "allowed_numbers": ["+1234567890"],
        })
        assert c._check_allowed("+0000000000") is False


class TestSignalStart:
    @pytest.mark.asyncio
    async def test_start_raises_without_account(self):
        """start() should raise ValueError if account is empty."""
        c = SignalChannelConnector(connector_config={"enabled": True})
        with pytest.raises(ValueError, match="account"):
            await c.start({})

    @pytest.mark.asyncio
    async def test_start_raises_if_binary_not_found(self, tmp_path):
        """start() should raise FileNotFoundError if signal-cli doesn't exist."""
        c = SignalChannelConnector(connector_config={
            "enabled": True,
            "account": "+1234567890",
            "signal_cli_path": str(tmp_path / "nonexistent"),
        })
        with pytest.raises(FileNotFoundError):
            await c.start({})

    @pytest.mark.asyncio
    async def test_start_raises_if_binary_not_executable(self, tmp_path):
        """start() should raise PermissionError if signal-cli isn't executable."""
        cli_path = tmp_path / "signal-cli"
        cli_path.write_text("#!/bin/sh\n")
        cli_path.chmod(0o644)  # not executable

        c = SignalChannelConnector(connector_config={
            "enabled": True,
            "account": "+1234567890",
            "signal_cli_path": str(cli_path),
        })
        with pytest.raises(PermissionError):
            await c.start({})

    @pytest.mark.asyncio
    async def test_start_launches_subprocess(self, tmp_path):
        """start() should launch signal-cli as a subprocess."""
        cli_path = tmp_path / "signal-cli"
        cli_path.write_text("#!/bin/sh\n")
        cli_path.chmod(0o755)

        c = SignalChannelConnector(connector_config={
            "enabled": True,
            "account": "+1234567890",
            "signal_cli_path": str(cli_path),
        })

        mock_process = AsyncMock()
        mock_process.pid = 12345
        mock_process.returncode = None
        mock_process.stdout = AsyncMock()
        mock_process.stdout.readline = AsyncMock(return_value=b"")
        mock_process.stdin = AsyncMock()
        mock_process.send_signal = MagicMock()
        mock_process.wait = AsyncMock()
        mock_process.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await c.start({})

        assert c._process is mock_process
        assert c._reader_task is not None

        # Clean up
        await c.stop()


class TestSignalStop:
    @pytest.mark.asyncio
    async def test_stop_sends_sigterm(self):
        """stop() should send SIGTERM to the subprocess."""
        c = SignalChannelConnector()

        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.send_signal = MagicMock()
        mock_process.wait = AsyncMock()
        mock_process.kill = MagicMock()
        c._process = mock_process
        c._reader_task = None

        await c.stop()

        import signal
        mock_process.send_signal.assert_called_once_with(signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_stop_kills_after_timeout(self):
        """stop() should SIGKILL if SIGTERM doesn't work within timeout."""
        c = SignalChannelConnector()

        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.send_signal = MagicMock()
        mock_process.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_process.kill = MagicMock()
        c._process = mock_process
        c._reader_task = None

        # Need to make wait succeed after kill
        call_count = 0
        async def wait_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.TimeoutError
            return 0

        mock_process.wait = AsyncMock(side_effect=wait_side_effect)

        await c.stop()
        mock_process.kill.assert_called_once()


class TestSignalHealthCheck:
    @pytest.mark.asyncio
    async def test_not_started(self):
        c = SignalChannelConnector()
        status = await c.health_check()
        assert status.healthy is False
        assert "not running" in status.detail

    @pytest.mark.asyncio
    async def test_process_exited(self):
        c = SignalChannelConnector()
        mock_process = MagicMock()
        mock_process.returncode = 1  # Exited
        c._process = mock_process
        status = await c.health_check()
        assert status.healthy is False

    @pytest.mark.asyncio
    async def test_process_running(self):
        c = SignalChannelConnector()
        mock_process = MagicMock()
        mock_process.returncode = None  # Still running
        mock_process.pid = 12345
        c._process = mock_process
        status = await c.health_check()
        assert status.healthy is True
        assert "12345" in status.detail


class TestSignalHandleReceive:
    @pytest.mark.asyncio
    async def test_text_message_calls_deliver_inbound(self):
        """A text message should call deliver_inbound."""
        c = SignalChannelConnector()

        params = {
            "envelope": {
                "source": "+1234567890",
                "sourceName": "Alice",
                "dataMessage": {
                    "message": "Hello from Signal",
                },
            },
        }

        with patch.object(c, "deliver_inbound", new_callable=AsyncMock) as mock_deliver:
            await c._handle_receive(params)

        mock_deliver.assert_called_once_with(
            channel_user_id="+1234567890",
            text="Hello from Signal",
            display_name="Alice",
        )

    @pytest.mark.asyncio
    async def test_no_source_ignored(self):
        """Messages without source should be ignored."""
        c = SignalChannelConnector()
        params = {"envelope": {"dataMessage": {"message": "No source"}}}

        with patch.object(c, "deliver_inbound", new_callable=AsyncMock) as mock_deliver:
            await c._handle_receive(params)

        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_text_ignored(self):
        """Messages without text should be ignored."""
        c = SignalChannelConnector()
        params = {
            "envelope": {
                "source": "+1234567890",
                "dataMessage": {},
            },
        }

        with patch.object(c, "deliver_inbound", new_callable=AsyncMock) as mock_deliver:
            await c._handle_receive(params)

        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_unauthorized_number_rejected(self):
        """Unauthorized numbers should be rejected."""
        c = SignalChannelConnector(connector_config={
            "allowed_numbers": ["+0000000000"],
        })

        mock_process = AsyncMock()
        mock_stdin = MagicMock()
        mock_stdin.write = MagicMock()
        mock_stdin.drain = AsyncMock()
        mock_process.stdin = mock_stdin
        mock_process.returncode = None
        c._process = mock_process

        params = {
            "envelope": {
                "source": "+1234567890",
                "dataMessage": {"message": "Unauthorized"},
            },
        }

        with patch.object(c, "deliver_inbound", new_callable=AsyncMock) as mock_deliver:
            await c._handle_receive(params)

        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_source_number_field_fallback(self):
        """Should handle sourceNumber field as fallback."""
        c = SignalChannelConnector()

        params = {
            "envelope": {
                "sourceNumber": "+1234567890",
                "dataMessage": {
                    "message": "Using sourceNumber",
                },
            },
        }

        with patch.object(c, "deliver_inbound", new_callable=AsyncMock) as mock_deliver:
            await c._handle_receive(params)

        mock_deliver.assert_called_once()
        assert mock_deliver.call_args.kwargs["channel_user_id"] == "+1234567890"

    @pytest.mark.asyncio
    async def test_own_account_messages_ignored(self):
        """Messages from the bot's own account should be silently ignored."""
        c = SignalChannelConnector(connector_config={
            "account": "+1111111111",
        })

        params = {
            "envelope": {
                "source": "+1111111111",
                "dataMessage": {"message": "Echo from self"},
            },
        }

        with patch.object(c, "deliver_inbound", new_callable=AsyncMock) as mock_deliver:
            await c._handle_receive(params)

        mock_deliver.assert_not_called()


class TestSignalSendMessage:
    @pytest.mark.asyncio
    async def test_send_message_writes_json_rpc(self, db):
        """send_message should write JSON-RPC to stdin."""
        c = SignalChannelConnector()

        mock_process = AsyncMock()
        mock_process.returncode = None
        # stdin.write is sync, stdin.drain is async
        mock_stdin = MagicMock()
        mock_stdin.write = MagicMock()
        mock_stdin.drain = AsyncMock()
        mock_process.stdin = mock_stdin
        c._process = mock_process

        # Insert a channel binding
        from carpenter.db import get_db
        conn = get_db()
        conn.execute(
            "INSERT INTO channel_bindings (channel_type, channel_user_id, conversation_id) "
            "VALUES ('signal', '+1234567890', 1)"
        )
        conn.commit()
        conn.close()

        result = await c.send_message(1, "Hello back!")
        assert result is True

        # Verify stdin was written
        mock_process.stdin.write.assert_called_once()
        written = mock_process.stdin.write.call_args[0][0]
        data = json.loads(written)
        assert data["method"] == "send"
        assert "+1234567890" in data["params"]["recipient"]
        assert data["params"]["message"] == "Hello back!"

    @pytest.mark.asyncio
    async def test_send_message_no_binding_returns_false(self, db):
        """send_message returns False when no channel binding exists."""
        c = SignalChannelConnector()
        mock_process = AsyncMock()
        mock_process.returncode = None
        c._process = mock_process

        result = await c.send_message(999, "Hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_message_not_started(self):
        """send_message returns False when connector not started."""
        c = SignalChannelConnector()
        result = await c.send_message(1, "Hello")
        assert result is False


class TestSignalResolveRecipient:
    def test_resolves_from_channel_bindings(self, db):
        c = SignalChannelConnector()

        from carpenter.db import get_db
        conn = get_db()
        conn.execute(
            "INSERT INTO channel_bindings (channel_type, channel_user_id, conversation_id) "
            "VALUES ('signal', '+1234567890', 42)"
        )
        conn.commit()
        conn.close()

        assert c._resolve_recipient(42) == "+1234567890"

    def test_returns_none_for_unknown_conversation(self, db):
        c = SignalChannelConnector()
        assert c._resolve_recipient(999) is None
