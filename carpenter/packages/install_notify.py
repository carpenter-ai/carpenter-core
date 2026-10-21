"""Tell the conversation that asked what the package install did.

The chat agent asked for an install and the turn ended there (the tool
returned a ``FinalReply``).  Minutes or hours later a human approves the
request and the platform installs it, with no model in the loop.  This
module closes that circle: it writes one **visible** system message into
the originating conversation, so the person reading the chat sees the
outcome, and pushes the same text out to a non-web channel if the
conversation came in over one.

**Zero model calls by default.**  ``conversation.add_message`` persists
and does nothing else (it deliberately performs no outbound delivery),
and the web UI renders a non-hidden system message from its
``/api/chat/pending`` poll.  So the operator learns what happened for
the price of one INSERT.  That is the whole design: re-invoking the
chat agent to have it relay the news — what ``arc_notify_handler`` does
for arcs — costs a model call per approval, every approval, and buys a
paraphrase of a report that is already exact.  The re-invocation
exists behind ``package_install.notify_agent``, default **false**, for
an operator who wants the agent to act on the result; nothing in this
package turns it on.

A conversation with no id (an install requested where no conversation
was recorded) is not an error: there is nobody to tell, so nothing is
written.  A failed channel push is not an error either — the message is
already durable in the conversation, and failing the install because a
Signal socket was down would be absurd.
"""

from __future__ import annotations

import logging

from . import install_batch
from .pending_install import (
    STATUS_FAILED,
    STATUS_INSTALLED,
    PendingInstall,
)

logger = logging.getLogger(__name__)

__all__ = [
    "notify_agent_enabled",
    "notify_install_outcome",
    "render_install_message",
]

#: Channel types that need no outbound push: the web UI polls for new
#: messages, and an embedded/internal conversation has no transport.
_POLLED_CHANNELS = frozenset({"", "web", "embedded", "internal"})


def notify_agent_enabled(config: dict | None = None) -> bool:
    """Whether the operator asked for the chat agent to be re-invoked.

    Default **false**, and deliberately read at call time rather than
    captured, so turning it off takes effect on the next install without
    a restart.
    """
    if config is None:
        from .. import config as _config
        config = _config.CONFIG
    section = config.get("package_install")
    if not isinstance(section, dict):
        return False
    return bool(section.get("notify_agent", False))


def render_install_message(record: PendingInstall) -> str:
    """Build the chat message for a finished (or failed) install request.

    Rendered from ``result_json``, so the message cannot claim an
    outcome the stored record does not have.  Never contains the
    request token: the message goes wherever the conversation goes, and
    the token is a credential — a spent one, but one that names the
    approval page.
    """
    result = record.result if isinstance(record.result, dict) else {}
    lines = install_batch.summarise(result)

    if record.status == STATUS_INSTALLED:
        header = "Package install finished."
    elif record.status == STATUS_FAILED:
        header = "Package install FAILED. Nothing is half-installed:"
    else:
        header = f"Package install request is now {record.status!r}."

    parts = [header]
    if lines:
        parts.append("\n".join(lines))
    elif record.error:
        parts.append(f"- {record.error}")

    if record.status == STATUS_FAILED and record.error and lines:
        parts.append(f"Reason: {record.error}")

    rolled_back = list(result.get("rolled_back") or ())
    if rolled_back:
        parts.append(
            "Rolled back (installed by this request, then uninstalled "
            "again): " + ", ".join(rolled_back) + ".",
        )

    kept = list(result.get("security_policies_kept") or ())
    if kept:
        parts.append(
            "These security policy entries were added by a rolled-back "
            "install and were NOT removed, because uninstall is a one-way "
            "ratchet (SD5): "
            + ", ".join(
                f"{entry.get('type')}={entry.get('value')} "
                f"(from {entry.get('package')})"
                for entry in kept
                if isinstance(entry, dict)
            )
            + ". Remove them by hand if you do not want them.",
        )

    if any(
        isinstance(block, dict)
        and isinstance(block.get("load"), dict)
        and block["load"].get("needs_restart")
        for block in result.get("packages") or ()
    ):
        parts.append(
            "A platform restart is needed before the package(s) marked "
            "above are usable. Nothing was restarted automatically.",
        )

    if record.status == STATUS_FAILED:
        parts.append(
            "Nothing was re-fetched and nothing was auto-retried. Ask "
            "again if you still want this, and a new approval link will "
            "be minted.",
        )

    return "\n".join(parts)


