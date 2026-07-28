"""Reflection escalation gate + reflection-home conversation.

Reflection SUPERVISOR arcs have no originating chat conversation — the
daily cron fires them without a user in the loop.  When their
coding-change follow-ups produce human-review URLs, those URLs need to
land somewhere a human actually watches; this module owns the plumbing
that makes that possible:

1.  :func:`ensure_escalation_ready` — a config-presence gate that
    :mod:`config_seed.templates.reflection.daily_tick` calls at the very
    top of ``handle_reflection_tick``.  Refuses to run unless a
    destination email address is configured *and* the ``email`` channel
    connector is enabled — no point burning reflection tokens on
    outputs no one will see.

2.  :func:`get_or_create_reflection_home_conversation` — the virtual
    email-medium conversation that reflection arcs link to.  Also
    seeds a ``channel_bindings`` row so the email connector can
    resolve a recipient for it.

3.  :func:`format_reflection_email_body` — reflection-specific body
    prep that surfaces any pending human-review URLs at the top so a
    skim of the inbox is actionable without opening the message.

Actual SMTP delivery is
:meth:`carpenter.channels.email_channel.EmailChannelConnector.send_message` —
this module no longer touches ``smtplib`` directly.
"""

from __future__ import annotations

import logging
import re

from .. import config
from ..db import db_transaction

logger = logging.getLogger(__name__)

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


def _get_email_connector():
    """Return the enabled+started email connector, or ``None``.

    Kept as a module-level function so tests can monkey-patch a single
    entry point instead of reaching into the registry.

    A connector that raised during :meth:`start` is left in the registry
    (:meth:`ConnectorRegistry.start_all` logs the exception and moves
    on) but its ``started`` flag stays False.  Treating it as ready
    would let :func:`ensure_escalation_ready` pass — reflection would
    fire, produce human-review URLs, and the send would silently fail
    inside the half-initialised connector.  Guarding on ``started``
    keeps the "reflection needs an async delivery channel" invariant
    honest.
    """
    from ..channels.registry import get_connector_registry

    reg = get_connector_registry()
    if reg is None:
        return None
    for connector in reg.list_connectors(kind="channel"):
        if getattr(connector, "channel_type", None) != "email":
            continue
        if not connector.enabled:
            continue
        # ``started`` is set by EmailChannelConnector at the end of
        # start(); a connector without the attribute (e.g. a stub in
        # tests) is treated as ready to preserve monkey-patch ergonomics.
        if not getattr(connector, "started", True):
            continue
        return connector
    return None


def ensure_escalation_ready() -> bool:
    """Return True iff a reflection can safely be started.

    The invariant: reflection MUST refuse to run unless an escalation
    destination is configured and a delivery connector is available.

    Two config-presence checks (no live SMTP handshake — that would burn
    latency on every daily tick):

    * ``config.CONFIG['reflection']['escalation']['email']['to']`` is a
      non-empty string containing ``@``.
    * An ``email``-typed :class:`ChannelConnector` is registered and
      enabled.  The connector itself is responsible for validating SMTP
      credentials at :meth:`start` — a broken one prevents startup
      rather than silently swallowing every reflection.
    """
    if _escalation_email_to() is None:
        return False
    if _get_email_connector() is None:
        return False
    return True


def get_or_create_reflection_home_conversation() -> int:
    """Return the id of the reflection-home email-medium conversation.

    Looks up (by title + ``channel_type='email'``) an existing
    conversation; if none exists, creates one.  Idempotent — safe to
    call on every daily tick.

    Also seeds a ``channel_bindings`` row so
    :meth:`EmailChannelConnector.send_message` can resolve the
    destination email for the conversation.  Both are done inside the
    same transaction so a partial state (conversation without a
    binding) is impossible on the happy path.

    Un-archives an existing reflection-home if it's flagged archived —
    the reflection-home is an outbound delivery endpoint, not a
    browsable conversation; archiving it silently rerouted escalation
    email into an ordinary chat thread pre-fix.
    """
    to_addr = _escalation_email_to()
    with db_transaction() as db:
        row = db.execute(
            "SELECT id, archived FROM conversations "
            "WHERE title = ? AND channel_type = 'email' "
            "ORDER BY id ASC LIMIT 1",
            (REFLECTION_HOME_TITLE,),
        ).fetchone()
        if row is not None:
            conv_id = int(row["id"])
            if row["archived"]:
                db.execute(
                    "UPDATE conversations SET archived = 0 WHERE id = ?",
                    (conv_id,),
                )
                logger.info(
                    "reflection_escalation: un-archived reflection-home "
                    "conversation %d (archived flag was stale)",
                    conv_id,
                )
        else:
            cursor = db.execute(
                "INSERT INTO conversations (title, channel_type, last_message_at) "
                "VALUES (?, 'email', CURRENT_TIMESTAMP)",
                (REFLECTION_HOME_TITLE,),
            )
            conv_id = int(cursor.lastrowid)

        if to_addr:
            db.execute(
                "INSERT INTO channel_bindings "
                "(channel_type, channel_user_id, display_name, conversation_id) "
                "VALUES ('email', ?, ?, ?) "
                "ON CONFLICT(channel_type, channel_user_id) "
                "DO UPDATE SET conversation_id = ?",
                (to_addr, to_addr, conv_id, conv_id),
            )
    return conv_id


def format_reflection_email_body(
    message: str, arc_id: int | None = None
) -> str:
    """Prep the outgoing body: hoist review URLs to the top, keep body.

    Regex-scans ``message`` for review URLs *and*, when ``arc_id`` is
    supplied, walks that arc's subtree for any pending-human-review URLs
    the message body itself may have omitted.  The chat-agent response
    that ``arc.chat_notify`` re-invokes often summarises with prose like
    "Review pending at the link above" without repeating the URL — that
    email would otherwise ship linkless, defeating the whole point of
    the escalation channel.
    """
    urls: list[str] = list(_REVIEW_URL_RE.findall(message))
    if arc_id is not None:
        try:
            from .workflows.arc_notify_handler import _collect_pending_reviews
            urls.extend(_collect_pending_reviews(arc_id))
        except Exception:  # noqa: BLE001 — URL augmentation must not break send
            logger.exception(
                "reflection_escalation: subtree review-URL lookup failed "
                "for arc %d; sending body without augmentation",
                arc_id,
            )
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


async def send_reflection_escalation(
    conversation_id: int, message: str, arc_id: int | None = None
) -> bool:
    """Fire one reflection escalation email.

    Thin wrapper: pick up the email connector, build the reflection-
    shaped body/subject, delegate to
    :meth:`EmailChannelConnector.send_message`.  Returns ``False`` if
    the connector is unavailable or the send failed — caller decides
    whether to fall back to another path.
    """
    connector = _get_email_connector()
    if connector is None:
        logger.warning(
            "reflection_escalation: no email connector available for "
            "conversation %d; escalation dropped",
            conversation_id,
        )
        return False
    body = format_reflection_email_body(message, arc_id=arc_id)
    snippet = " ".join(message.split())[:60].strip() or "(no summary)"
    if arc_id is not None:
        subject = f"[Reflection] arc {arc_id}: {snippet}"
    else:
        subject = f"[Reflection] {snippet}"
    return await connector.send_message(
        conversation_id, body, metadata={"subject": subject, "arc_id": arc_id}
    )
