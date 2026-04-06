"""Tests for the notification system (carpenter.core.notifications)."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from carpenter.core import notifications
from carpenter.db import get_db
from carpenter.agent import conversation


# ── Helpers ──────────────────────────────────────────────────────────

def _get_all_notifications():
    """Read all rows from the notifications table."""
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM notifications ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def _setup_conversation():
    """Create a conversation so the chat channel has somewhere to write."""
    return conversation.create_conversation()


@pytest.fixture(autouse=True)
def _reset_batch_state():
    """Ensure batch buffer is clean between tests."""
    with notifications._batch_lock:
        notifications._batch_buffer.clear()
        if notifications._batch_timer is not None:
            notifications._batch_timer.cancel()
        notifications._batch_timer = None
        notifications._batch_id = None
    yield
    # Cleanup after test
    with notifications._batch_lock:
        notifications._batch_buffer.clear()
        if notifications._batch_timer is not None:
            notifications._batch_timer.cancel()
        notifications._batch_timer = None
        notifications._batch_id = None


_EMAIL_OFF = {
    "enabled": False, "mode": "smtp", "smtp_host": "", "smtp_port": 587,
    "smtp_to": "", "smtp_from": "", "smtp_username": "", "smtp_password": "",
    "smtp_tls": True, "command": "",
}

_EMAIL_ON = {
    "enabled": True, "mode": "smtp", "smtp_host": "mail.test", "smtp_port": 587,
    "smtp_to": "user@test.com", "smtp_from": "bot@test.com",
    "smtp_username": "", "smtp_password": "", "smtp_tls": False, "command": "",
}


def _notif_config(email=None, batch_window=0, routing=None):
    """Build a notifications config dict."""
    from carpenter import config
    cfg = dict(config.CONFIG)
    cfg["notifications"] = {
        "email": email or _EMAIL_OFF,
        "batch_window": batch_window,
        "routing": routing or {},
    }
    return cfg


# ── Routing tests ───────────────────────────────────────────────────

def test_urgent_routes_to_chat_and_email(monkeypatch):
    """Urgent priority delivers to both chat and email channels."""
    _setup_conversation()
    monkeypatch.setattr("carpenter.config.CONFIG", _notif_config(email=_EMAIL_ON))

    mock_smtp = MagicMock()
    with patch("carpenter.core.notifications.smtplib.SMTP", return_value=mock_smtp):
        notifications.notify("Test urgent message", priority="urgent")

    # Check chat channel: system message should be in conversation
    db = get_db()
    try:
        msgs = db.execute(
            "SELECT * FROM messages WHERE role = 'system'"
        ).fetchall()
        assert len(msgs) >= 1
        assert "[URGENT]" in msgs[-1]["content"]
        assert "Test urgent message" in msgs[-1]["content"]
    finally:
        db.close()

    # Check email was attempted
    mock_smtp.sendmail.assert_called_once()

    # Check log channel
    notifs = _get_all_notifications()
    assert len(notifs) == 1
    assert notifs[0]["priority"] == "urgent"
    assert "chat" in notifs[0]["channel"]
    assert "email" in notifs[0]["channel"]


def test_normal_routes_to_chat(monkeypatch):
    """Normal priority with no email configured routes to chat only."""
    _setup_conversation()
    monkeypatch.setattr("carpenter.config.CONFIG", _notif_config())

    notifications.notify("Normal message", priority="normal")

    # Should have a system message in conversation
    db = get_db()
    try:
        msgs = db.execute(
            "SELECT * FROM messages WHERE role = 'system'"
        ).fetchall()
        assert len(msgs) >= 1
        assert "Normal message" in msgs[-1]["content"]
    finally:
        db.close()

    # Log should record chat channel only
    notifs = _get_all_notifications()
    assert len(notifs) == 1
    assert notifs[0]["channel"] == "chat"


def test_low_routes_to_email_only(monkeypatch):
    """Low priority routes to email only (no chat)."""
    conv_id = _setup_conversation()
    monkeypatch.setattr("carpenter.config.CONFIG", _notif_config(email=_EMAIL_ON))

    mock_smtp = MagicMock()
    with patch("carpenter.core.notifications.smtplib.SMTP", return_value=mock_smtp):
        notifications.notify("Low priority msg", priority="low")

    # No system message should be added to chat
    db = get_db()
    try:
        msgs = db.execute(
            "SELECT * FROM messages WHERE role = 'system' AND conversation_id = ?",
            (conv_id,),
        ).fetchall()
        assert len(msgs) == 0
    finally:
        db.close()

    # Email should have been sent
    mock_smtp.sendmail.assert_called_once()

    # Logged with email channel
    notifs = _get_all_notifications()
    assert len(notifs) == 1
    assert notifs[0]["channel"] == "email"


def test_fyi_routes_to_log_only(monkeypatch):
    """FYI priority routes to log only (no chat, no email)."""
    conv_id = _setup_conversation()
    monkeypatch.setattr("carpenter.config.CONFIG", _notif_config(email=_EMAIL_ON))

    notifications.notify("FYI message", priority="fyi")

    # No system messages
    db = get_db()
    try:
        msgs = db.execute(
            "SELECT * FROM messages WHERE role = 'system' AND conversation_id = ?",
            (conv_id,),
        ).fetchall()
        assert len(msgs) == 0
    finally:
        db.close()

    # Logged as log-only
    notifs = _get_all_notifications()
    assert len(notifs) == 1
    assert notifs[0]["channel"] == "log"
    assert notifs[0]["priority"] == "fyi"


# ── Chat channel tests ──────────────────────────────────────────────

def test_chat_channel_inserts_system_message(monkeypatch):
    """ChatChannel inserts a system message into the active conversation."""
    conv_id = _setup_conversation()
    conversation.add_message(conv_id, "user", "Hello")
    monkeypatch.setattr("carpenter.config.CONFIG", _notif_config())

    notifications._send_chat("Test notification", "normal", None)

    msgs = conversation.get_messages(conv_id)
    system_msgs = [m for m in msgs if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert system_msgs[0]["content"] == "Test notification"


def test_chat_channel_includes_urgent_prefix(monkeypatch):
    """Urgent notifications get [URGENT] prefix in chat."""
    conv_id = _setup_conversation()
    monkeypatch.setattr("carpenter.config.CONFIG", _notif_config())

    notifications._send_chat("Security alert", "urgent", "security_events")

    msgs = conversation.get_messages(conv_id)
    system_msgs = [m for m in msgs if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert "[URGENT]" in system_msgs[0]["content"]
    assert "[security_events]" in system_msgs[0]["content"]
    assert "Security alert" in system_msgs[0]["content"]


# ── Email SMTP channel tests ────────────────────────────────────────

def test_email_smtp_sends_message():
    """EmailChannel smtp mode sends via smtplib."""
    email_config = {
        "enabled": True,
        "mode": "smtp",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_from": "bot@example.com",
        "smtp_to": "user@example.com",
        "smtp_username": "bot",
        "smtp_password": "secret",
        "smtp_tls": True,
        "command": "",
    }

    mock_smtp = MagicMock()
    with patch("carpenter.core.notifications.smtplib.SMTP", return_value=mock_smtp):
        result = notifications._send_email_smtp(email_config, "Test body", "Test Subject")

    assert result is True
    mock_smtp.starttls.assert_called_once()
    mock_smtp.login.assert_called_once_with("bot", "secret")
    mock_smtp.sendmail.assert_called_once()
    mock_smtp.quit.assert_called_once()

    # Verify the message envelope and subject
    call_args = mock_smtp.sendmail.call_args
    assert call_args[0][0] == "bot@example.com"
    assert call_args[0][1] == ["user@example.com"]
    raw_msg = call_args[0][2]
    assert "Test Subject" in raw_msg
    assert "bot@example.com" in raw_msg
    assert "user@example.com" in raw_msg


def test_email_smtp_no_tls():
    """EmailChannel smtp mode works without TLS."""
    email_config = {
        "enabled": True,
        "mode": "smtp",
        "smtp_host": "smtp.example.com",
        "smtp_port": 25,
        "smtp_from": "bot@example.com",
        "smtp_to": "user@example.com",
        "smtp_username": "",
        "smtp_password": "",
        "smtp_tls": False,
        "command": "",
    }

    mock_smtp = MagicMock()
    with patch("carpenter.core.notifications.smtplib.SMTP", return_value=mock_smtp):
        result = notifications._send_email_smtp(email_config, "Test body", "Subject")

    assert result is True
    mock_smtp.starttls.assert_not_called()
    mock_smtp.login.assert_not_called()


def test_email_smtp_failure_returns_false():
    """EmailChannel smtp mode returns False on failure without raising."""
    email_config = {
        "enabled": True,
        "mode": "smtp",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_from": "bot@example.com",
        "smtp_to": "user@example.com",
        "smtp_username": "",
        "smtp_password": "",
        "smtp_tls": True,
        "command": "",
    }

    with patch("carpenter.core.notifications.smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        result = notifications._send_email_smtp(email_config, "Test", "Subject")

    assert result is False


def test_email_smtp_missing_host_returns_false():
    """SMTP with empty host returns False without attempting connection."""
    email_config = {
        "enabled": True,
        "mode": "smtp",
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_from": "",
        "smtp_to": "",
        "smtp_username": "",
        "smtp_password": "",
        "smtp_tls": True,
        "command": "",
    }

    result = notifications._send_email_smtp(email_config, "Test", "Subject")
    assert result is False


# ── Email command channel tests ──────────────────────────────────────

def test_email_command_sends_message():
    """EmailChannel command mode pipes message to shell command."""
    email_config = {
        "enabled": True,
        "mode": "command",
        "command": "cat > /dev/null",
        "smtp_host": "", "smtp_port": 587, "smtp_from": "", "smtp_to": "",
        "smtp_username": "", "smtp_password": "", "smtp_tls": True,
    }

    mock_result = MagicMock()
    mock_result.returncode = 0
    with patch("carpenter.core.notifications.subprocess.run", return_value=mock_result) as mock_run:
        result = notifications._send_email_command(email_config, "Hello world", "Test Subject")

    assert result is True
    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args
    assert "Subject: Test Subject" in call_kwargs.kwargs.get("input", call_kwargs[1].get("input", ""))
    assert "Hello world" in call_kwargs.kwargs.get("input", call_kwargs[1].get("input", ""))


def test_email_command_failure_returns_false():
    """Command failure returns False without raising."""
    email_config = {
        "enabled": True,
        "mode": "command",
        "command": "false",
        "smtp_host": "", "smtp_port": 587, "smtp_from": "", "smtp_to": "",
        "smtp_username": "", "smtp_password": "", "smtp_tls": True,
    }

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "command failed"
    with patch("carpenter.core.notifications.subprocess.run", return_value=mock_result):
        result = notifications._send_email_command(email_config, "Test", "Subject")

    assert result is False


def test_email_command_empty_returns_false():
    """Empty command string returns False."""
    email_config = {
        "enabled": True,
        "mode": "command",
        "command": "",
        "smtp_host": "", "smtp_port": 587, "smtp_from": "", "smtp_to": "",
        "smtp_username": "", "smtp_password": "", "smtp_tls": True,
    }

    result = notifications._send_email_command(email_config, "Test", "Subject")
    assert result is False


# ── Log channel tests ────────────────────────────────────────────────

def test_log_channel_records_in_notifications_table(monkeypatch):
    """LogChannel records a row in the notifications table."""
    _setup_conversation()
    monkeypatch.setattr("carpenter.config.CONFIG", _notif_config())

    notifications.notify("Test log entry", priority="fyi", category="test_category")

    notifs = _get_all_notifications()
    assert len(notifs) == 1
    assert notifs[0]["message"] == "Test log entry"
    assert notifs[0]["priority"] == "fyi"
    assert notifs[0]["category"] == "test_category"
    assert notifs[0]["status"] == "sent"
    assert notifs[0]["sent_at"] is not None


# ── Batching tests ───────────────────────────────────────────────────

def test_batching_buffers_and_flushes(monkeypatch):
    """Batching buffers notifications and flushes them together."""
    _setup_conversation()
    monkeypatch.setattr("carpenter.config.CONFIG", _notif_config(batch_window=300))

    # Send multiple notifications (non-urgent, so they batch)
    notifications.notify("Message 1", priority="normal")
    notifications.notify("Message 2", priority="normal", category="review_needed")

    # Nothing delivered yet
    notifs = _get_all_notifications()
    assert len(notifs) == 0

    # Force flush
    notifications.flush_now()

    # Now they should be delivered as a single combined notification
    notifs = _get_all_notifications()
    assert len(notifs) == 1
    assert "Message 1" in notifs[0]["message"]
    assert "Message 2" in notifs[0]["message"]
    assert notifs[0]["batch_id"] is not None


def test_batching_single_message_not_prefixed(monkeypatch):
    """A single batched message is delivered without batch numbering."""
    _setup_conversation()
    monkeypatch.setattr("carpenter.config.CONFIG", _notif_config(batch_window=300))

    notifications.notify("Solo message", priority="normal")
    notifications.flush_now()

    notifs = _get_all_notifications()
    assert len(notifs) == 1
    assert notifs[0]["message"] == "Solo message"
    assert "Batched" not in notifs[0]["message"]


def test_urgent_bypasses_batching(monkeypatch):
    """Urgent notifications are never batched — they deliver immediately."""
    _setup_conversation()
    monkeypatch.setattr("carpenter.config.CONFIG", _notif_config(batch_window=300))

    notifications.notify("Urgent now!", priority="urgent")

    # Should be delivered immediately, not batched
    notifs = _get_all_notifications()
    assert len(notifs) == 1
    assert notifs[0]["message"] == "Urgent now!"
    assert notifs[0]["priority"] == "urgent"
    # Chat message should have the prefix
    db = get_db()
    try:
        msgs = db.execute("SELECT * FROM messages WHERE role = 'system'").fetchall()
        assert len(msgs) >= 1
        assert "[URGENT]" in msgs[-1]["content"]
    finally:
        db.close()


# ── Custom routing tests ─────────────────────────────────────────────

def test_custom_routing_overrides_priority(monkeypatch):
    """Category routing config overrides the notification priority."""
    _setup_conversation()
    monkeypatch.setattr("carpenter.config.CONFIG",
                        _notif_config(routing={"custom_category": "fyi"}))

    # Send as "normal" but category routes to "fyi" (log only)
    notifications.notify("Routed to fyi", priority="normal", category="custom_category")

    # No chat messages should exist (fyi = log only)
    db = get_db()
    try:
        msgs = db.execute("SELECT * FROM messages WHERE role = 'system'").fetchall()
        assert len(msgs) == 0
    finally:
        db.close()

    # But notification should be logged
    notifs = _get_all_notifications()
    assert len(notifs) == 1
    assert notifs[0]["channel"] == "log"


# ── Email failure resilience ─────────────────────────────────────────

def test_email_failure_does_not_crash(monkeypatch):
    """Email failures are logged but don't crash the notify() call."""
    _setup_conversation()
    bad_email = {**_EMAIL_ON, "smtp_host": "bad.host", "smtp_tls": True}
    monkeypatch.setattr("carpenter.config.CONFIG", _notif_config(email=bad_email))

    with patch("carpenter.core.notifications.smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        # Should not raise
        notifications.notify("Test with broken email", priority="urgent")

    # Chat should still work
    db = get_db()
    try:
        msgs = db.execute("SELECT * FROM messages WHERE role = 'system'").fetchall()
        assert len(msgs) >= 1
        assert "Test with broken email" in msgs[-1]["content"]
    finally:
        db.close()

    # Notification logged (chat delivered, email failed)
    notifs = _get_all_notifications()
    assert len(notifs) == 1
    assert "chat" in notifs[0]["channel"]


# ── Invalid priority ─────────────────────────────────────────────────

def test_invalid_priority_defaults_to_normal(monkeypatch):
    """Invalid priority levels default to 'normal'."""
    _setup_conversation()
    monkeypatch.setattr("carpenter.config.CONFIG", _notif_config())

    notifications.notify("Test invalid priority", priority="INVALID")

    notifs = _get_all_notifications()
    assert len(notifs) == 1
    assert notifs[0]["priority"] == "normal"


# ── get_notifications API ────────────────────────────────────────────

def test_get_notifications_returns_records(monkeypatch):
    """get_notifications() retrieves notification records."""
    _setup_conversation()
    monkeypatch.setattr("carpenter.config.CONFIG", _notif_config())

    notifications.notify("First", priority="fyi")
    notifications.notify("Second", priority="fyi")

    result = notifications.get_notifications(limit=10)
    assert len(result) == 2
    # Most recent first
    assert result[0]["message"] == "Second"
    assert result[1]["message"] == "First"


def test_get_notifications_filter_by_status(monkeypatch):
    """get_notifications() can filter by status."""
    _setup_conversation()
    monkeypatch.setattr("carpenter.config.CONFIG", _notif_config())

    notifications.notify("Test", priority="fyi")

    # Filter by sent status
    sent = notifications.get_notifications(status="sent")
    assert len(sent) == 1

    # Filter by non-existent status
    pending = notifications.get_notifications(status="pending")
    assert len(pending) == 0


