"""Conversation management tools. Tier 1: callback to platform."""
from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_types={"title": "Label"})
def rename(conversation_id: int, title: str) -> dict:
    """Rename a conversation. Sets the title displayed in the conversation list."""
    return callback("conversation.rename", {
        "conversation_id": conversation_id,
        "title": title,
    })


@tool(local=True, readonly=False, side_effects=True)
def archive(conversation_id: int) -> dict:
    """Archive a conversation (hide from active list, keep queryable)."""
    return callback("conversation.archive", {
        "conversation_id": conversation_id,
    })


@tool(local=True, readonly=False, side_effects=True)
def archive_batch(conversation_ids: list[int]) -> dict:
    """Archive multiple conversations in one call.

    Args:
        conversation_ids: List of conversation IDs to archive.

    Returns dict with archived_count and conversation_ids.
    """
    return callback("conversation.archive_batch", {
        "conversation_ids": conversation_ids,
    })


@tool(local=True, readonly=False, side_effects=True)
def archive_all(exclude_ids: list[int] | None = None) -> dict:
    """Archive all conversations, optionally excluding specific ones.

    Args:
        exclude_ids: Optional list of conversation IDs to keep unarchived.

    Returns dict with archived_count.
    """
    params = {}
    if exclude_ids is not None:
        params["exclude_ids"] = exclude_ids
    return callback("conversation.archive_all", params)
