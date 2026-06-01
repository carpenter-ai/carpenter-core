"""Named arc outputs with sibling-by-role resolution.

Today, a step handler that needs data from a sibling arc reads a
platform-internal field (e.g. ``_agent_response``) from that sibling's
``arc_state``, and looks the sibling up by hardcoded ``name`` column match.
Both halves are brittle: ``_agent_response`` is an engine implementation
detail, and ``name`` is an arbitrary string that tends to be the step label.

This module adds two layers on top of ``arc_state``:

* **Named outputs.** A step declares outputs it produces and writes them by
  name. A downstream step reads by the same name. The storage is still
  ``arc_state``, namespaced under the ``output:{name}`` key convention so
  outputs do not collide with other workflow-internal state.
* **Sibling-by-role resolution.** A step finds a sibling by its declared
  template role (not the database ``name`` column). This makes the lookup
  stable against renames of individual steps. For backwards compatibility,
  when a step has no ``role`` declared, its ``name`` is accepted as the role.

This module is a primitive: it adds APIs, does not wire up any template, and
does not change any existing behaviour. Templates and step handlers will
migrate to it in follow-up PRs.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ...db import db_connection, db_transaction

_OUTPUT_KEY_PREFIX = "output:"


def _output_key(name: str) -> str:
    if not name:
        raise ValueError("output name must be non-empty")
    if ":" in name:
        raise ValueError(f"output name may not contain ':' (got {name!r})")
    return _OUTPUT_KEY_PREFIX + name


def set_arc_output(arc_id: int, name: str, value: Any) -> None:
    """Record a named output for an arc.

    Values must be JSON-serialisable. Re-setting the same name overwrites.
    """
    key = _output_key(name)
    value_json = json.dumps(value)  # raise now, not later, for bad types
    with db_transaction() as db:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?) "
            "ON CONFLICT(arc_id, key) DO UPDATE SET value_json = excluded.value_json, "
            "updated_at = CURRENT_TIMESTAMP",
            (arc_id, key, value_json),
        )


def get_arc_output(arc_id: int, name: str, default: Any = None) -> Any:
    """Read a named output from an arc. Returns ``default`` if not set."""
    key = _output_key(name)
    with db_connection() as db:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = ?",
            (arc_id, key),
        ).fetchone()
        return json.loads(row["value_json"]) if row else default


def list_arc_outputs(arc_id: int) -> dict[str, Any]:
    """Return all named outputs for an arc as ``{name: value}``."""
    with db_connection() as db:
        rows = db.execute(
            "SELECT key, value_json FROM arc_state WHERE arc_id = ? AND key LIKE ?",
            (arc_id, _OUTPUT_KEY_PREFIX + "%"),
        ).fetchall()
    prefix_len = len(_OUTPUT_KEY_PREFIX)
    return {row["key"][prefix_len:]: json.loads(row["value_json"]) for row in rows}


def _step_role(step: dict) -> str:
    """The stable identifier for a template step.

    Prefers an explicit ``role`` field; falls back to ``name``. This keeps the
    API forward-compatible with templates that have not yet declared roles.
    """
    return step.get("role") or step.get("name") or ""


def find_sibling_arc_id(current_arc_id: int, sibling_role: str) -> Optional[int]:
    """Find a sibling arc (same parent) whose template step role matches.

    Resolution order for each candidate sibling:

    1. If the sibling arc has ``arcs.step_role == sibling_role``, match.
       (Preferred — populated by ``instantiate_template`` per D2 PR-α.)
    2. Else if the sibling's template step has ``role == sibling_role``, match.
       (Backfill path: arcs predating the column whose template still
       declares the role.)
    3. Else if the sibling's step ``name == sibling_role``, match.
    4. Else if the sibling arc's ``arcs.name == sibling_role``, match.

    Returns the sibling arc ID, or ``None`` if no sibling matches. If
    multiple match, returns the one with the lowest ``step_order`` (earliest
    sibling); ties broken by arc id.
    """
    if not sibling_role:
        return None

    with db_connection() as db:
        current = db.execute(
            "SELECT parent_id FROM arcs WHERE id = ?",
            (current_arc_id,),
        ).fetchone()
        if not current or current["parent_id"] is None:
            return None
        parent_id = current["parent_id"]

        siblings = db.execute(
            "SELECT id, name, template_id, step_order, step_role FROM arcs "
            "WHERE parent_id = ? AND id != ? ORDER BY step_order, id",
            (parent_id, current_arc_id),
        ).fetchall()

        # Pass 1: direct match on arcs.step_role column. This is the fast
        # path for arcs created post-D2 PR-α migration.
        for sib in siblings:
            if sib["step_role"] and sib["step_role"] == sibling_role:
                return sib["id"]

        # Cache template steps per template_id to avoid repeated lookups.
        template_steps_cache: dict[int, list[dict]] = {}

        def steps_for(template_id: Optional[int]) -> list[dict]:
            if template_id is None:
                return []
            if template_id not in template_steps_cache:
                row = db.execute(
                    "SELECT steps_json FROM workflow_templates WHERE id = ?",
                    (template_id,),
                ).fetchone()
                if not row:
                    template_steps_cache[template_id] = []
                else:
                    from .template_executor import _extract_steps_list
                    template_steps_cache[template_id] = _extract_steps_list(
                        json.loads(row["steps_json"])
                    )
            return template_steps_cache[template_id]

        # Pass 2: legacy template-steps lookup for arcs predating the
        # step_role column (their step_role IS NULL but template still
        # declares the role).
        for sib in siblings:
            if sib["step_role"]:
                continue  # already considered in pass 1
            steps = steps_for(sib["template_id"])
            step = next((s for s in steps if s.get("name") == sib["name"]), None)
            if step is not None and _step_role(step) == sibling_role:
                return sib["id"]

        # Pass 3: plain arcs.name match (for legacy callers / arcs without
        # a template step definition).
        for sib in siblings:
            if sib["name"] == sibling_role:
                return sib["id"]

    return None


def get_sibling_output(
    current_arc_id: int,
    sibling_role: str,
    output_name: str,
    default: Any = None,
) -> Any:
    """Read a named output from a sibling arc identified by its template role.

    Returns ``default`` if no sibling matches or the named output is not set.
    """
    sibling_id = find_sibling_arc_id(current_arc_id, sibling_role)
    if sibling_id is None:
        return default
    return get_arc_output(sibling_id, output_name, default=default)
