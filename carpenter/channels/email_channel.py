"""Email channel connector — outbound SMTP delivery.

Modelled directly on :class:`SignalChannelConnector`:

* ``channel_type = "email"``.
* Recipient (the destination address) is stored in ``channel_bindings``
  keyed on the conversation id, so every conversation-medium concept
  from the other channels applies unchanged: one binding per
  conversation, one recipient per binding.
* SMTP settings come from ``connector_config`` in the same shape the
  Telegram/Signal factories use.  Two credential sources are supported,
  mutually exclusive:

  1. ``credentials_package: <name>`` — resolve
     ``EMAIL_SMTP_{HOST,PORT,USERNAME,PASSWORD,FROM}`` from the named
     capability package's per-package ``.env`` via
     :func:`resolve_package_secret`.  This is the existing
     ``carpenter-imap-email`` coupling, preserved so the connector-port
     doesn't force a credential-store migration.
  2. Inline ``smtp_host`` / ``smtp_port`` / ``smtp_username`` /
     ``smtp_password`` / ``smtp_from`` — standard fields alongside the
     other connector configs, useful when SMTP creds are not managed
     by a capability package.

Inbound is out of scope — no IMAP polling, no webhook.  The
``deliver_inbound()`` machinery on :class:`ChannelConnector` remains
available for a future inbound path.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from .base import HealthStatus
from .channel import ChannelConnector

logger = logging.getLogger(__name__)

# Package-secret keys we accept when ``credentials_package`` is set.
_PACKAGE_SECRET_MAP = {
    "smtp_host": "EMAIL_SMTP_HOST",
    "smtp_port": "EMAIL_SMTP_PORT",
    "smtp_username": "EMAIL_SMTP_USERNAME",
    "smtp_password": "EMAIL_SMTP_PASSWORD",
    "smtp_from": "EMAIL_SMTP_FROM",
}


class EmailChannelConnector(ChannelConnector):
    """Outbound-only email connector.

    See module docstring for the two credential-source modes.
    """

    channel_type = "email"

    def __init__(
        self,
        name: str = "email",
        connector_config: dict | None = None,
    ) -> None:
        self.name = name
        cc = connector_config or {}
        self.enabled = cc.get("enabled", False)
        self._credentials_package: str | None = cc.get("credentials_package")
        self._smtp_host = cc.get("smtp_host", "") or ""
        self._smtp_port: int = int(cc.get("smtp_port") or 587)
        self._smtp_username = cc.get("smtp_username", "") or ""
        self._smtp_password = cc.get("smtp_password", "") or ""
        self._smtp_from = cc.get("smtp_from", "") or ""
        self._smtp_tls: bool = bool(cc.get("smtp_tls", True))
        self._smtp_ssl: bool = bool(cc.get("smtp_ssl", False))
        self._smtp_timeout: float = float(cc.get("smtp_timeout", 30))
        self._subject_prefix: str = cc.get("subject_prefix", "[Carpenter]")
        self._last_healthy: datetime | None = None
        # Set True at the end of ``start()`` after credentials are
        # materialised.  The reflection gate consults this before opening
        # — a connector that raised in ``start()`` (and was therefore
        # left in the registry by :meth:`ConnectorRegistry.start_all` but
        # never fully initialised) must not report ready.
        self.started: bool = False

    # -- Lifecycle ------------------------------------------------------

    async def start(self, config: dict) -> None:
        """Resolve credentials and validate config.

        No persistent connection: SMTP is per-send.  All this does is
        materialise the SMTP settings so a caller-facing failure at
        :meth:`send_message` is a network problem, not a config problem.
        """
        if self._credentials_package:
            self._load_from_package(self._credentials_package)

        if not self._smtp_host:
            raise ValueError(
                f"Email connector {self.name!r}: smtp_host is required "
                "(inline or via credentials_package)"
            )
        if not self._smtp_from:
            # Match _send_email_smtp's fallback so callers see the
            # username in the From header rather than a synthetic
            # ``carpenter@<host>`` address when a real sender was
            # available all along.
            self._smtp_from = self._smtp_username or ""

        self._last_healthy = datetime.now()
        self.started = True
        logger.info(
            "Email connector %r started (host=%s, from=%s)",
            self.name, self._smtp_host, self._smtp_from or "<unset>",
        )

    async def stop(self) -> None:
        """No persistent connection to tear down."""
        self.started = False
        return None

    async def health_check(self) -> HealthStatus:
        """Report configuration-presence health.

        A live SMTP handshake here would burn latency + auth cycles on
        every heartbeat, so we only report whether the connector is
        configured enough to attempt a send.  A failed send in
        :meth:`send_message` will log with a traceback separately.
        """
        healthy = bool(self._smtp_host and self._smtp_username)
        detail = (
            f"host={self._smtp_host}" if healthy else "SMTP not configured"
        )
        return HealthStatus(
            healthy=healthy,
            detail=detail,
            last_seen=self._last_healthy,
        )

    # -- Send -----------------------------------------------------------

    async def send_message(
        self,
        conversation_id: int,
        text: str,
        metadata: dict | None = None,
    ) -> bool:
        """Send ``text`` to the recipient bound to ``conversation_id``.

        ``metadata['subject']`` overrides the auto-derived subject when
        supplied.  ``metadata['arc_id']`` is included in the default
        subject for traceability (mirrors the previous
        ``[Reflection] arc N: ...`` shape) when subject is auto-derived.
        """
        recipient = self._resolve_recipient(conversation_id)
        if recipient is None:
            logger.warning(
                "Email connector %r: no recipient bound to conversation %s — "
                "skipping send",
                self.name, conversation_id,
            )
            return False

        md = metadata or {}
        subject = md.get("subject") or self._default_subject(
            text, md.get("arc_id")
        )
        email_config = self._email_config(recipient)

        # smtplib is blocking; keep the event loop responsive.
        from ..core.notifications import _send_email_smtp

        try:
            return await asyncio.to_thread(
                _send_email_smtp, email_config, text, subject
            )
        except Exception:  # noqa: BLE001 — SMTP failure must not raise
            logger.exception(
                "Email connector %r: send failed for conversation %s",
                self.name, conversation_id,
            )
            return False

    # -- Internal -------------------------------------------------------

    def _resolve_recipient(self, conversation_id: int) -> str | None:
        """Look up the destination email for a conversation."""
        from ..db import get_db
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT channel_user_id FROM channel_bindings "
                "WHERE channel_type = 'email' AND conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            return row["channel_user_id"] if row else None
        finally:
            conn.close()

    def _default_subject(self, text: str, arc_id: int | None) -> str:
        snippet = " ".join(text.split())[:60].strip() or "(no summary)"
        if arc_id is not None:
            return f"{self._subject_prefix} arc {arc_id}: {snippet}"
        return f"{self._subject_prefix} {snippet}"

    def _email_config(self, recipient: str) -> dict:
        return {
            "smtp_host": self._smtp_host,
            "smtp_port": self._smtp_port,
            "smtp_from": self._smtp_from or self._smtp_username,
            "smtp_to": recipient,
            "smtp_username": self._smtp_username,
            "smtp_password": self._smtp_password,
            "smtp_tls": self._smtp_tls,
            "smtp_ssl": self._smtp_ssl,
            "smtp_timeout": self._smtp_timeout,
        }

    def _load_from_package(self, package_name: str) -> None:
        """Populate empty SMTP fields from a capability package's secrets.

        Only fields that were not set inline are overwritten — inline
        config wins so a call site can pin a specific value.
        """
        from ..packages.capabilities import resolve_package_secret

        for attr, env_key in _PACKAGE_SECRET_MAP.items():
            current = getattr(self, f"_{attr}", "")
            if current:
                continue
            value = resolve_package_secret(package_name, env_key)
            if value is None:
                continue
            if attr == "smtp_port":
                try:
                    setattr(self, "_smtp_port", int(value))
                except (TypeError, ValueError):
                    logger.warning(
                        "Email connector %r: invalid smtp_port %r from "
                        "package %r; keeping default %d",
                        self.name, value, package_name, self._smtp_port,
                    )
            else:
                setattr(self, f"_{attr}", value)


def bind_recipient(conversation_id: int, email_address: str) -> None:
    """Idempotently record ``email_address`` as the recipient for a
    conversation.

    Matches the shape SignalChannel uses for phone-number bindings —
    ``channel_bindings(channel_type, channel_user_id, conversation_id)``.
    Callers that create an outbound email-medium conversation (e.g. the
    reflection-home) should call this at creation time so
    :meth:`EmailChannelConnector.send_message` can resolve a recipient.
    """
    from ..db import get_db
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO channel_bindings "
            "(channel_type, channel_user_id, display_name, conversation_id) "
            "VALUES ('email', ?, ?, ?) "
            "ON CONFLICT(channel_type, channel_user_id) "
            "DO UPDATE SET conversation_id = ?",
            (email_address, email_address, conversation_id, conversation_id),
        )
        conn.commit()
    finally:
        conn.close()
