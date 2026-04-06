"""Messaging tool backend."""
import logging

from ..agent import conversation
from ..db import get_db

logger = logging.getLogger(__name__)


def handle_send(params: dict) -> dict:
    """Handle messaging.send -- deliver message into the conversation.

    Expects ``conversation_id`` in *params* (auto-injected by
    ``_callback.py`` from the ``TC_CONVERSATION_ID`` env var).  If no
    conversation context is available the message is logged but not
    persisted.

    Optionally accepts ``arc_id`` to tag the message as originating
    from an arc executor.
    """
    message = params.get("message", "")
    conv_id = params.get("conversation_id")
    arc_id = params.get("arc_id")

    if conv_id is None:
        logger.warning("messaging.send without conversation_id, message logged only: %s", message)
        return {"success": True, "delivered": False}

    conversation.add_message(conv_id, "assistant", message, arc_id=arc_id)
    logger.info("Message delivered to conversation %s (arc=%s): %s", conv_id, arc_id, message[:80])
    return {"success": True, "delivered": True}


def handle_ask(params: dict) -> dict:
    """Handle messaging.ask -- placeholder for user interaction."""
    question = params.get("question", "")
    logger.info("Question from executor: %s", question)
    # In Phase 6 (chat interface), this will create a prompt and wait for user input
    # For now, return a placeholder
    return {"answer": "", "pending": True}
