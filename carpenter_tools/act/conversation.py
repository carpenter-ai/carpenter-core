"""Conversation management tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_types={"title": "Label"})
def rename(conversation_id: int, title: str) -> dict:
    """Rename a conversation. Sets the title displayed in the conversation list."""
    ...


@tool(local=True, readonly=False, side_effects=True)
def archive(conversation_id: int) -> dict:
    """Archive a conversation (hide from active list, keep queryable)."""
    ...


@tool(local=True, readonly=False, side_effects=True)
def archive_batch(conversation_ids: list[int]) -> dict:
    """Archive multiple conversations in one call.

    Args:
        conversation_ids: List of conversation IDs to archive.

    Returns dict with archived_count and conversation_ids.
    """
    ...


@tool(local=True, readonly=False, side_effects=True)
def archive_all(exclude_ids: list[int] | None = None) -> dict:
    """Archive all conversations, optionally excluding specific ones.

    Args:
        exclude_ids: Optional list of conversation IDs to keep unarchived.

    Returns dict with archived_count.
    """
    ...
