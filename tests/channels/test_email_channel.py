"""Tests for :class:`EmailChannelConnector`.

Mirrors ``tests/channels/test_telegram_channel.py`` — property checks,
start/stop, health_check, send_message with ``channel_bindings``
recipient resolution, plus the two email-specific paths:
``credentials_package`` secret loading and metadata-driven subject.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from carpenter.channels.base import HealthStatus
from carpenter.channels.email_channel import (
    EmailChannelConnector,
    bind_recipient,
)


# ── Properties / config ──────────────────────────────────────────────


class TestEmailChannelProperties:
    def test_kind_is_channel(self):
        c = EmailChannelConnector()
        assert c.kind == "channel"

    def test_channel_type_is_email(self):
        c = EmailChannelConnector()
        assert c.channel_type == "email"

    def test_default_disabled(self):
        c = EmailChannelConnector()
        assert c.enabled is False

    def test_enabled_via_config(self):
        c = EmailChannelConnector(connector_config={"enabled": True})
        assert c.enabled is True

    def test_custom_name(self):
        c = EmailChannelConnector(name="my-email")
        assert c.name == "my-email"

    def test_smtp_fields_from_config(self):
        c = EmailChannelConnector(connector_config={
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "smtp_username": "bot@example.com",
            "smtp_password": "hunter2",
            "smtp_from": "bot@example.com",
            "smtp_ssl": True,
        })
        assert c._smtp_host == "smtp.example.com"
        assert c._smtp_port == 465
        assert c._smtp_username == "bot@example.com"
        assert c._smtp_password == "hunter2"
        assert c._smtp_ssl is True

    def test_default_subject_prefix(self):
        c = EmailChannelConnector()
        assert c._subject_prefix == "[Carpenter]"

    def test_custom_subject_prefix(self):
        c = EmailChannelConnector(connector_config={"subject_prefix": "[Bot]"})
        assert c._subject_prefix == "[Bot]"


# ── start / stop / health ────────────────────────────────────────────


class TestEmailChannelStartStop:
    @pytest.mark.asyncio
    async def test_start_raises_without_smtp_host(self):
        c = EmailChannelConnector(connector_config={"enabled": True})
        with pytest.raises(ValueError, match="smtp_host"):
            await c.start({})

    @pytest.mark.asyncio
    async def test_start_defaults_from_to_username(self):
        c = EmailChannelConnector(connector_config={
            "enabled": True,
            "smtp_host": "smtp.example.com",
            "smtp_username": "bot@example.com",
            "smtp_password": "x",
        })
        await c.start({})
        assert c._smtp_from == "bot@example.com"

    @pytest.mark.asyncio
    async def test_start_loads_from_credentials_package(self):
        """``credentials_package`` populates empty SMTP fields."""
        c = EmailChannelConnector(connector_config={
            "enabled": True,
            "credentials_package": "carpenter-imap-email",
        })

        secrets = {
            "EMAIL_SMTP_HOST": "smtp.pkg.example",
            "EMAIL_SMTP_PORT": "587",
            "EMAIL_SMTP_USERNAME": "pkg@example.com",
            "EMAIL_SMTP_PASSWORD": "pkgpass",
            "EMAIL_SMTP_FROM": "pkg@example.com",
        }

        with patch(
            "carpenter.packages.capabilities.resolve_package_secret",
            side_effect=lambda pkg, key: secrets.get(key),
        ):
            await c.start({})

        assert c._smtp_host == "smtp.pkg.example"
        assert c._smtp_port == 587
        assert c._smtp_username == "pkg@example.com"
        assert c._smtp_password == "pkgpass"
        assert c._smtp_from == "pkg@example.com"

    @pytest.mark.asyncio
    async def test_credentials_package_does_not_override_inline(self):
        """Inline SMTP values win over package secrets."""
        c = EmailChannelConnector(connector_config={
            "enabled": True,
            "credentials_package": "carpenter-imap-email",
            "smtp_host": "inline.example",
            "smtp_username": "inline@example.com",
            "smtp_password": "inline",
        })

        secrets = {
            "EMAIL_SMTP_HOST": "pkg.example",
            "EMAIL_SMTP_USERNAME": "pkg@example.com",
            "EMAIL_SMTP_PASSWORD": "pkg",
            "EMAIL_SMTP_FROM": "pkg@example.com",
        }
        with patch(
            "carpenter.packages.capabilities.resolve_package_secret",
            side_effect=lambda pkg, key: secrets.get(key),
        ):
            await c.start({})

        assert c._smtp_host == "inline.example"
        assert c._smtp_username == "inline@example.com"
        # smtp_from was empty inline, so package secret fills it in
        # (empty fields are the ones _load_from_package targets).
        assert c._smtp_from == "pkg@example.com"

    @pytest.mark.asyncio
    async def test_stop_is_noop(self):
        c = EmailChannelConnector(connector_config={
            "enabled": True,
            "smtp_host": "smtp.example.com",
            "smtp_username": "bot@example.com",
            "smtp_password": "x",
        })
        await c.start({})
        # Must not raise; no persistent connection to tear down.
        await c.stop()


class TestEmailChannelHealth:
    @pytest.mark.asyncio
    async def test_unconfigured(self):
        c = EmailChannelConnector()
        status = await c.health_check()
        assert status.healthy is False
        assert "not configured" in status.detail

    @pytest.mark.asyncio
    async def test_configured(self):
        c = EmailChannelConnector(connector_config={
            "enabled": True,
            "smtp_host": "smtp.example.com",
            "smtp_username": "bot@example.com",
            "smtp_password": "x",
        })
        await c.start({})
        status = await c.health_check()
        assert status.healthy is True
        assert "smtp.example.com" in status.detail


# ── send_message ─────────────────────────────────────────────────────


class TestEmailChannelSend:
    @pytest.mark.asyncio
    async def test_send_message_no_binding_returns_false(self, db):
        c = EmailChannelConnector(connector_config={
            "smtp_host": "smtp.example.com",
            "smtp_username": "bot@example.com",
            "smtp_password": "x",
        })
        ok = await c.send_message(999, "hello")
        assert ok is False

    @pytest.mark.asyncio
    async def test_send_message_calls_smtp_with_binding(self, db):
        """Recipient resolved from channel_bindings, SMTP invoked."""
        c = EmailChannelConnector(connector_config={
            "smtp_host": "smtp.example.com",
            "smtp_username": "bot@example.com",
            "smtp_password": "x",
            "smtp_from": "bot@example.com",
        })

        db.execute(
            "INSERT INTO channel_bindings "
            "(channel_type, channel_user_id, conversation_id) "
            "VALUES ('email', 'op@example.com', 42)"
        )
        db.commit()

        with patch(
            "carpenter.core.notifications._send_email_smtp",
            return_value=True,
        ) as mock_send:
            ok = await c.send_message(42, "body text")

        assert ok is True
        mock_send.assert_called_once()
        email_config, message, subject = mock_send.call_args[0]
        assert email_config["smtp_to"] == "op@example.com"
        assert email_config["smtp_host"] == "smtp.example.com"
        assert email_config["smtp_from"] == "bot@example.com"
        assert message == "body text"
        # Default subject uses the prefix + snippet.
        assert subject.startswith("[Carpenter]")

    @pytest.mark.asyncio
    async def test_send_message_metadata_overrides_subject(self, db):
        c = EmailChannelConnector(connector_config={
            "smtp_host": "smtp.example.com",
            "smtp_username": "bot@example.com",
            "smtp_password": "x",
        })
        db.execute(
            "INSERT INTO channel_bindings "
            "(channel_type, channel_user_id, conversation_id) "
            "VALUES ('email', 'op@example.com', 7)"
        )
        db.commit()

        with patch(
            "carpenter.core.notifications._send_email_smtp",
            return_value=True,
        ) as mock_send:
            ok = await c.send_message(
                7, "body", metadata={"subject": "Custom subject"},
            )
        assert ok is True
        _, _, subject = mock_send.call_args[0]
        assert subject == "Custom subject"

    @pytest.mark.asyncio
    async def test_send_message_default_subject_includes_arc_id(self, db):
        """When ``metadata['arc_id']`` is set, it appears in the subject."""
        c = EmailChannelConnector(connector_config={
            "smtp_host": "smtp.example.com",
            "smtp_username": "bot@example.com",
            "smtp_password": "x",
        })
        db.execute(
            "INSERT INTO channel_bindings "
            "(channel_type, channel_user_id, conversation_id) "
            "VALUES ('email', 'op@example.com', 7)"
        )
        db.commit()

        with patch(
            "carpenter.core.notifications._send_email_smtp",
            return_value=True,
        ) as mock_send:
            await c.send_message(
                7, "reflection ran", metadata={"arc_id": 42},
            )
        _, _, subject = mock_send.call_args[0]
        assert "arc 42" in subject

    @pytest.mark.asyncio
    async def test_send_message_swallows_smtp_exception(self, db):
        c = EmailChannelConnector(connector_config={
            "smtp_host": "smtp.example.com",
            "smtp_username": "bot@example.com",
            "smtp_password": "x",
        })
        db.execute(
            "INSERT INTO channel_bindings "
            "(channel_type, channel_user_id, conversation_id) "
            "VALUES ('email', 'op@example.com', 7)"
        )
        db.commit()

        def boom(*a, **kw):
            raise RuntimeError("smtp exploded")

        with patch(
            "carpenter.core.notifications._send_email_smtp",
            side_effect=boom,
        ):
            ok = await c.send_message(7, "body")
        assert ok is False


# ── bind_recipient ───────────────────────────────────────────────────


class TestBindRecipient:
    def test_bind_recipient_inserts_row(self, db):
        bind_recipient(99, "op@example.com")
        row = db.execute(
            "SELECT channel_user_id, conversation_id "
            "FROM channel_bindings "
            "WHERE channel_type = 'email' AND conversation_id = ?",
            (99,),
        ).fetchone()
        assert row["channel_user_id"] == "op@example.com"

    def test_bind_recipient_upserts_conversation(self, db):
        """Second call for same recipient updates the conversation_id."""
        bind_recipient(1, "op@example.com")
        bind_recipient(2, "op@example.com")
        rows = db.execute(
            "SELECT conversation_id FROM channel_bindings "
            "WHERE channel_type = 'email' AND channel_user_id = ?",
            ("op@example.com",),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["conversation_id"] == 2
