"""Shared notification helpers for workflow handlers.

Provides utilities for injecting system messages into arc-linked conversations
and routing notifications through the notification system.
"""

from ...agent import conversation
from .. import notifications


def notify_arc_conversation(
    arc_id: int,
    message: str,
    conversation_id: int | None = None,
    *,
    also_notify: bool = False,
    priority: str = "normal",
    category: str = "info",
) -> None:
    """Inject a system message into the conversation linked to an arc.

    Args:
        arc_id: The arc whose conversation should receive the message
        message: The message to inject
        conversation_id: Optional conversation ID; if None, will be fetched
            from arc state using _get_arc_state
        also_notify: If True, also route through notifications.notify()
        priority: Priority level for notification system (if also_notify=True)
        category: Category for notification system (if also_notify=True)
    """
    # If conversation_id not provided, caller must fetch from arc state
    if conversation_id is None:
        raise ValueError(
            "conversation_id is required; fetch from arc state before calling"
        )

    # Inject system message into the conversation
    if conversation_id is not None:
        conversation.add_message(conversation_id, "system", message, arc_id=arc_id)

    # Optionally route through notification system (chat + email for urgent)
    if also_notify:
        notifications.notify(message, priority=priority, category=category)
