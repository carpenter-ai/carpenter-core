"""Tests for the reflection escalation gate + email-medium plumbing.

The email delivery layer itself now lives in
:class:`carpenter.channels.email_channel.EmailChannelConnector` (tested
separately in ``tests/channels/test_email_channel.py``).  This module
tests only the reflection-specific glue:

* :func:`ensure_escalation_ready` — config-presence + connector-presence
  gate that ``handle_reflection_tick`` uses to refuse to start when
  escalation would be undeliverable.
* :func:`get_or_create_reflection_home_conversation` — idempotent
  email-medium conversation with a seeded ``channel_bindings`` row.
* :func:`format_reflection_email_body` — URL-hoisting body preparation.
* :func:`send_reflection_escalation` — thin coroutine that hands the
  formatted body to the email connector's ``send_message``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

# Tests that need to drive coroutines use `async def` so pytest-asyncio
# (mode=auto) manages the loop.  Calling ``asyncio.run()`` here would
# close the process default loop and break sibling tests that use the
# deprecated ``asyncio.get_event_loop()`` pattern.

from carpenter import config
from carpenter.agent import conversation
from carpenter.core import reflection_escalation
from carpenter.db import get_db


# ── ensure_escalation_ready ───────────────────────────────────────────


def _install_fake_email_connector(monkeypatch, connector=None):
    """Stub _get_email_connector to return ``connector`` (or a MagicMock)."""
    if connector is None:
        connector = MagicMock()
        connector.channel_type = "email"
        connector.enabled = True
    monkeypatch.setattr(
        "carpenter.core.reflection_escalation._get_email_connector",
        lambda: connector,
    )
    return connector


def test_ensure_escalation_ready_false_when_config_missing(monkeypatch):
    """No ``reflection.escalation.email.to`` config → gate refuses."""
    _install_fake_email_connector(monkeypatch)
    monkeypatch.setitem(config.CONFIG, "reflection", {})
    assert reflection_escalation.ensure_escalation_ready() is False


def test_ensure_escalation_ready_false_when_no_connector(monkeypatch):
    """No enabled email connector → gate refuses even with a `to` address."""
    monkeypatch.setattr(
        "carpenter.core.reflection_escalation._get_email_connector",
        lambda: None,
    )
    monkeypatch.setitem(
        config.CONFIG,
        "reflection",
        {"escalation": {"email": {"to": "you@example.com"}}},
    )
    assert reflection_escalation.ensure_escalation_ready() is False


def test_ensure_escalation_ready_false_when_to_missing_at(monkeypatch):
    """A ``to`` value missing an ``@`` is not a valid address."""
    _install_fake_email_connector(monkeypatch)
    monkeypatch.setitem(
        config.CONFIG,
        "reflection",
        {"escalation": {"email": {"to": "not-an-email"}}},
    )
    assert reflection_escalation.ensure_escalation_ready() is False


def test_ensure_escalation_ready_true_when_all_present(monkeypatch):
    """Config address + a registered connector → gate opens."""
    _install_fake_email_connector(monkeypatch)
    monkeypatch.setitem(
        config.CONFIG,
        "reflection",
        {"escalation": {"email": {"to": "you@example.com"}}},
    )
    assert reflection_escalation.ensure_escalation_ready() is True


# ── get_or_create_reflection_home_conversation ─────────────────────────


def test_reflection_home_conversation_idempotent(monkeypatch):
    """Creating on first call, reusing on second call."""
    monkeypatch.setitem(
        config.CONFIG,
        "reflection",
        {"escalation": {"email": {"to": "you@example.com"}}},
    )
    first = reflection_escalation.get_or_create_reflection_home_conversation()
    second = reflection_escalation.get_or_create_reflection_home_conversation()
    assert first == second

    conv = conversation.get_conversation(first)
    assert conv is not None
    assert conv["title"] == reflection_escalation.REFLECTION_HOME_TITLE
    assert conv["channel_type"] == "email"


def test_reflection_home_conversation_seeds_channel_binding(monkeypatch):
    """A ``channel_bindings`` row must land so the email connector can
    resolve a recipient for the reflection-home conversation."""
    to_addr = "operator@example.com"
    monkeypatch.setitem(
        config.CONFIG,
        "reflection",
        {"escalation": {"email": {"to": to_addr}}},
    )
    home_id = reflection_escalation.get_or_create_reflection_home_conversation()

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT channel_user_id, conversation_id "
            "FROM channel_bindings "
            "WHERE channel_type = 'email' AND conversation_id = ?",
            (home_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["channel_user_id"] == to_addr
    assert row["conversation_id"] == home_id


def test_reflection_home_conversation_unarchives_stale(monkeypatch):
    """A previously-archived reflection-home is un-archived on next lookup."""
    monkeypatch.setitem(
        config.CONFIG,
        "reflection",
        {"escalation": {"email": {"to": "you@example.com"}}},
    )
    home_id = reflection_escalation.get_or_create_reflection_home_conversation()
    conversation.archive_conversation(home_id)
    assert conversation.get_conversation(home_id)["archived"] == 1

    same_id = reflection_escalation.get_or_create_reflection_home_conversation()
    assert same_id == home_id
    conv = conversation.get_conversation(home_id)
    assert conv["archived"] == 0


def test_reflection_home_conversation_ignores_non_email_same_title(monkeypatch):
    """A same-title conversation with a NULL channel_type is not reused."""
    monkeypatch.setitem(
        config.CONFIG,
        "reflection",
        {"escalation": {"email": {"to": "you@example.com"}}},
    )
    conv_id = conversation.create_conversation()
    conversation.set_conversation_title(
        conv_id, reflection_escalation.REFLECTION_HOME_TITLE,
    )

    home_id = reflection_escalation.get_or_create_reflection_home_conversation()
    assert home_id != conv_id
    home = conversation.get_conversation(home_id)
    assert home["channel_type"] == "email"


# ── format_reflection_email_body ────────────────────────────────────────


def test_format_reflection_email_body_hoists_body_urls():
    """A review URL in the body is surfaced in the header."""
    url = "https://example.com/api/review/deadbeef-1234-5678-9abc-def012345678"
    body = f"Please review at {url}"
    out = reflection_escalation.format_reflection_email_body(body)
    assert "Pending review URLs:" in out
    assert url in out
    assert out.index("Pending review URLs") < out.index("Please review at")


def test_format_reflection_email_body_hoists_subtree_urls(monkeypatch):
    """When body omits the URL, subtree lookup fills it in.

    Regression cover: the chat-agent summary often reads
    "Review pending at the link above" with no URL — the connector must
    still ship an actionable link.
    """
    subtree_url = (
        "https://example.com/api/review/deadbeef-1234-5678-9abc-def012345678"
    )
    monkeypatch.setattr(
        "carpenter.core.workflows.arc_notify_handler._collect_pending_reviews",
        lambda arc_id: [subtree_url],
    )
    body = "Review pending at the link above."
    out = reflection_escalation.format_reflection_email_body(body, arc_id=7)
    assert subtree_url in out
    assert out.index("Pending review URLs") < out.index(
        "Review pending at the link above."
    )


def test_format_reflection_email_body_no_urls_returns_message():
    """When there are no URLs anywhere, no header is added."""
    body = "Reflection completed with nothing to do."
    out = reflection_escalation.format_reflection_email_body(body)
    assert out == body


def test_format_reflection_email_body_deduplicates_urls(monkeypatch):
    """A URL in both body and subtree is not listed twice."""
    url = "https://example.com/api/review/deadbeef-1234-5678-9abc-def012345678"
    monkeypatch.setattr(
        "carpenter.core.workflows.arc_notify_handler._collect_pending_reviews",
        lambda arc_id: [url],
    )
    body = f"See {url} to approve."
    out = reflection_escalation.format_reflection_email_body(body, arc_id=7)
    # Only one occurrence in the header block
    header = out.split("\n\n", 1)[0]
    assert header.count(url) == 1


# ── send_reflection_escalation ─────────────────────────────────────────


async def test_send_reflection_escalation_returns_false_when_no_connector(
    monkeypatch,
):
    """Missing email connector → the coroutine returns ``False`` cleanly."""
    monkeypatch.setattr(
        "carpenter.core.reflection_escalation._get_email_connector",
        lambda: None,
    )
    ok = await reflection_escalation.send_reflection_escalation(
        conversation_id=42, message="anything"
    )
    assert ok is False


async def test_send_reflection_escalation_calls_connector(monkeypatch):
    """Body is formatted, subject is derived, connector.send_message is called."""
    url = "https://example.com/api/review/deadbeef-1234-5678-9abc-def012345678"
    connector = MagicMock()
    connector.send_message = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "carpenter.core.reflection_escalation._get_email_connector",
        lambda: connector,
    )

    body = f"Arc 7 completed. See {url} to approve."
    ok = await reflection_escalation.send_reflection_escalation(
        conversation_id=42, message=body, arc_id=7
    )
    assert ok is True

    connector.send_message.assert_awaited_once()
    args, kwargs = connector.send_message.call_args
    # positional (conversation_id, formatted_body)
    assert args[0] == 42
    formatted = args[1]
    assert url in formatted
    assert formatted.index("Pending review URLs") < formatted.index(
        "Arc 7 completed."
    )
    # metadata carries subject + arc_id
    md = kwargs["metadata"]
    assert "arc 7" in md["subject"]
    assert "[Reflection]" in md["subject"]
    assert md["arc_id"] == 7


async def test_send_reflection_escalation_forwards_failure(monkeypatch):
    """A ``False`` return from the connector propagates back to the caller."""
    connector = MagicMock()
    connector.send_message = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "carpenter.core.reflection_escalation._get_email_connector",
        lambda: connector,
    )
    ok = await reflection_escalation.send_reflection_escalation(
        conversation_id=1, message="hi"
    )
    assert ok is False


# ── daily_tick gate ────────────────────────────────────────────────────


async def test_handle_reflection_tick_gates_when_escalation_missing(monkeypatch):
    """When the gate blocks, no reflection arc must be created."""
    from config_seed.templates.reflection import daily_tick as _daily_tick

    monkeypatch.setattr(
        "carpenter.core.reflection_escalation.ensure_escalation_ready",
        lambda: False,
    )

    arc_creation_calls: list[dict] = []

    def _record(*args, **kwargs):
        arc_creation_calls.append({"args": args, "kwargs": kwargs})
        return 999

    monkeypatch.setattr(
        "carpenter.core.arcs.manager.create_arc",
        _record,
    )

    await _daily_tick.handle_reflection_tick(work_id=0, payload={})

    assert arc_creation_calls == []