def _channel_connector(channel_type: str | None):
    """The started channel connector for ``channel_type``, or None.

    Mirrors ``reflection_escalation._get_email_connector``: a connector
    that raised during ``start()`` stays in the registry with
    ``started`` False, and pushing into a half-initialised connector
    fails silently inside it, so ``started`` is checked rather than
    assumed.
    """
    if not channel_type or channel_type in _POLLED_CHANNELS:
        return None
    try:
        from ..channels.registry import get_connector_registry
    except ImportError:  # pragma: no cover — stripped build
        return None
    registry = get_connector_registry()
    if registry is None:
        return None
    for connector in registry.list_connectors(kind="channel"):
        if getattr(connector, "channel_type", None) != channel_type:
            continue
        if not getattr(connector, "enabled", True):
            continue
        if not getattr(connector, "started", True):
            continue
        return connector
    return None


async def notify_install_outcome(
    record: PendingInstall, *, config: dict | None = None,
) -> bool:
    """Write the outcome into the conversation that asked, and push it.

    Args:
        record: The finished request, as
            ``mark_installed`` / ``mark_failed`` returned it — the
            status and ``result_json`` are read from it.
        config: Config override (tests).

    Returns:
        True when a message was persisted to a conversation.  False
        means there was nobody to tell, which is not a failure.
    """
    conv_id = record.conversation_id
    if not conv_id:
        logger.info(
            "Package install request %s finished (%s) with no conversation "
            "to notify",
            record.token_prefix, record.status,
        )
        return False

    message = render_install_message(record)

    from ..agent import conversation

    try:
        conversation.add_message(conv_id, "system", message, hidden=False)
    except Exception:  # noqa: BLE001 — the install already happened
        logger.warning(
            "Package install request %s: could not write the outcome to "
            "conversation %s",
            record.token_prefix, conv_id, exc_info=True,
        )
        return False

    logger.warning(
        "Package install request %s: notified conversation %s (%s)",
        record.token_prefix, conv_id, record.status,
    )

    connector = _channel_connector(record.channel_type)
    if connector is not None:
        try:
            await connector.send_message(conv_id, message)
        except Exception:  # noqa: BLE001 — delivery is best-effort
            logger.warning(
                "Package install request %s: push to the %r channel failed; "
                "the message is still in the conversation",
                record.token_prefix, record.channel_type, exc_info=True,
            )

    if notify_agent_enabled(config):
        await _reinvoke_agent(record, conv_id, message)

    return True


async def _reinvoke_agent(
    record: PendingInstall, conv_id: int, message: str,
) -> None:
    """Re-invoke the chat agent on the outcome — only if asked to.

    Off by default and never called otherwise.  It costs one model call
    per approved install, which is exactly the cost this module exists
    to avoid for a notification the platform can render itself.
    """
    logger.warning(
        "Package install request %s: package_install.notify_agent is on, "
        "so the chat agent is being re-invoked (this costs a model call)",
        record.token_prefix,
    )
    try:
        from .. import thread_pools
        from ..agent import invocation

        await thread_pools.run_in_work_pool(
            invocation.invoke_for_chat,
            message,
            conversation_id=conv_id,
            _message_already_saved=True,
            _system_triggered=True,
        )
    except Exception:  # noqa: BLE001 — the notification already landed
        logger.warning(
            "Package install request %s: the optional agent re-invocation "
            "failed; the outcome message is still in the conversation",
            record.token_prefix, exc_info=True,
        )
