"""Tests for SignalChannelConnector — REST API mode."""

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


# ---- REST API mode tests ----


def _rest_api_config(**overrides):
    """Build a connector_config dict for rest_api mode."""
    base = {
        "enabled": True,
        "mode": "rest_api",
        "rest_api_url": "http://localhost:8080",
        "account": "+1234567890",
    }
    base.update(overrides)
    return base


class TestSignalRestApiConfig:
    def test_mode_defaults_to_subprocess(self):
        c = SignalChannelConnector()
        assert c._mode == "subprocess"

    def test_mode_from_config(self):
        c = SignalChannelConnector(connector_config=_rest_api_config())
        assert c._mode == "rest_api"

    def test_rest_api_url_from_config(self):
        c = SignalChannelConnector(connector_config=_rest_api_config(
            rest_api_url="http://myhost:9090",
        ))
        assert c._rest_api_url == "http://myhost:9090"

    def test_webhook_path_default(self):
        c = SignalChannelConnector(connector_config=_rest_api_config())
        assert c._webhook_path == "/hooks/signal"

    def test_webhook_path_custom(self):
        c = SignalChannelConnector(connector_config=_rest_api_config(
            webhook_path="/my/webhook",
        ))
        assert c._webhook_path == "/my/webhook"

    def test_router_initially_none(self):
        c = SignalChannelConnector(connector_config=_rest_api_config())
        assert c.routes is None

    def test_client_initially_none(self):
        c = SignalChannelConnector(connector_config=_rest_api_config())
        assert c._client is None


class TestSignalRestApiStart:
    @pytest.mark.asyncio
    async def test_start_raises_without_account(self):
        c = SignalChannelConnector(connector_config={
            "enabled": True,
            "mode": "rest_api",
            "rest_api_url": "http://localhost:8080",
        })
        with pytest.raises(ValueError, match="account"):
            await c.start({})

    @pytest.mark.asyncio
    async def test_start_raises_without_rest_api_url(self):
        c = SignalChannelConnector(connector_config={
            "enabled": True,
            "mode": "rest_api",
            "account": "+1234567890",
        })
        with pytest.raises(ValueError, match="rest_api_url"):
            await c.start({})

    @pytest.mark.asyncio
    async def test_start_raises_on_health_check_failure(self):
        c = SignalChannelConnector(connector_config=_rest_api_config())

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ConnectionError, match="Cannot reach"):
                await c.start({})

        # Client should be cleaned up after failure
        assert c._client is None
        mock_client.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_creates_client_and_router(self):
        c = SignalChannelConnector(connector_config=_rest_api_config())

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_client):
            await c.start({})

        assert c._client is mock_client
        assert c.routes is not None
        assert c._last_healthy is not None

        # Clean up
        await c.stop()

    @pytest.mark.asyncio
    async def test_start_calls_health_endpoint(self):
        c = SignalChannelConnector(connector_config=_rest_api_config())

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_client):
            await c.start({})

        mock_client.get.assert_called_once_with("http://localhost:8080/v1/health")
        await c.stop()


class TestSignalRestApiStop:
    @pytest.mark.asyncio
    async def test_stop_closes_client(self):
        c = SignalChannelConnector(connector_config=_rest_api_config())
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.aclose = AsyncMock()
        c._client = mock_client

        await c.stop()

        mock_client.aclose.assert_called_once()
        assert c._client is None

    @pytest.mark.asyncio
    async def test_stop_noop_when_not_started(self):
        c = SignalChannelConnector(connector_config=_rest_api_config())
        # Should not raise
        await c.stop()


class TestSignalRestApiHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy_on_200(self):
        c = SignalChannelConnector(connector_config=_rest_api_config())
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        c._client = mock_client

        status = await c.health_check()
        assert status.healthy is True
        assert "rest_api" in status.detail
        mock_client.get.assert_called_once_with("http://localhost:8080/v1/health")

    @pytest.mark.asyncio
    async def test_unhealthy_on_error(self):
        c = SignalChannelConnector(connector_config=_rest_api_config())
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        c._client = mock_client

        status = await c.health_check()
        assert status.healthy is False

    @pytest.mark.asyncio
    async def test_unhealthy_when_not_started(self):
        c = SignalChannelConnector(connector_config=_rest_api_config())
        status = await c.health_check()
        assert status.healthy is False
        assert "not started" in status.detail


class TestSignalRestApiSendMessage:
    @pytest.mark.asyncio
    async def test_send_posts_to_v2_send(self, db):
        c = SignalChannelConnector(connector_config=_rest_api_config())

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)
        c._client = mock_client

        # Insert a channel binding
        from carpenter.db import get_db
        conn = get_db()
        conn.execute(
            "INSERT INTO channel_bindings (channel_type, channel_user_id, conversation_id) "
            "VALUES ('signal', '+9876543210', 1)"
        )
        conn.commit()
        conn.close()

        result = await c.send_message(1, "Hello via REST!")
        assert result is True

        mock_client.post.assert_called_once_with(
            "http://localhost:8080/v2/send",
            json={
                "message": "Hello via REST!",
                "number": "+1234567890",
                "recipients": ["+9876543210"],
            },
        )

    @pytest.mark.asyncio
    async def test_send_returns_false_on_http_error(self, db):
        c = SignalChannelConnector(connector_config=_rest_api_config())

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())
        )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)
        c._client = mock_client

        from carpenter.db import get_db
        conn = get_db()
        conn.execute(
            "INSERT INTO channel_bindings (channel_type, channel_user_id, conversation_id) "
            "VALUES ('signal', '+9876543210', 1)"
        )
        conn.commit()
        conn.close()

        result = await c.send_message(1, "Will fail")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_returns_false_no_binding(self, db):
        c = SignalChannelConnector(connector_config=_rest_api_config())
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        c._client = mock_client

        result = await c.send_message(999, "No binding")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_returns_false_not_started(self):
        c = SignalChannelConnector(connector_config=_rest_api_config())
        result = await c.send_message(1, "Not started")
        assert result is False


