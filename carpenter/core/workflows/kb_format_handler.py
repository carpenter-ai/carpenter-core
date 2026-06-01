"""Deterministic KB-format handler for the ``kb-change`` workflow.

Walks every changed ``*.md`` file in the arc's workspace and checks:

1. KB path-regex validity (``carpenter.tool_backends.kb._VALID_PATH_RE``)
   applied to the path beneath any ``kb/`` segment so files moved to a
   nonsense path are caught at lint time.
2. YAML frontmatter validity when a ``---`` fence is present. (KB files
   do not require frontmatter today; we only fail when frontmatter is
   present and malformed.)

Internal ``[[link]]`` cross-reference checking is currently advisory
(TODO — no shared utility exists in this repo).

Python-only (I6): no LLM is invoked on the trust boundary. Failure of
any single check fails the step, which fails the arc.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import yaml

from .. import workspace_manager
from ._arc_state import get_arc_state as _get_arc_state, set_arc_state as _set_arc_state
from ..arcs import manager as arc_manager
from ...tool_backends.kb import _VALID_PATH_RE  # type: ignore[attr-defined]

logger = logging.getLogger(__name__)


def _kb_subpath(rel_path: str) -> str | None:
    """Return the KB-style path (no extension) for *rel_path*, or None.

    Strips a leading ``...kb/`` prefix and the trailing ``.md`` extension
    so the result is the same form that
    :func:`carpenter.tool_backends.kb._validate_path` validates.
    """
    norm = rel_path.replace("\\", "/")
    if not norm.endswith(".md"):
        return None
    # Find rightmost "kb/" segment.
    parts = norm.split("/")
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "kb":
            sub = "/".join(parts[i + 1:])
            if sub.endswith(".md"):
                sub = sub[: -len(".md")]
            return sub
    # Not under a ``kb/`` directory — treat as docs/* style; return name minus ext.
    return norm[: -len(".md")]


_FRONTMATTER_FENCE = "---"


def _extract_frontmatter(text: str) -> tuple[str | None, str | None]:
    """Extract YAML frontmatter from *text*.

    Returns ``(frontmatter_text, error_msg)``. ``frontmatter_text`` is
    None if no frontmatter is present. ``error_msg`` is None on success.
    """
    if not text.startswith(_FRONTMATTER_FENCE + "\n") and not text.startswith(_FRONTMATTER_FENCE + "\r\n"):
        return None, None
    # Find closing fence
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        return None, None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_FENCE:
            return "\n".join(lines[1:i]), None
    return None, "Unterminated frontmatter (no closing '---' fence)"


def check_workspace_kb(workspace_path: str) -> dict[str, Any]:
    """Validate every changed Markdown file in *workspace_path*.

    Returns ``{"ok": bool, "findings": [...], "files": [...]}``.
    """
    findings: list[dict[str, Any]] = []
    files_checked: list[str] = []

    try:
        changed = workspace_manager.get_changed_files(workspace_path)
    except Exception as exc:  # noqa: BLE001
        findings.append({
            "file": "",
            "severity": "error",
            "message": f"Could not list changed files: {exc}",
        })
        return {"ok": False, "findings": findings, "files": files_checked}

    for rel_path in changed:
        if not rel_path.endswith(".md"):
            continue
        files_checked.append(rel_path)

        # ── Path-regex ───────────────────────────────────────────────
        sub = _kb_subpath(rel_path)
        if sub is None:
            findings.append({
                "file": rel_path,
                "severity": "error",
                "message": "Not a Markdown file (.md)",
            })
            continue
        if not sub:
            findings.append({
                "file": rel_path,
                "severity": "error",
                "message": "KB path is empty after stripping kb/ prefix",
            })
        elif not _VALID_PATH_RE.match(sub):
            findings.append({
                "file": rel_path,
                "severity": "error",
                "message": (
                    f"KB path {sub!r} does not match required regex "
                    f"(lowercase letters, digits, hyphens, underscores, slashes)"
                ),
            })

        # ── Frontmatter validity ─────────────────────────────────────
        abs_path = os.path.join(workspace_path, rel_path)
        try:
            with open(abs_path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            findings.append({
                "file": rel_path,
                "severity": "error",
                "message": f"Could not read file: {exc}",
            })
            continue

        fm_text, fm_err = _extract_frontmatter(text)
        if fm_err:
            findings.append({"file": rel_path, "severity": "error", "message": fm_err})
            continue
        if fm_text is not None:
            try:
                yaml.safe_load(fm_text)
            except yaml.YAMLError as exc:
                findings.append({
                    "file": rel_path,
                    "severity": "error",
                    "message": f"Frontmatter YAML parse error: {exc}",
                })

        # TODO: Internal [[link]] reference checking (no shared utility yet).

    ok = not any(f.get("severity") == "error" for f in findings)
    return {"ok": ok, "findings": findings, "files": files_checked}


async def handle_verify_kb_format(work_id: int, payload: dict) -> None:
    """Work-queue handler for the ``kb-change.verify-kb-format`` step."""
    arc_id = payload.get("arc_id")
    if not arc_id:
        logger.error("verify-kb-format: missing arc_id in payload")
        return

    workspace_path = _get_arc_state(arc_id, "workspace_path")
    if not workspace_path or not os.path.isdir(workspace_path):
        msg = f"workspace_path missing or absent: {workspace_path!r}"
        arc_manager.add_history(arc_id, "verify_kb_format_failed", {"message": msg})
        _set_arc_state(arc_id, "_kb_format_result", {"ok": False, "findings": [{"message": msg}]})
        try:
            arc_manager.update_status(arc_id, "failed")
        except ValueError:
            pass
        return

    result = check_workspace_kb(workspace_path)
    _set_arc_state(arc_id, "_kb_format_result", result)
    arc_manager.add_history(
        arc_id,
        "verify_kb_format_completed",
        {"ok": result["ok"], "file_count": len(result["files"]), "finding_count": len(result["findings"])},
    )
    if not result["ok"]:
        try:
            arc_manager.update_status(arc_id, "failed")
        except ValueError:
            pass
