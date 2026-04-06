"""Conversation tool backend — handles conversation mutation callbacks."""
import logging

from ..agent.conversation import (
    set_conversation_title,
    archive_conversation,
    archive_conversations_batch,
    archive_all_conversations,
)

logger = logging.getLogger(__name__)


def handle_rename(params: dict) -> dict:
    """Rename a conversation. Params: conversation_id, title."""
    conversation_id = params.get("conversation_id")
    title = params.get("title", "")
    if not conversation_id:
        return {"error": "conversation_id is required"}
    if not title:
        return {"error": "title is required"}
    set_conversation_title(conversation_id, title)
    return {"conversation_id": conversation_id, "title": title}


def handle_archive(params: dict) -> dict:
    """Archive a conversation. Params: conversation_id."""
    conversation_id = params.get("conversation_id")
    if not conversation_id:
        return {"error": "conversation_id is required"}
    archive_conversation(conversation_id)
    return {"conversation_id": conversation_id, "archived": True}


def handle_archive_batch(params: dict) -> dict:
    """Archive multiple conversations. Params: conversation_ids (list of int)."""
    conversation_ids = params.get("conversation_ids")
    if not conversation_ids or not isinstance(conversation_ids, list):
        return {"error": "conversation_ids must be a non-empty list of ints"}
    if not all(isinstance(i, int) for i in conversation_ids):
        return {"error": "conversation_ids must be a non-empty list of ints"}
    count = archive_conversations_batch(conversation_ids)
    return {"archived_count": count, "conversation_ids": conversation_ids}


def handle_archive_all(params: dict) -> dict:
    """Archive all conversations. Params: exclude_ids (optional list of int)."""
    exclude_ids = params.get("exclude_ids")
    if exclude_ids is not None:
        if not isinstance(exclude_ids, list) or not all(isinstance(i, int) for i in exclude_ids):
            return {"error": "exclude_ids must be a list of ints"}
    count = archive_all_conversations(exclude_ids)
    return {"archived_count": count}