class TestSignalRestApiWebhook:
    def _make_started_connector(self):
        """Create a connector with router set up (simulating post-start state)."""
        c = SignalChannelConnector(connector_config=_rest_api_config())
        c._setup_webhook()
        return c

    @pytest.mark.asyncio
    async def test_webhook_json_rpc_format(self):
        """Webhook handles JSON-RPC wrapped payloads."""
        c = self._make_started_connector()

        # JSON-RPC format from signal-cli-rest-api
        # Source must differ from account (+1234567890) to avoid self-message filter
        body = {
            "method": "receive",
            "params": {
                "envelope": {
                    "source": "+9999999999",
                    "sourceName": "Alice",
                    "dataMessage": {"message": "Hello via webhook"},
                },
            },
        }

        with patch.object(c, "deliver_inbound", new_callable=AsyncMock) as mock_deliver:
            with patch.object(c, "_handle_receive", wraps=c._handle_receive):
                await c._handle_receive(body["params"])

        mock_deliver.assert_called_once_with(
            channel_user_id="+9999999999",
            text="Hello via webhook",
            display_name="Alice",
        )

    @pytest.mark.asyncio
    async def test_webhook_raw_envelope_format(self):
        """Webhook handles raw envelope payloads."""
        c = self._make_started_connector()

        # Source must differ from account (+1234567890) to avoid self-message filter
        params = {
            "envelope": {
                "source": "+9999999999",
                "sourceName": "Bob",
                "dataMessage": {"message": "Direct envelope"},
            },
        }

        with patch.object(c, "deliver_inbound", new_callable=AsyncMock) as mock_deliver:
            await c._handle_receive(params)

        mock_deliver.assert_called_once_with(
            channel_user_id="+9999999999",
            text="Direct envelope",
            display_name="Bob",
        )

    @pytest.mark.asyncio
    async def test_webhook_router_has_post_route(self):
        """The webhook router should have a POST route at the webhook path."""
        c = self._make_started_connector()
        assert c.routes is not None

        post_routes = [r for r in c.routes if hasattr(r, "methods") and "POST" in r.methods]
        assert len(post_routes) == 1
        assert post_routes[0].path == "/hooks/signal"

    @pytest.mark.asyncio
    async def test_webhook_custom_path(self):
        """The webhook router should use the configured path."""
        c = SignalChannelConnector(connector_config=_rest_api_config(
            webhook_path="/custom/signal",
        ))
        c._setup_webhook()

        post_routes = [r for r in c.routes if hasattr(r, "methods") and "POST" in r.methods]
        assert post_routes[0].path == "/custom/signal"

    @pytest.mark.asyncio
    async def test_webhook_dispatches_to_handle_receive(self):
        """The actual webhook endpoint should call _handle_receive."""
        from starlette.testclient import TestClient
        from starlette.applications import Starlette

        c = self._make_started_connector()

        app = Starlette(routes=c.routes)

        body = {
            "method": "receive",
            "params": {
                "envelope": {
                    "source": "+5555555555",
                    "dataMessage": {"message": "Integration test"},
                },
            },
        }

        with patch.object(c, "_handle_receive", new_callable=AsyncMock) as mock_handle:
            client = TestClient(app)
            resp = client.post("/hooks/signal", json=body)

        assert resp.status_code == 200
        mock_handle.assert_called_once()
        call_args = mock_handle.call_args[0][0]
        assert call_args["envelope"]["source"] == "+5555555555"

    @pytest.mark.asyncio
    async def test_webhook_raw_envelope_dispatches(self):
        """Webhook endpoint handles raw envelope (no params wrapper)."""
        from starlette.testclient import TestClient
        from starlette.applications import Starlette

        c = self._make_started_connector()

        app = Starlette(routes=c.routes)

        body = {
            "envelope": {
                "source": "+5555555555",
                "dataMessage": {"message": "Raw envelope"},
            },
        }

        with patch.object(c, "_handle_receive", new_callable=AsyncMock) as mock_handle:
            client = TestClient(app)
            resp = client.post("/hooks/signal", json=body)

        assert resp.status_code == 200
        mock_handle.assert_called_once()
        call_args = mock_handle.call_args[0][0]
        assert "envelope" in call_args

    @pytest.mark.asyncio
    async def test_webhook_unrecognized_body_returns_200(self):
        """Webhook returns 200 for unrecognized payloads (no error)."""
        from starlette.testclient import TestClient
        from starlette.applications import Starlette

        c = self._make_started_connector()

        app = Starlette(routes=c.routes)

        with patch.object(c, "_handle_receive", new_callable=AsyncMock) as mock_handle:
            client = TestClient(app)
            resp = client.post("/hooks/signal", json={"something": "else"})

        assert resp.status_code == 200
        mock_handle.assert_not_called()


class TestSignalRestApiRejectUnauthorized:
    @pytest.mark.asyncio
    async def test_unauthorized_uses_rest_api_reject(self):
        """In rest_api mode, unauthorized messages send rejection via HTTP."""
        c = SignalChannelConnector(connector_config=_rest_api_config(
            allowed_numbers=["+0000000000"],
        ))

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)
        c._client = mock_client

        # Source must differ from account (+1234567890) to avoid self-message filter
        params = {
            "envelope": {
                "source": "+9999999999",
                "dataMessage": {"message": "Unauthorized"},
            },
        }

        with patch.object(c, "deliver_inbound", new_callable=AsyncMock) as mock_deliver:
            await c._handle_receive(params)

        mock_deliver.assert_not_called()
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert call_kwargs[0][0] == "http://localhost:8080/v2/send"
        payload = call_kwargs[1]["json"]
        assert payload["recipients"] == ["+9999999999"]
        assert "not authorized" in payload["message"]
