"""Knowledge Base modification tool backend.

Handles add/edit/delete operations on KB entries from executor callbacks.
All writes go through submit_code review pipeline first.
"""

import logging
import re

from ..kb import get_store

logger = logging.getLogger(__name__)

# Valid KB path: lowercase letters, digits, hyphens, underscores, slashes
_VALID_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9_/-]*$")


def _validate_path(path: str) -> str | None:
    """Validate a KB path. Returns error message or None if valid."""
    if not path:
        return "path is required"
    if ".." in path:
        return "path cannot contain '..'"
    if path.startswith("/"):
        return "path cannot be absolute"
    if not _VALID_PATH_RE.match(path):
        return "path must be lowercase letters, digits, hyphens, underscores, and slashes"
    return None


def handle_edit(params: dict) -> dict:
    """Edit an existing KB entry.

    params:
        path: KB entry path
        content: New markdown content (aliases: body, text, markdown)
        description: Optional updated description (aliases: summary, desc, title)
    """
    path = params.get("path", "")
    content = params.get("content", "") or params.get("body", "") or params.get("text", "") or params.get("markdown", "")
    description = params.get("description", "") or params.get("summary", "") or params.get("desc", "") or params.get("title", "")

    error = _validate_path(path)
    if error:
        return {"error": error}

    if not content:
        return {"error": "content is required"}

    store = get_store()

    # Verify entry exists
    existing = store.get_entry(path)
    if existing is None:
        return {"error": f"KB entry not found: {path}"}

    conversation_id = params.get("conversation_id")
    result = store.write_entry(
        path=path,
        content=content,
        description=description,
        entry_type=existing.get("entry_type", "knowledge"),
        trust_level=existing.get("trust_level", "trusted"),
        conversation_id=conversation_id,
    )
    if result.startswith("Error"):
        return {"error": result}
    store.queue_change(path, "modified")
    return {"status": result}


def handle_add(params: dict) -> dict:
    """Create a new KB entry.

    params:
        path: KB entry path
        content: Markdown content (aliases: body, text, markdown)
        description: Short description (aliases: summary, desc, title)
        entry_type: knowledge | reference | meta
    """
    path = params.get("path", "")
    content = params.get("content", "") or params.get("body", "") or params.get("text", "") or params.get("markdown", "")
    description = params.get("description", "") or params.get("summary", "") or params.get("desc", "") or params.get("title", "")
    entry_type = params.get("entry_type", "knowledge")

    error = _validate_path(path)
    if error:
        return {"error": error}

    if not content:
        return {"error": "content is required"}
    if not description:
        # Auto-generate description from the first non-heading line of content
        for line in content.strip().splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped and not stripped.startswith("---"):
                description = stripped[:120]
                break
        if not description:
            description = path.split("/")[-1].replace("-", " ").replace("_", " ")

    store = get_store()

    # Check entry doesn't already exist
    existing = store.get_entry(path)
    if existing is not None:
        return {"error": f"KB entry already exists: {path}. Use kb.edit() instead."}

    conversation_id = params.get("conversation_id")
    result = store.write_entry(
        path=path,
        content=content,
        description=description,
        entry_type=entry_type,
        conversation_id=conversation_id,
    )
    if result.startswith("Error"):
        return {"error": result}
    store.queue_change(path, "added")
    return {"status": result}


def handle_delete(params: dict) -> dict:
    """Delete a KB entry.

    params:
        path: KB entry path to delete
    """
    path = params.get("path", "")

    error = _validate_path(path)
    if error:
        return {"error": error}

    store = get_store()
    result = store.delete_entry(path)
    if result.startswith("Error"):
        return {"error": result}

    store.queue_change(path, "deleted")
    return {"status": result}
