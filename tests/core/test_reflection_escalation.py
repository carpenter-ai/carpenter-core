"""Tests for the reflection escalation gate + email-medium dispatch.

Covers the two public entry points on
:mod:`carpenter.core.reflection_escalation` plus the
:func:`conversation.add_message` generalisation and the
:func:`handle_reflection_tick` gate.
"""

from __future__ import annotations

import asyncio

import pytest

from carpenter import config
from carpenter.agent import conversation
from carpenter.core import reflection_escalation


# ── ensure_escalation_ready ───────────────────────────────────────────


def _install_full_smtp(monkeypatch):
    """Stub resolve_package_secret so all required keys return values."""
    def fake_resolve(pkg, key):
        if pkg != "carpenter-imap-email":
            return None
        return {
            "EMAIL_SMTP_HOST": "smtp.example.com",
            "EMAIL_SMTP_USERNAME": "bot@example.com",
            "EMAIL_SMTP_PASSWORD": "hunter2",
            "EMAIL_SMTP_PORT": "587",
            "EMAIL_SMTP_FROM": "bot@example.com",
        }.get(key)

    monkeypatch.setattr(
        "carpenter.core.reflection_escalation.resolve_package_secret",
        fake_resolve,
    )


def test_ensure_escalation_ready_false_when_config_missing(monkeypatch):
    """No ``reflection.escalation.email.to`` config → gate refuses."""
    _install_full_smtp(monkeypatch)
    # Ensure there is no reflection.escalation.email.to entry.
    monkeypatch.setitem(config.CONFIG, "reflection", {})
    assert reflection_escalation.ensure_escalation_ready() is False


def test_ensure_escalation_ready_false_when_smtp_creds_missing(monkeypatch):
    """SMTP creds unresolved → gate refuses even with a `to` address."""
    monkeypatch.setattr(
        "carpenter.core.reflection_escalation.resolve_package_secret",
        lambda pkg, key: None,
    )
    monkeypatch.setitem(
        config.CONFIG,
        "reflection",
        {"escalation": {"email": {"to": "you@example.com"}}},
    )
    assert reflection_escalation.ensure_escalation_ready() is False


def test_ensure_escalation_ready_false_when_to_missing_at(monkeypatch):
    """A ``to`` value missing an ``@`` is not a valid address."""
    _install_full_smtp(monkeypatch)
    monkeypatch.setitem(
        config.CONFIG,
        "reflection",
        {"escalation": {"email": {"to": "not-an-email"}}},
    )
    assert reflection_escalation.ensure_escalation_ready() is False


def test_ensure_escalation_ready_true_when_all_present(monkeypatch):
    """Both config and creds present → gate opens."""
    _install_full_smtp(monkeypatch)
    monkeypatch.setitem(
        config.CONFIG,
        "reflection",
        {"escalation": {"email": {"to": "you@example.com"}}},
    )
    assert reflection_escalation.ensure_escalation_ready() is True


# ── get_or_create_reflection_home_conversation ─────────────────────────


def test_reflection_home_conversation_idempotent():
    """Creating on first call, reusing on second call."""
    first = reflection_escalation.get_or_create_reflection_home_conversation()
    second = reflection_escalation.get_or_create_reflection_home_conversation()
    assert first == second

    conv = conversation.get_conversation(first)
    assert conv is not None
    assert conv["title"] == reflection_escalation.REFLECTION_HOME_TITLE
    assert conv["channel_type"] == "email"


def test_reflection_home_conversation_unarchives_stale():
    """A previously-archived reflection-home is un-archived on next lookup.

    Without this, arc_notify_handler would discard the archived conv and
    fall back to get_last_conversation() (a chat conv the user doesn't
    monitor), silently rerouting reflection escalation email.
    """
    home_id = reflection_escalation.get_or_create_reflection_home_conversation()
    conversation.archive_conversation(home_id)
    assert conversation.get_conversation(home_id)["archived"] == 1

    same_id = reflection_escalation.get_or_create_reflection_home_conversation()
    assert same_id == home_id
    conv = conversation.get_conversation(home_id)
    assert conv["archived"] == 0


def test_reflection_home_conversation_ignores_non_email_same_title():
    """A same-title conversation with a NULL channel_type is not reused.

    We look up by (title, channel_type='email'), so a stray conversation
    that happens to have the same title but no email channel_type is
    ignored and a fresh email-medium row is created.
    """
    # Create a non-email conversation with the same title.
    conv_id = conversation.create_conversation()
    conversation.set_conversation_title(
        conv_id, reflection_escalation.REFLECTION_HOME_TITLE,
    )

    home_id = reflection_escalation.get_or_create_reflection_home_conversation()
    assert home_id != conv_id
    home = conversation.get_conversation(home_id)
    assert home["channel_type"] == "email"


# ── add_message routing ────────────────────────────────────────────────


