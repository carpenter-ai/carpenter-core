"""Reflection escalation gate + email-medium conversation dispatch.

Reflection SUPERVISOR arcs have NO originating chat conversation — the daily
cron fires them without a user in the loop.  When their coding-change
follow-ups produce human-review URLs, the existing
``notify_arc_conversation`` path had no conversation to inject the URL into,
so the URLs piled up in the DB unseen.  This module fixes that with two
pieces:

1.  :func:`ensure_escalation_ready` — a config-presence gate that
    :mod:`config_seed.templates.reflection.daily_tick` calls at the very
    top of ``handle_reflection_tick``.  If the escalation destination is
    not fully configured (no ``reflection.escalation.email.to`` in the
    platform config, or the ``carpenter-imap-email`` SMTP creds don't
    resolve), the daily tick refuses to run rather than burning tokens
    on reflections whose action outputs no one will see.

2.  :func:`get_or_create_reflection_home_conversation` +
    :func:`dispatch_email_message` — a virtual conversation with
    ``channel_type='email'`` that reflection arcs are linked to.  When
    ``conversation.add_message`` writes into a conversation whose
    ``channel_type`` is ``'email'``, it also calls
    :func:`dispatch_email_message`, which reuses
    :func:`carpenter.core.notifications._send_email_smtp` to route the
    message body (highlighting any embedded review URLs) via SMTP.

This preserves ALL of the existing arc→conversation-notify code
(``arc.chat_notify``, ``link_arc_to_conversation``, etc.); we only
generalise the delivery side of :func:`conversation.add_message`.
"""

from __future__ import annotations

import logging
import re

from .. import config
from ..db import db_connection, db_transaction
from ..packages.capabilities import resolve_package_secret

logger = logging.getLogger(__name__)

# The per-package env keys we require from carpenter-imap-email for SMTP send.
_REQUIRED_SMTP_KEYS = ("EMAIL_SMTP_HOST", "EMAIL_SMTP_USERNAME", "EMAIL_SMTP_PASSWORD")

# The package that owns the SMTP credentials.  Reuses the existing
# carpenter-imap-email package's per-package .env rather than defining a
# parallel credential store just for reflection.
_SMTP_CRED_PACKAGE = "carpenter-imap-email"

# Marker title used to look up the reflection-home conversation idempotently.
REFLECTION_HOME_TITLE = "Reflection escalation"

# Review-URL pattern that we highlight at the top of the email body when
# present, so a human skimming the inbox can act without opening the message.
_REVIEW_URL_RE = re.compile(r"https?://[^\s]+/api/review/[a-f0-9-]+")


def _escalation_email_to() -> str | None:
    """Return the configured escalation email address, or None if unset."""
    to = (
        config.CONFIG.get("reflection", {})
        .get("escalation", {})
        .get("email", {})
        .get("to")
    )
    if isinstance(to, str) and "@" in to and to.strip():
        return to.strip()
    return None


def ensure_escalation_ready() -> bool:
    """Return True iff a reflection can safely be started.

    The invariant: reflection MUST refuse to run unless an escalation
    destination is configured.  We check two things (config-presence only —
    no live SMTP handshake, which would burn latency on every daily tick):

    * ``config.CONFIG['reflection']['escalation']['email']['to']`` is a
      non-empty string that contains ``@``.
    * ``EMAIL_SMTP_HOST``, ``EMAIL_SMTP_USERNAME``, ``EMAIL_SMTP_PASSWORD``
      all resolve to non-empty values from the ``carpenter-imap-email``
      package's credential store (via
      :func:`carpenter.packages.capabilities.resolve_package_secret`).

    Returns ``True`` iff every check passes, else ``False``.
    """
    if _escalation_email_to() is None:
        return False
    for key in _REQUIRED_SMTP_KEYS:
        value = resolve_package_secret(_SMTP_CRED_PACKAGE, key)
        if not value:
            return False
    return True


