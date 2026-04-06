"""User notification system for Carpenter.

Routes notifications through appropriate channels based on priority:
- urgent  -> chat + email (if configured)
- normal  -> chat if active session, else email (if configured)
- low     -> email only (if configured)
- fyi     -> log only

All notifications are recorded in the notifications table (log channel).

Channels:
- ChatChannel: inserts a system message into the active conversation
- EmailChannel: sends via SMTP or shell command
- LogChannel: records in the notifications database table

Batching:
- When batch_window > 0, notifications are buffered in memory
- After the window expires, all buffered notifications are flushed as one message
- Each batch gets a UUID batch_id for grouping
"""

import logging
import smtplib
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from email.mime.text import MIMEText

from .. import config
from ..db import get_db, db_connection, db_transaction

logger = logging.getLogger(__name__)

# Valid priority levels (tuple for ordering; overridable via config)
_BUILTIN_PRIORITIES = ("urgent", "normal", "low", "fyi")

# Default routing: priority -> list of channels (overridable via config)
_BUILTIN_ROUTING = {
    "urgent": ["chat", "email"],
    "normal": ["chat", "email"],
    "low": ["email"],
    "fyi": [],  # log-only
}


def _get_priorities() -> tuple:
    """Return priority levels, using config override if set."""
    notif_config = config.CONFIG.get("notifications", {})
    custom = notif_config.get("priorities")
    if custom and isinstance(custom, (list, tuple)):
        return tuple(custom)
    return _BUILTIN_PRIORITIES


def _get_default_routing() -> dict:
    """Return priority-to-channels routing, merging config overrides with defaults."""
    notif_config = config.CONFIG.get("notifications", {})
    custom = notif_config.get("default_routing")
    if not custom or not isinstance(custom, dict):
        return dict(_BUILTIN_ROUTING)
    merged = dict(_BUILTIN_ROUTING)
    merged.update(custom)
    return merged


# Module-level aliases for backward compatibility (used by _flush_batch)
PRIORITIES = _BUILTIN_PRIORITIES

# Batching state
_batch_lock = threading.Lock()
_batch_buffer: list[dict] = []
_batch_timer: threading.Timer | None = None
_batch_id: str | None = None


def notify(message: str, priority: str = "normal", category: str | None = None) -> None:
    """Send a notification through the appropriate channel(s).

    Priority levels: urgent, normal, low, fyi
    Categories: reflection_actions, review_needed, security_events, etc.

    The log channel (notifications table) is always written to.
    Other channels are determined by priority and config routing overrides.

    Args:
        message: The notification message text.
        priority: Priority level (urgent, normal, low, fyi).
        category: Optional category for routing overrides.
    """
    priorities = _get_priorities()
    if priority not in priorities:
        logger.warning("Invalid notification priority '%s', defaulting to 'normal'", priority)
        priority = "normal"

    notif_config = config.CONFIG.get("notifications", {})

    # Determine effective priority: category routing can override
    effective_priority = priority
    routing_overrides = notif_config.get("routing", {})
    if category and category in routing_overrides:
        override = routing_overrides[category]
        if override in priorities:
            effective_priority = override

    # Get channels for this priority
    default_routing = _get_default_routing()
    channels = list(default_routing.get(effective_priority, []))

    # Check batching
    batch_window = notif_config.get("batch_window", 60)
    if batch_window > 0 and effective_priority not in ("urgent",):
        _enqueue_batch(message, effective_priority, category, channels, batch_window)
        return

    # Deliver immediately
    _deliver(message, effective_priority, category, channels)


def _deliver(message: str, priority: str, category: str | None,
             channels: list[str], batch_id: str | None = None) -> None:
    """Deliver a notification to the specified channels.

    Always logs to the notifications table regardless of channels.
    """
    delivered_channels = []

    for channel_name in channels:
        try:
            if channel_name == "chat":
                _send_chat(message, priority, category)
                delivered_channels.append("chat")
            elif channel_name == "email":
                sent = _send_email(message, priority, category)
                if sent:
                    delivered_channels.append("email")
        except Exception:  # broad catch: channel delivery may raise anything
            logger.exception("Failed to deliver notification via %s", channel_name)

    # Always log to database
    _log_notification(message, priority, category, delivered_channels, batch_id)


def _send_chat(message: str, priority: str, category: str | None) -> None:
    """Insert a system message into the most recent active conversation."""
    from ..agent import conversation

    conv_id = conversation.get_last_conversation(exclude_archived=True)
    prefix = ""
    if priority == "urgent":
        prefix = "[URGENT] "
    if category:
        prefix += f"[{category}] "

    conversation.add_message(conv_id, "system", f"{prefix}{message}")
    logger.debug("Chat notification sent to conversation %d", conv_id)


def _send_email(message: str, priority: str, category: str | None) -> bool:
    """Send an email notification. Returns True if sent successfully.

    Supports two modes:
    - smtp: Standard SMTP via smtplib
    - command: Shell command that receives the message body on stdin
    """
    notif_config = config.CONFIG.get("notifications", {})
    email_config = notif_config.get("email", {})

    if not email_config.get("enabled", False):
        return False

    mode = email_config.get("mode", "smtp")

    # Build subject line
    subject_parts = ["Carpenter"]
    if priority == "urgent":
        subject_parts.append("URGENT")
    if category:
        subject_parts.append(category)
    subject = " - ".join(subject_parts)

    if mode == "command":
        return _send_email_command(email_config, message, subject)
    else:
        return _send_email_smtp(email_config, message, subject)