def test_add_message_chat_medium_does_not_invoke_smtp(monkeypatch):
    """Regular chat conversations must NOT trigger email dispatch."""
    calls: list[tuple] = []

    def fake_dispatch(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(
        "carpenter.core.reflection_escalation.dispatch_email_message",
        fake_dispatch,
    )

    conv_id = conversation.create_conversation()  # channel_type = NULL
    conversation.add_message(conv_id, "assistant", "hi there")

    assert calls == []


def test_add_message_email_medium_dispatches(monkeypatch):
    """Email-medium conversations MUST route add_message through SMTP."""
    calls: list[dict] = []

    def fake_dispatch(conversation_id, role, message, arc_id=None):
        calls.append({
            "conversation_id": conversation_id,
            "role": role,
            "message": message,
            "arc_id": arc_id,
        })
        return True

    monkeypatch.setattr(
        "carpenter.core.reflection_escalation.dispatch_email_message",
        fake_dispatch,
    )

    home_id = reflection_escalation.get_or_create_reflection_home_conversation()
    # No arc_id: FK requires a real arc row, and the routing logic doesn't
    # care about arc_id — it only checks the conversation's channel_type.
    conversation.add_message(home_id, "system", "arc 42 completed")

    assert len(calls) == 1
    assert calls[0]["conversation_id"] == home_id
    assert calls[0]["role"] == "system"
    assert calls[0]["arc_id"] is None
    assert "arc 42 completed" in calls[0]["message"]


def test_dispatch_email_message_skips_user_role(monkeypatch):
    """The ``user`` role must be skipped entirely (no SMTP send)."""
    sent: list[tuple] = []

    def fake_send(email_config, message, subject):
        sent.append((email_config, message, subject))
        return True

    monkeypatch.setattr(
        "carpenter.core.notifications._send_email_smtp",
        fake_send,
    )
    _install_full_smtp(monkeypatch)
    monkeypatch.setitem(
        config.CONFIG,
        "reflection",
        {"escalation": {"email": {"to": "you@example.com"}}},
    )

    result = reflection_escalation.dispatch_email_message(
        conversation_id=1, role="user", message="hi",
    )
    assert result is False
    assert sent == []


def test_dispatch_email_message_calls_send_email_smtp(monkeypatch):
    """A non-user message with valid config actually invokes SMTP send."""
    sent: list[tuple] = []

    def fake_send(email_config, message, subject):
        sent.append((email_config, message, subject))
        return True

    monkeypatch.setattr(
        "carpenter.core.notifications._send_email_smtp",
        fake_send,
    )
    _install_full_smtp(monkeypatch)
    monkeypatch.setitem(
        config.CONFIG,
        "reflection",
        {"escalation": {"email": {"to": "you@example.com"}}},
    )

    review_url = "https://example.com/api/review/deadbeef-1234-5678-9abc-def012345678"
    body = f"Arc 7 completed. See {review_url} to approve."
    ok = reflection_escalation.dispatch_email_message(
        conversation_id=42, role="assistant", message=body, arc_id=7,
    )
    assert ok is True
    assert len(sent) == 1
    email_config, message, subject = sent[0]
    # Subject includes the arc id and a body snippet.
    assert "arc 7" in subject
    assert "[Reflection]" in subject
    # Body highlights the review URL at the top AND preserves the original.
    assert review_url in message
    assert message.index("Pending review URLs") < message.index("Arc 7 completed.")
    # Email config was assembled from the fake resolver.
    assert email_config["smtp_host"] == "smtp.example.com"
    assert email_config["smtp_to"] == "you@example.com"


def test_dispatch_email_message_appends_subtree_review_urls(monkeypatch):
    """URL missing from message body but present in the arc subtree must
    still land in the email header.

    Regression cover: the chat-agent response that ``arc.chat_notify``
    re-invokes often summarises with "Review pending at the link above"
    without repeating the URL, so a body-only regex would ship a
    linkless escalation email — defeating the whole point of the
    channel.
    """
    sent: list[tuple] = []

    def fake_send(email_config, message, subject):
        sent.append((email_config, message, subject))
        return True

    subtree_url = (
        "https://example.com/api/review/deadbeef-1234-5678-9abc-def012345678"
    )
    monkeypatch.setattr(
        "carpenter.core.notifications._send_email_smtp",
        fake_send,
    )
    monkeypatch.setattr(
        "carpenter.core.workflows.arc_notify_handler._collect_pending_reviews",
        lambda arc_id: [subtree_url],
    )
    _install_full_smtp(monkeypatch)
    monkeypatch.setitem(
        config.CONFIG,
        "reflection",
        {"escalation": {"email": {"to": "you@example.com"}}},
    )

    body = "Review pending at the link above."  # no URL in prose
    ok = reflection_escalation.dispatch_email_message(
        conversation_id=42, role="assistant", message=body, arc_id=7,
    )
    assert ok is True
    _, message, _ = sent[0]
    assert subtree_url in message
    assert message.index("Pending review URLs") < message.index(
        "Review pending at the link above."
    )


def test_dispatch_email_message_swallows_smtp_errors(monkeypatch):
    """An exception in _send_email_smtp is logged, not raised."""
    def boom(email_config, message, subject):
        raise RuntimeError("smtp exploded")

    monkeypatch.setattr(
        "carpenter.core.notifications._send_email_smtp",
        boom,
    )
    _install_full_smtp(monkeypatch)
    monkeypatch.setitem(
        config.CONFIG,
        "reflection",
        {"escalation": {"email": {"to": "you@example.com"}}},
    )

    # No exception must escape.
    result = reflection_escalation.dispatch_email_message(
        conversation_id=1, role="system", message="oops",
    )
    assert result is False


# ── daily_tick gate ────────────────────────────────────────────────────


def test_handle_reflection_tick_gates_when_escalation_missing(monkeypatch):
    """When the gate blocks, no reflection arc must be created."""
    from config_seed.templates.reflection import daily_tick as _daily_tick

    monkeypatch.setattr(
        "carpenter.core.reflection_escalation.ensure_escalation_ready",
        lambda: False,
    )

    # Sentinel to detect any arc creation attempt.
    arc_creation_calls: list[dict] = []

    def _record(*args, **kwargs):
        arc_creation_calls.append({"args": args, "kwargs": kwargs})
        return 999

    monkeypatch.setattr(
        "carpenter.core.arcs.manager.create_arc",
        _record,
    )

    asyncio.run(_daily_tick.handle_reflection_tick(work_id=0, payload={}))

    assert arc_creation_calls == []