def get_or_create_reflection_home_conversation() -> int:
    """Return the id of the reflection-home email-medium conversation.

    Looks up (by title + ``channel_type='email'``) an existing
    conversation; if none exists, creates one.  Idempotent — safe to call
    on every daily tick.  The returned conversation is the target for
    reflection SUPERVISOR arcs' ``link_arc_to_conversation`` so that the
    existing ``arc.chat_notify`` pathway routes reflection completion
    messages (including any pending-review URLs) into the email medium.
    """
    # Always un-archive an existing reflection-home in the same transaction we
    # find it in.  ``arc_notify_handler`` discards archived conversations and
    # falls back to ``get_last_conversation()`` (a chat conv), so a stale
    # archived flag silently reroutes escalation email into an ordinary chat
    # thread the user does not monitor.  The reflection-home is an outbound
    # delivery endpoint, not a browsable conversation — archiving it has no
    # legitimate semantics for this code path.
    with db_transaction() as db:
        row = db.execute(
            "SELECT id, archived FROM conversations "
            "WHERE title = ? AND channel_type = 'email' "
            "ORDER BY id ASC LIMIT 1",
            (REFLECTION_HOME_TITLE,),
        ).fetchone()
        if row is not None:
            if row["archived"]:
                db.execute(
                    "UPDATE conversations SET archived = 0 WHERE id = ?",
                    (row["id"],),
                )
                logger.info(
                    "reflection_escalation: un-archived reflection-home "
                    "conversation %d (archived flag was stale)",
                    row["id"],
                )
            return int(row["id"])
        cursor = db.execute(
            "INSERT INTO conversations (title, channel_type, last_message_at) "
            "VALUES (?, 'email', CURRENT_TIMESTAMP)",
            (REFLECTION_HOME_TITLE,),
        )
        return int(cursor.lastrowid)


def _build_email_config() -> dict:
    """Assemble the ``email_config`` dict consumed by ``_send_email_smtp``.

    Pulls host/port/username/password from the ``carpenter-imap-email``
    package's per-package ``.env`` (via
    :func:`resolve_package_secret`) and the ``to`` address from
    :data:`config.CONFIG`.  Mirrors the shape of the ``notifications.email``
    config used by :mod:`carpenter.core.notifications` so we can reuse
    :func:`carpenter.core.notifications._send_email_smtp` unchanged.
    """
    host = resolve_package_secret(_SMTP_CRED_PACKAGE, "EMAIL_SMTP_HOST") or ""
    port_raw = resolve_package_secret(_SMTP_CRED_PACKAGE, "EMAIL_SMTP_PORT")
    try:
        port = int(port_raw) if port_raw else 587
    except (TypeError, ValueError):
        port = 587
    username = resolve_package_secret(_SMTP_CRED_PACKAGE, "EMAIL_SMTP_USERNAME") or ""
    password = resolve_package_secret(_SMTP_CRED_PACKAGE, "EMAIL_SMTP_PASSWORD") or ""
    from_addr = (
        resolve_package_secret(_SMTP_CRED_PACKAGE, "EMAIL_SMTP_FROM")
        or username
        or ""
    )
    to_addr = _escalation_email_to() or ""
    return {
        "smtp_host": host,
        "smtp_port": port,
        "smtp_from": from_addr,
        "smtp_to": to_addr,
        "smtp_username": username,
        "smtp_password": password,
        "smtp_tls": True,
    }


def _format_subject(message: str, arc_id: int | None) -> str:
    """Build the outgoing email Subject line."""
    body_snippet = " ".join(message.split())[:60].strip()
    if not body_snippet:
        body_snippet = "(no summary)"
    if arc_id is not None:
        return f"[Reflection] arc {arc_id}: {body_snippet}"
    return f"[Reflection] {body_snippet}"


def _format_body(message: str) -> str:
    """Build the outgoing email body, highlighting any review URLs on top."""
    urls = _REVIEW_URL_RE.findall(message)
    if not urls:
        return message
    dedup: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            dedup.append(url)
            seen.add(url)
    header_lines = ["Pending review URLs:"] + [f"  - {u}" for u in dedup]
    return "\n".join(header_lines) + "\n\n" + message


def dispatch_email_message(
    conversation_id: int,
    role: str,
    message: str,
    arc_id: int | None = None,
) -> bool:
    """Send ``message`` for an email-medium conversation via SMTP.

    Called by :func:`carpenter.agent.conversation.add_message` whenever a
    message lands in a conversation with ``channel_type='email'``.  This is
    best-effort: an SMTP failure is logged but never raised — the DB row
    is already durable, so a caller-observable exception would be a
    regression.

    ``role='user'`` messages are skipped entirely, both because an
    email-medium conversation has no genuine user turn to echo back and
    because echoing user turns would build a trivial send-loop with any
    future inbound-email adapter.

    Returns ``True`` if SMTP delivery succeeded, ``False`` on skip or
    failure.
    """
    if role == "user":
        return False
    # Late import so importing this module doesn't force the smtplib +
    # email.mime deps on non-reflection callers.
    from .notifications import _send_email_smtp

    email_config = _build_email_config()
    if not email_config["smtp_host"] or not email_config["smtp_to"]:
        logger.warning(
            "reflection_escalation: cannot dispatch email for conversation %d — "
            "SMTP host or destination is not configured",
            conversation_id,
        )
        return False

    subject = _format_subject(message, arc_id)
    body = _format_body(message)
    try:
        return bool(_send_email_smtp(email_config, body, subject))
    except Exception:  # broad catch: SMTP failure must not break add_message
        logger.exception(
            "reflection_escalation: SMTP dispatch failed for conversation %d",
            conversation_id,
        )
        return False
