"""Template capability grants for arc-level permission augmentation.

Templates can declare per-step capabilities in YAML that grant additional
tool access or scope bypasses beyond what the arc's agent_type alone allows.

Capabilities are stored in arc_state under key ``_capabilities`` as a JSON
list of strings.  At tool dispatch time, the callback middleware consults
this list to augment the agent-type tool whitelist or bypass cross-arc
read restrictions.

Capability names use a ``namespace.verb`` convention:
  - ``kb.write``      — grant KB modification tools
  - ``kb.read``       — grant KB/state read tools
  - ``system.read``   — grant broad read access + bypass cross-arc checks
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# ── Capability → tool grants ────────────────────────────────────────
# Maps capability names to sets of tool names that the capability grants.
# Used to augment agent-type allowed_tools whitelists.

CAPABILITY_TOOL_GRANTS: dict[str, frozenset[str]] = {
    "kb.write": frozenset({"kb.add", "kb.edit", "kb.delete"}),
    "kb.read": frozenset({"state.get", "state.list"}),
    "system.read": frozenset({
        "state.get", "state.list",
        "arc.get", "arc.get_children", "arc.get_history",
        "arc.get_plan", "arc.get_children_plan",
    }),
}

# ── Scope bypass capabilities ───────────────────────────────────────
# Capabilities that bypass cross-arc read restrictions (the parent-child
# descendant check in callbacks.py).

SCOPE_BYPASS_CAPABILITIES: frozenset[str] = frozenset({"system.read"})


def get_arc_capabilities(arc_id: int) -> set[str]:
    """Load capabilities for an arc from arc_state.

    Returns the set of capability strings, or empty set if none stored.
    """
    from ...db import get_db, db_connection

    with db_connection() as db:
        try:
            row = db.execute(
                "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = ?",
                (arc_id, "_capabilities"),
            ).fetchone()
            if row is None:
                return set()
            caps = json.loads(row["value_json"])
            if isinstance(caps, list):
                return set(caps)
            return set()
        except (json.JSONDecodeError, TypeError):
            logger.warning("Invalid _capabilities in arc_state for arc %d", arc_id)
            return set()


def resolve_capability_tools(capabilities: set[str]) -> frozenset[str]:
    """Resolve a set of capabilities to the union of their granted tools."""
    if not capabilities:
        return frozenset()
    granted: set[str] = set()
    for cap in capabilities:
        tools = CAPABILITY_TOOL_GRANTS.get(cap)
        if tools:
            granted |= tools
    return frozenset(granted)
