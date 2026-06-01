"""Python step handlers for the reflection template.

Two Python-only steps ship in this package:

- :func:`handle_gather_activity` — pre-reflect step. Reads the reflected
  root arc's id from the parent arc's ``reflected_arc_id`` state (set
  by the subscription's ``initial_arc_state`` at arc creation), calls
  :func:`activity_gatherer.gather_from_arc`, and writes the result into
  the sibling ``reflect`` arc's goal. Then completes and propagates so
  the ``reflect`` step can be dispatched with its goal populated.

- :func:`handle_save_reflection` — post-reflect step. Reads the sibling
  ``reflect`` arc's ``_agent_response`` via
  :func:`arc_outputs.find_sibling_arc_id` (``role=analyze``), enqueues
  the KB write via :func:`.reflection_storage.save_reflection` keyed on
  the reflected arc id, and completes. Auto-action fan-out is owned by
  the ``dispatch-actions`` sibling step.

Both are registered in ``__init__.py`` via
:func:`register_handlers`. Neither is cadence-aware — reflection is now
triggered per root-arc completion.
"""

from __future__ import annotations

import logging

from carpenter import config
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine.arc_outputs import find_sibling_arc_id
from carpenter.core.workflows._arc_state import (
    get_arc_state as _get_arc_state,
)
from carpenter.db import db_connection, db_transaction
from carpenter.agent import conversation as conv_module
from carpenter.agent import model_resolver
from carpenter.core.workflows._arc_state import set_arc_state
from carpenter.prompts import load_prompt_template

from .proposed_action_parser import classify_action, parse_proposed_actions
from .reflection_storage import save_reflection

logger = logging.getLogger(__name__)


# (action_type, review_mode) → (template_name, prompt_name).
#
# ``review_mode`` is ``"auto"`` for clean reflections and ``"human"`` when
# the reflected arc (or any descendant) was non-trusted — the latter
# routes to the gated template variants, which add an ``await-approval``
# step gated on ``arc.manual_trigger`` ahead of ``execute-action``.
#
# Config/other route to KB per legacy ``_submit_other_action``-equivalent
# behavior (see ``reflection_action.py``: non-actionable types get
# recorded; the closest arc analogue is a KB entry documenting the
# proposed change).
_ACTION_TYPE_TO_TEMPLATE = {
    ("kb", "auto"): ("reflection-kb-action", "action-kb"),
    ("code", "auto"): ("reflection-code-action", "action-code"),
    ("config", "auto"): ("reflection-kb-action", "action-kb"),
    ("other", "auto"): ("reflection-kb-action", "action-kb"),
    ("kb", "human"): ("reflection-kb-action-gated", "action-kb"),
    ("code", "human"): ("reflection-code-action-gated", "action-code"),
    ("config", "human"): ("reflection-kb-action-gated", "action-kb"),
    ("other", "human"): ("reflection-kb-action-gated", "action-kb"),
}


async def handle_gather_activity(arc_id: int, arc_info: dict) -> None:
    """Populate the sibling ``reflect`` arc's goal with gathered data.

    The subscription's ``initial_arc_state`` stores
    ``reflected_arc_id`` on the parent. This handler reads it, runs
    :func:`activity_gatherer.gather_from_arc`, and writes the returned
    markdown into the sibling reflect arc's ``goal`` column (the reflect
    step runs an LLM agent and reads its goal from the arcs row).
    """
    from . import activity_gatherer

    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    parent_id = arc_info["parent_id"]
    reflected_arc_id = _get_arc_state(parent_id, "reflected_arc_id")
    if reflected_arc_id is None:
        logger.warning(
            "gather-activity arc %d: parent %d has no reflected_arc_id — "
            "the reflection subscription must set it via initial_arc_state",
            arc_id, parent_id,
        )
        gathered = "# Reflection — no arc specified\n"
    else:
        try:
            reflected_arc_id = int(reflected_arc_id)
        except (TypeError, ValueError):
            logger.warning(
                "gather-activity arc %d: reflected_arc_id=%r is not an int",
                arc_id, reflected_arc_id,
            )
            gathered = "# Reflection — bad reflected_arc_id\n"
        else:
            gathered = activity_gatherer.gather_from_arc(reflected_arc_id)

    reflect_arc_id = find_sibling_arc_id(arc_id, "analyze")
    if reflect_arc_id is not None:
        with db_transaction() as db:
            db.execute(
                "UPDATE arcs SET goal = ? WHERE id = ?",
                (gathered, reflect_arc_id),
            )
    else:
        logger.warning(
            "gather-activity arc %d: no sibling reflect arc (role=analyze)",
            arc_id,
        )

    arc_manager.update_status(arc_id, "completed")
    arc_manager.freeze_arc(arc_id)

    from carpenter.core.arcs.dispatch_handler import _propagate_completion
    _propagate_completion(arc_id)

    logger.info(
        "gather-activity arc %d completed: %d chars into reflect arc %s",
        arc_id, len(gathered), reflect_arc_id,
    )


