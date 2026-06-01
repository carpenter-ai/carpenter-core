"""Knowledge Base modification tool backend.

Handles add/edit/delete operations on KB entries from executor callbacks.
All writes go through submit_code review pipeline first.

Platform-integrity tier enforcement (PR 4 of 6 in the platform-integrity
rollout): KB paths resolve to filesystem locations under either
``config_seed/kb/*.md`` (platform seed — T1) or
``<carpenter_home>/config/kb/*.md`` (user store — T2).  Writes to T1 or
T0 paths via ``kb.add`` / ``kb.edit`` / ``kb.delete`` are refused with
a ``DispatchError``; reads (none exposed today; ``state.get`` covers
that surface) get T0-only gating per the standard tier policy.

DispatchError is imported lazily to avoid a circular import via
``api.callbacks`` (the same pattern as ``tool_backends.files``).
"""

import logging
import os
import re

from ..kb import get_store
from ..security.platform_paths import (
    PATH_TIER_T0,
    PATH_TIER_T1,
    audit_path_decision,
    path_tier,
)

logger = logging.getLogger(__name__)

# Valid KB path: lowercase letters, digits, hyphens, underscores, slashes
_VALID_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9_/-]*$")


def _get_dispatch_error_cls():
    """Lazy import of DispatchError to avoid circular import via callbacks."""
    from ..executor.dispatch_bridge import DispatchError
    return DispatchError


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


def _resolve_kb_fs_path(store, kb_path: str) -> str:
    """Resolve a KB entry's filesystem path.

    For existing entries, defer to the store's own ``_fs_path`` (it
    knows about ``_index.md`` folder entries and leaf ``.md`` files).
    For new entries (no file yet) the canonical write target is the
    same path used by ``write_entry``: ``<kb_dir>/<path>.md``.  Either
    way we want the realpath so tier classification matches against the
    same canonical form used by other gates.
    """
    fs_path = store._fs_path(kb_path)
    if not fs_path:
        # New entry — use the write target.
        fs_path = os.path.join(store.kb_dir, kb_path + ".md")
    try:
        return os.path.realpath(fs_path)
    except Exception:  # noqa: BLE001
        return fs_path


def _enforce_kb_tier(op: str, fs_path: str, params: dict) -> None:
    """Raise DispatchError(403) if ``fs_path`` is T0 or T1.

    Records a tier-specific audit row with ``tool=kb.<op>`` so the
    integrity audit feed is consistent across kb / files / config_tool.
    """
    tier = path_tier(fs_path)
    caller_arc_id = params.get("_caller_arc_id")
    if tier == PATH_TIER_T0:
        audit_path_decision(
            caller_arc_id,
            "t0_write_refused",
            fs_path,
            {"tool": f"kb.{op}"},
        )
        logger.warning(
            "kb.%s refused: T0 (invisible) path %s for caller arc %s",
            op, fs_path, caller_arc_id,
        )
        DispatchError = _get_dispatch_error_cls()
        raise DispatchError(
            "KB path is platform-invisible",
            status_code=403,
        )
    if tier == PATH_TIER_T1:
        audit_path_decision(
            caller_arc_id,
            "t1_write_refused",
            fs_path,
            {"tool": f"kb.{op}"},
        )
        logger.warning(
            "kb.%s refused: T1 (platform) path %s for caller arc %s",
            op, fs_path, caller_arc_id,
        )
        DispatchError = _get_dispatch_error_cls()
        raise DispatchError(
            "KB entry is in a platform-protected area; "
            "use the kb-change workflow",
            status_code=403,
        )


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

    # Platform-integrity tier gate (I12) — resolve the on-disk path and
    # refuse T0/T1 writes.  Runs after _validate_path and entry-exists
    # check so the failure mode is the same shape regardless of whether
    # the caller mis-spelled the path or aimed at the platform area.
    fs_path = _resolve_kb_fs_path(store, path)
    _enforce_kb_tier("edit", fs_path, params)

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

    # Platform-integrity tier gate — refuse T0/T1 writes.  The resolved
    # path is the canonical write target (``<kb_dir>/<path>.md``) used by
    # ``write_entry``.
    fs_path = _resolve_kb_fs_path(store, path)
    _enforce_kb_tier("add", fs_path, params)

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

    # Platform-integrity tier gate — refuse T0/T1 deletes.  Mirrors
    # the add/edit gate so deletion can't be used to remove a T1
    # platform KB entry out from under the platform-protected workflow.
    fs_path = _resolve_kb_fs_path(store, path)
    _enforce_kb_tier("delete", fs_path, params)

    result = store.delete_entry(path)
    if result.startswith("Error"):
        return {"error": result}

    store.queue_change(path, "deleted")
    return {"status": result}
