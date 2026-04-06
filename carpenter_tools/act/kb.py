"""Knowledge Base modification tools. Tier 1: callback to platform.

Action tools only — navigation is via chat tools (kb_describe, kb_search).
"""
from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_types={"path": "WorkspacePath", "content": "UnstructuredText", "description": "UnstructuredText"})
def edit(path: str, content: str, description: str = "") -> dict:
    """Modify an existing KB entry. Content is markdown with [[links]].

    Args:
        path: KB entry path (e.g. 'scheduling/tools').
        content: New markdown content for the entry.
        description: Optional updated description.

    Returns:
        Dict with status message.
    """
    return callback("kb.edit", {
        "path": path,
        "content": content,
        "description": description,
    })


@tool(local=True, readonly=False, side_effects=True,
      param_types={"path": "WorkspacePath", "content": "UnstructuredText", "description": "UnstructuredText", "entry_type": "Label"})
def add(path: str, content: str, description: str, entry_type: str = "knowledge") -> dict:
    """Create a new KB entry at the given path.

    Args:
        path: KB entry path (e.g. 'topic/new-entry').
        content: Markdown content with [[links]].
        description: Short description for indexes and search.
        entry_type: Entry type: 'reference', 'knowledge', or 'meta'.

    Returns:
        Dict with status message.
    """
    return callback("kb.add", {
        "path": path,
        "content": content,
        "description": description,
        "entry_type": entry_type,
    })


@tool(local=True, readonly=False, side_effects=True,
      param_types={"path": "WorkspacePath"})
def delete(path: str) -> dict:
    """Delete a KB entry. Cannot delete auto-generated entries.

    Args:
        path: KB entry path to delete.

    Returns:
        Dict with status message.
    """
    return callback("kb.delete", {"path": path})