async def handle_save_reflection(arc_id: int, arc_info: dict) -> None:
    """Save the reflection from its sibling reflect arc's AI output."""
    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    parent_id = arc_info["parent_id"]
    reflected_arc_id = _get_arc_state(parent_id, "reflected_arc_id")
    try:
        reflected_arc_id = int(reflected_arc_id) if reflected_arc_id is not None else None
    except (TypeError, ValueError):
        reflected_arc_id = None

    response_text = "(No reflection output)"
    reflect_arc_id = find_sibling_arc_id(arc_id, "analyze")
    if reflect_arc_id is not None:
        raw = _get_arc_state(reflect_arc_id, "_agent_response")
        if raw:
            response_text = raw
        else:
            logger.warning(
                "save-reflection arc %d: no _agent_response on reflect arc %d",
                arc_id, reflect_arc_id,
            )
    else:
        logger.warning(
            "save-reflection arc %d: no sibling reflect arc (role=analyze) "
            "under parent %d",
            arc_id, parent_id,
        )

    model = model_resolver.get_model_for_role("reflection")

    if reflected_arc_id is None:
        logger.warning(
            "save-reflection arc %d: no reflected_arc_id on parent %d — "
            "nothing to key the reflection on",
            arc_id, parent_id,
        )
        # Fall back to using the parent's id so the row still writes;
        # this path only trips in tests or misconfigured subscriptions.
        reflected_arc_id = parent_id

    save_reflection(reflected_arc_id, response_text, model=model)

    # Auto-action fan-out now lives in the ``dispatch-actions`` template
    # step (see :func:`handle_dispatch_actions`); spawning child arcs per
    # proposed action replaced the legacy ``reflection_actions`` SQL
    # writes that ``process_reflection_actions`` used to do here.

    arc_manager.update_status(arc_id, "completed")
    arc_manager.freeze_arc(arc_id)

    from carpenter.core.arcs.dispatch_handler import _propagate_completion
    _propagate_completion(arc_id)

    with db_connection() as db:
        conv_row = db.execute(
            "SELECT conversation_id FROM conversation_arcs WHERE arc_id = ?",
            (parent_id,),
        ).fetchone()
    if conv_row:
        conv_module.archive_conversation(conv_row["conversation_id"])

    logger.info(
        "save-reflection arc %d completed: KB write enqueued "
        "(reflected_arc_id=%s)",
        arc_id, reflected_arc_id,
    )


def _is_reflected_arc_tainted(reflected_arc_id: int | None) -> bool:
    """Return True if the reflected arc (or any descendant) is non-trusted.

    Stub preserving the safety property of legacy
    ``_check_reflection_tainted``. Legacy queried conversation_taint by
    title match — in the per-arc model, the equivalent signal is the arc's
    own ``integrity_level`` (and its descendants'). If any is not
    'trusted', the reflection drew from tainted inputs and action arcs
    spawned from it should be marked for human review.
    """
    if reflected_arc_id is None:
        return False
    try:
        with db_connection() as db:
            row = db.execute(
                "SELECT integrity_level FROM arcs WHERE id = ?",
                (reflected_arc_id,),
            ).fetchone()
            if row and row["integrity_level"] and row["integrity_level"] != "trusted":
                return True
            # Check descendants. Cheap recursive walk; reflection arcs are
            # rare and tree depth is bounded, so no CTE needed.
            descendant_rows = db.execute(
                "WITH RECURSIVE descendants(id) AS ("
                "  SELECT id FROM arcs WHERE parent_id = ? "
                "  UNION ALL "
                "  SELECT a.id FROM arcs a "
                "  JOIN descendants d ON a.parent_id = d.id"
                ") "
                "SELECT a.integrity_level FROM arcs a "
                "JOIN descendants d ON a.id = d.id "
                "WHERE a.integrity_level IS NOT NULL "
                "  AND a.integrity_level != 'trusted' "
                "LIMIT 1",
                (reflected_arc_id,),
            ).fetchall()
            return len(descendant_rows) > 0
    except Exception:
        logger.exception(
            "taint check failed for reflected arc %s; defaulting to untainted",
            reflected_arc_id,
        )
        return False


