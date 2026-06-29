"""Proposed-action parsing for the dispatch-actions step.

Stateless helper imported by :mod:`step_handlers`. Feature-specific;
lives inside the template package.

- :func:`parse_proposed_actions` — JSON-or-lines parser returning structured
  dicts ``{"description": str, "target_path": str | None}``.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


# A "path-like" token: contains a dot followed by an extension, may contain
# slashes/dots/underscores/hyphens.  Matches both relative (``kb/foo.md``)
# and absolute (``/abs/path/foo.py``) forms.  Conservative — we only treat
# a backticked token as a path if it has an extension; bare ``foo`` words
# would otherwise produce too many false positives.
_PATH_TOKEN_RE = re.compile(r"^[\w./\-]+\.[\w]+$")
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def _extract_target_path(description: str) -> str | None:
    """Return the first backticked path-like token in *description*, or None.

    Strict-but-tolerant: a description without a backticked path-like
    token returns ``None``.  Multiple backticked tokens are scanned in
    order; the first match wins.
    """
    if not description:
        return None
    for match in _BACKTICK_RE.finditer(description):
        token = match.group(1).strip()
        if _PATH_TOKEN_RE.match(token):
            return token
    return None


def _make_action(description: str, target_path: str | None = None) -> dict:
    """Build a structured action dict from a description string.

    If ``target_path`` is not supplied, it is extracted heuristically from
    backticked path-like tokens in the description.
    """
    desc = description.strip()
    if target_path is None:
        target_path = _extract_target_path(desc)
    return {"description": desc, "target_path": target_path}


def parse_proposed_actions(proposed_actions: str | None) -> list[dict]:
    """Parse proposed actions from reflection output.

    Handles both JSON (list of strings or list of objects) and plain-text
    (line-separated) formats.  Returns a list of structured dicts shaped
    ``{"description": str, "target_path": str | None}``.

    A description without a backticked path-like token still produces a
    valid action with ``target_path = None``.  Already-structured JSON
    objects (with their own ``description``/``target_path`` fields) are
    preserved; missing ``target_path`` falls back to backtick extraction.
    """
    if not proposed_actions or not proposed_actions.strip():
        return []

    # Try JSON first.
    try:
        parsed = json.loads(proposed_actions)
        if isinstance(parsed, list):
            actions: list[dict] = []
            for item in parsed:
                if not item:
                    continue
                if isinstance(item, dict):
                    desc = str(item.get("description", "")).strip()
                    if not desc:
                        # No description; skip rather than spawn an empty action.
                        continue
                    tp = item.get("target_path")
                    if tp is not None and not isinstance(tp, str):
                        tp = None
                    if not tp:
                        tp = _extract_target_path(desc)
                    actions.append({"description": desc, "target_path": tp or None})
                else:
                    actions.append(_make_action(str(item)))
            return actions
        if isinstance(parsed, str):
            return [_make_action(parsed)]
    except (json.JSONDecodeError, TypeError):
        pass

    # Fall back to line-separated text.
    actions = []
    for line in proposed_actions.strip().split("\n"):
        line = line.strip()
        if line.startswith("- "):
            line = line[2:]
        elif line.startswith("* "):
            line = line[2:]
        elif len(line) > 2 and line[0].isdigit() and line[1] in (".", ")"):
            line = line[2:].strip()
        elif len(line) > 3 and line[:2].isdigit() and line[2] in (".", ")"):
            line = line[3:].strip()

        if line:
            actions.append(_make_action(line))

    return actions