def _send_email_smtp(email_config: dict, message: str, subject: str) -> bool:
    """Send email via SMTP."""
    host = email_config.get("smtp_host", "")
    port = email_config.get("smtp_port", 587)
    from_addr = email_config.get("smtp_from", "")
    to_addr = email_config.get("smtp_to", "")
    username = email_config.get("smtp_username", "")
    password = email_config.get("smtp_password", "")
    use_tls = email_config.get("smtp_tls", True)

    if not host or not to_addr:
        logger.warning("Email SMTP not fully configured (missing host or to_addr)")
        return False

    try:
        msg = MIMEText(message, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = from_addr or f"carpenter@{host}"
        msg["To"] = to_addr

        if use_tls:
            smtp = smtplib.SMTP(host, port)
            smtp.starttls()
        else:
            smtp = smtplib.SMTP(host, port)

        if username and password:
            smtp.login(username, password)

        smtp.sendmail(msg["From"], [to_addr], msg.as_string())
        smtp.quit()
        logger.info("Email notification sent to %s via SMTP", to_addr)
        return True
    except (smtplib.SMTPException, OSError, ValueError) as _exc:
        logger.exception("Failed to send email via SMTP to %s", to_addr)
        return False


def _send_email_command(email_config: dict, message: str, subject: str) -> bool:
    """Send email via shell command (stdin)."""
    command = email_config.get("command", "")
    if not command:
        logger.warning("Email command mode configured but no command specified")
        return False

    try:
        # Prepend subject as header in the message body
        full_message = f"Subject: {subject}\n\n{message}"
        result = subprocess.run(
            command, input=full_message, shell=True,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("Email notification sent via command: %s", command)
            return True
        else:
            logger.warning(
                "Email command returned non-zero exit code %d: %s",
                result.returncode, result.stderr[:200],
            )
            return False
    except subprocess.TimeoutExpired:
        logger.warning("Email command timed out after 30s: %s", command)
        return False
    except OSError as _exc:
        logger.exception("Failed to send email via command: %s", command)
        return False


def _log_notification(message: str, priority: str, category: str | None,
                      channels: list[str], batch_id: str | None = None) -> None:
    """Record a notification in the notifications table."""
    channel_str = ",".join(channels) if channels else "log"
    with db_transaction() as db:
        try:
            db.execute(
                "INSERT INTO notifications (message, priority, category, channel, status, batch_id, sent_at) "
                "VALUES (?, ?, ?, ?, 'sent', ?, CURRENT_TIMESTAMP)",
                (message, priority, category, channel_str, batch_id),
            )
        except sqlite3.Error as _exc:
            logger.exception("Failed to log notification to database")


# ── Batching ─────────────────────────────────────────────────────────

def _enqueue_batch(message: str, priority: str, category: str | None,
                   channels: list[str], batch_window: int) -> None:
    """Buffer a notification for batched delivery."""
    global _batch_timer, _batch_id

    with _batch_lock:
        if _batch_id is None:
            _batch_id = uuid.uuid4().hex[:12]

        _batch_buffer.append({
            "message": message,
            "priority": priority,
            "category": category,
            "channels": channels,
        })

        # Start or reset the timer
        if _batch_timer is not None:
            _batch_timer.cancel()

        _batch_timer = threading.Timer(batch_window, _flush_batch)
        _batch_timer.daemon = True
        _batch_timer.start()


def _flush_batch() -> None:
    """Flush all buffered notifications as a single combined message."""
    global _batch_timer, _batch_id

    with _batch_lock:
        if not _batch_buffer:
            return

        items = list(_batch_buffer)
        batch_id = _batch_id
        _batch_buffer.clear()
        _batch_timer = None
        _batch_id = None

    # Combine messages
    if len(items) == 1:
        combined_message = items[0]["message"]
    else:
        parts = []
        for i, item in enumerate(items, 1):
            prefix = ""
            if item["category"]:
                prefix = f"[{item['category']}] "
            parts.append(f"{i}. {prefix}{item['message']}")
        combined_message = f"Batched notifications ({len(items)}):\n" + "\n".join(parts)

    # Determine most urgent priority (lowest index in PRIORITIES tuple)
    priority_order = {p: i for i, p in enumerate(PRIORITIES)}
    highest_priority = min(
        (item["priority"] for item in items),
        key=lambda p: priority_order.get(p, 99),
        default="normal",
    )

    all_channels = set()
    for item in items:
        all_channels.update(item["channels"])

    # Determine combined category
    categories = set(item["category"] for item in items if item["category"])
    combined_category = ", ".join(sorted(categories)) if categories else None

    _deliver(combined_message, highest_priority, combined_category,
             list(all_channels), batch_id=batch_id)


def flush_now() -> None:
    """Force-flush any pending batched notifications immediately.

    Useful for testing and shutdown scenarios.
    """
    global _batch_timer
    with _batch_lock:
        if _batch_timer is not None:
            _batch_timer.cancel()
            _batch_timer = None
    _flush_batch()


def get_notifications(limit: int = 50, status: str | None = None) -> list[dict]:
    """Retrieve notifications from the database.

    Args:
        limit: Maximum number of notifications to return.
        status: Filter by status (e.g., 'sent', 'pending'). None = all.

    Returns:
        List of notification dicts, most recent first.
    """
    with db_connection() as db:
        if status:
            rows = db.execute(
                "SELECT * FROM notifications WHERE status = ? "
                "ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM notifications ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