async def handle_dispatch_actions(arc_id: int, arc_info: dict) -> None:
    """Fan out reflection-proposed actions into one child arc per action.

    Logic:

    1. Locate the sibling reflect arc (``role=analyze``) and read its
       ``_agent_response``.
    2. Parse proposed actions out of the response text (uses
       :func:`.proposed_action_parser.parse_proposed_actions`).
    3. Truncate to ``reflection.max_actions_per_reflection`` (default 5).
    4. For each remaining action: classify, select a template, render the
       corresponding prompt with ``description=action_desc``, and spawn a
       new arc under the reflection parent arc with that prompt as goal.
    5. Record the spawned arc ids / action types on this arc's state so
       the flow is queryable.

    Taint propagation: if the reflected arc (or any descendant) is
    non-trusted, spawned action arcs are tagged with an arc_state flag
    ``_review_mode=human`` (kept for observability — lets you query which
    arcs were spawned tainted) and are instantiated from the *-gated
    template variants instead of the auto variants. The gated templates
    insert an ``await-approval`` step with
    ``activation_event: arc.manual_trigger`` before ``execute-action``,
    so the action arc's dispatch is blocked until an operator emits
    ``arc.manual_trigger``. Clean reflections use the auto variants
    (single-step ``execute-action``) unchanged.
    """
    from carpenter.core.engine import template_manager

    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    parent_id = arc_info["parent_id"]
    reflected_arc_id_raw = _get_arc_state(parent_id, "reflected_arc_id")
    try:
        reflected_arc_id = (
            int(reflected_arc_id_raw) if reflected_arc_id_raw is not None else None
        )
    except (TypeError, ValueError):
        reflected_arc_id = None

    # Fetch the reflect arc's AI output as the raw proposed-actions text.
    raw_response: str | None = None
    reflect_arc_id = find_sibling_arc_id(arc_id, "analyze")
    if reflect_arc_id is not None:
        raw_response = _get_arc_state(reflect_arc_id, "_agent_response")
    else:
        logger.warning(
            "dispatch-actions arc %d: no sibling reflect arc (role=analyze)",
            arc_id,
        )

    actions = parse_proposed_actions(raw_response)
    if not actions:
        logger.info(
            "dispatch-actions arc %d: no proposed actions in reflect output "
            "(reflect arc=%s) — no-op",
            arc_id, reflect_arc_id,
        )
        set_arc_state(arc_id, "_agent_response", {
            "spawned_arcs": [],
            "action_types": [],
            "total_proposed": 0,
            "truncated": 0,
        })
        arc_manager.update_status(arc_id, "completed")
        arc_manager.freeze_arc(arc_id)
        from carpenter.core.arcs.dispatch_handler import _propagate_completion
        _propagate_completion(arc_id)
        return

    cap = int(
        config.CONFIG.get("reflection", {}).get("max_actions_per_reflection", 5)
    )
    total_proposed = len(actions)
    truncated_count = max(0, total_proposed - cap)
    actions = actions[:cap]

    is_tainted = _is_reflected_arc_tainted(reflected_arc_id)
    review_mode = "human" if is_tainted else "auto"

    spawned_arcs: list[int] = []
    action_types: list[str] = []

    for i, action_desc in enumerate(actions):
        action_type = classify_action(action_desc)
        template_name, prompt_name = _ACTION_TYPE_TO_TEMPLATE.get(
            (action_type, review_mode),
            _ACTION_TYPE_TO_TEMPLATE[("other", review_mode)],
        )

        template = template_manager.get_template_by_name(template_name)
        if template is None:
            logger.warning(
                "dispatch-actions arc %d: template %r not found; skipping action %d",
                arc_id, template_name, i,
            )
            continue

        try:
            rendered_goal = load_prompt_template(
                prompt_name,
                context={"description": action_desc},
                subdirectory="reflections",
            )
        except FileNotFoundError:
            rendered_goal = action_desc

        try:
            # parent_id is the reflection root arc (non-template, mutable).
            child_arc_id = arc_manager.add_child(
                parent_id=parent_id,
                name=f"reflection-action-{i}",
                goal=rendered_goal,
                template_id=template["id"],
            )
            # Instantiate the template's single step as a grandchild.
            template_manager.instantiate_template(template["id"], child_arc_id)
        except Exception:
            logger.exception(
                "dispatch-actions arc %d: failed to spawn action %d (type=%s)",
                arc_id, i, action_type,
            )
            continue

        # Record the original action description + type + taint gating on
        # the spawned arc so tooling / follow-up gating can inspect.
        set_arc_state(child_arc_id, "action_type", action_type)
        set_arc_state(child_arc_id, "action_description", action_desc)
        if is_tainted:
            set_arc_state(child_arc_id, "_review_mode", "human")

        spawned_arcs.append(child_arc_id)
        action_types.append(action_type)

    # Record dispatch outcome on this arc for queryability.
    set_arc_state(arc_id, "_agent_response", {
        "spawned_arcs": spawned_arcs,
        "action_types": action_types,
        "total_proposed": total_proposed,
        "truncated": truncated_count,
        "tainted": is_tainted,
    })

    arc_manager.update_status(arc_id, "completed")
    arc_manager.freeze_arc(arc_id)

    from carpenter.core.arcs.dispatch_handler import _propagate_completion
    _propagate_completion(arc_id)

    logger.info(
        "dispatch-actions arc %d: spawned %d child arcs from %d proposed "
        "actions (%d truncated by cap=%d, tainted=%s)",
        arc_id, len(spawned_arcs), total_proposed, truncated_count, cap,
        is_tainted,
    )
