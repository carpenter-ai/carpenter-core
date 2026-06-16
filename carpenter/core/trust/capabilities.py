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
    """Resolve a set of capabilities to the union of their granted tools.

    Static capability → tool grants come from :data:`CAPABILITY_TOOL_GRANTS`.
    In addition, a per-package capability grant (``pkg.<name>``, issued to
    a package's own arcs by the package-capability framework) resolves to
    that package's registered trusted dispatch verbs — so an arc carrying
    the grant may invoke its package's verbs through the agent-type
    whitelist path.  The verbs themselves are still gated per-package in
    the dispatch bridge (fail-closed); this only widens the agent-type
    allow-list for arcs that legitimately hold the grant.
    """
    if not capabilities:
        return frozenset()
    granted: set[str] = set()
    for cap in capabilities:
        tools = CAPABILITY_TOOL_GRANTS.get(cap)
        if tools:
            granted |= tools
        # Per-package capability grant → that package's registered verbs.
        if cap.startswith("pkg."):
            package_name = cap[len("pkg."):]
            if package_name:
                try:
                    from ...packages.capabilities import get_capability_registry
                    granted |= set(
                        get_capability_registry().verbs_for_package(package_name)
                    )
                except ImportError:  # pragma: no cover — defensive
                    pass
    return frozenset(granted)
