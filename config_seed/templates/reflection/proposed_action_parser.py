"""Proposed-action classification + parsing for the dispatch-actions step.

Two stateless helpers imported by :mod:`step_handlers`. Feature-specific;
lives inside the template package.

- :func:`classify_action` — heuristic type inference (kb/code/config/other).
- :func:`parse_proposed_actions` — JSON-or-lines parser.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Keywords used for classification heuristics.
_KB_KEYWORDS = [
    "knowledge base", "kb entry", "update kb", "create kb entry",
    "modify knowledge", "new kb entry", "SKILL.md", "skill",
]
_CODE_KEYWORDS = [
    "code", "implement", "fix bug", "refactor", "write code",
    "modify code", "update code", "patch", "function", "module",
]
_CONFIG_KEYWORDS = [
    "config", "configuration", "setting", "parameter",
    "enable", "disable", "threshold", "limit",
]


def classify_action(description: str) -> str:
    """Classify an action description into a type.

    Returns one of: ``kb``, ``code``, ``config``, ``other``.
    """
    lower = description.lower()

    for kw in _KB_KEYWORDS:
        if kw in lower:
            return "kb"

    for kw in _CODE_KEYWORDS:
        if kw in lower:
            return "code"

    for kw in _CONFIG_KEYWORDS:
        if kw in lower:
            return "config"

    return "other"


def parse_proposed_actions(proposed_actions: str | None) -> list[str]:
    """Parse proposed actions from reflection output.

    Handles both JSON list format and plain-text (line-separated) format.
    Returns a list of action description strings.
    """
    if not proposed_actions or not proposed_actions.strip():
        return []

    # Try JSON first.
    try:
        parsed = json.loads(proposed_actions)
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item]
        if isinstance(parsed, str):
            return [parsed]
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
            actions.append(line)

    return actions
