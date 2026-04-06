"""Handler for reflection template workflow.

Replaces the legacy inline reflection handler with a template-based approach.
A single ``reflection`` YAML template defines the arc structure, capabilities,
and model policy. This handler:

1. Creates a parent arc and conversation for the reflection
2. Instantiates the reflection template as child arcs
3. Populates the ``reflect`` arc's goal with gathered activity data
4. Dispatches the reflect arc for AI processing
5. The ``save-reflection`` step (Python-only, intercepted in dispatch_handler)
   reads the AI output and saves the reflection

All cadences (daily/weekly/monthly) use the same template; cadence-specific
behavior (gather function, period duration) is handled here.
"""

import logging
from datetime import datetime, timedelta, timezone

from ... import config
from ...db import get_db, db_connection, db_transaction
from ._arc_state import get_arc_state as _get_arc_state, set_arc_state as _set_arc_state
from ...agent import conversation as conv_module
from ...agent.reflection import (
    gather_daily_data,
    gather_monthly_data,
    gather_weekly_data,
    save_reflection,
    should_reflect,
)
from ...agent import model_resolver
from ..arcs import manager as arc_manager
from ..engine import template_manager, work_queue

logger = logging.getLogger(__name__)


# ── Main handler ─────────────────────────────────────────────────────────

async def handle_reflection_trigger(work_id: int, payload: dict):
    """Handle a reflection.trigger event.

    Creates a parent arc, instantiates the reflection template, populates
    the reflect step's goal with gathered data, and dispatches it.
    """
    event_payload = payload.get("event_payload", {})
    cadence = event_payload.get("cadence", "daily")

    now = datetime.now(timezone.utc)
    if cadence == "daily":
        period_start = (now - timedelta(days=1)).isoformat()
    elif cadence == "weekly":
        period_start = (now - timedelta(days=7)).isoformat()
    elif cadence == "monthly":
        period_start = (now - timedelta(days=30)).isoformat()
    else:
        logger.warning("Unknown reflection cadence: %s", cadence)
        return

    period_end = now.isoformat()

    # Check activity threshold
    if not should_reflect(cadence):
        save_reflection(
            cadence, period_start, period_end,
            "Quiet period, minimal activity.",
        )
        logger.info("Skipping %s reflection -- below activity threshold", cadence)
        return

    # Gather data based on cadence
    gather_fn = {
        "daily": gather_daily_data,
        "weekly": gather_weekly_data,
        "monthly": gather_monthly_data,
    }[cadence]
    gathered_data = gather_fn()

    # Load the reflection template
    template = template_manager.get_template_by_name("reflection")
    if template is None:
        raise RuntimeError(
            "Reflection template not found. The 'reflection' YAML template "
            "must be loaded before reflection triggers can be handled. "
            "Ensure templates are loaded during startup."
        )

    # Create parent arc
    parent_arc_id = arc_manager.create_arc(
        name=f"{cadence}-reflection",
        goal=f"{cadence.title()} reflection",
        agent_type="PLANNER",
        _allow_tainted=True,
    )

    # Create and link a conversation for the parent arc
    date_str = now.strftime("%Y-%m-%d")
    conv_id = conv_module.create_conversation()
    conv_module.set_conversation_title(
        conv_id, f"[{cadence.title()} Reflection] {date_str}"
    )
    conv_module.link_arc_to_conversation(conv_id, parent_arc_id)

    # Instantiate template under the parent arc
    arc_ids = template_manager.instantiate_template(template["id"], parent_arc_id)

    # The first arc is the reflect step; update its goal with gathered data
    reflect_arc_id = arc_ids[0]
    with db_transaction() as db:
        db.execute(
            "UPDATE arcs SET goal = ? WHERE id = ?",
            (gathered_data, reflect_arc_id),
        )

    # Link reflect arc to the conversation too
    conv_module.link_arc_to_conversation(conv_id, reflect_arc_id)

    # Store metadata in parent arc_state
    _set_arc_state(parent_arc_id, "cadence", cadence)
    _set_arc_state(parent_arc_id, "period_start", period_start)
    _set_arc_state(parent_arc_id, "period_end", period_end)

    # Enqueue the reflect arc for dispatch
    work_queue.enqueue(
        "arc.dispatch",
        {"arc_id": reflect_arc_id},
        idempotency_key=f"refl-dispatch-{reflect_arc_id}",
        max_retries=work_queue.SINGLE_ATTEMPT,
    )

    logger.info(
        "Reflection trigger (%s): parent arc %d, reflect arc %d enqueued",
        cadence, parent_arc_id, reflect_arc_id,
    )


# ── Save-reflection intercept ───────────────────────────────────────────

def is_reflection_save_step(arc_info: dict) -> bool:
    """Check if an arc is the save-reflection step of a reflection template.

    Returns True if:
    - Arc name is 'save-reflection'
    - Arc was created from a template (from_template is True)
    - Parent arc exists and its name ends with '-reflection'
    """
    if arc_info.get("name") != "save-reflection":
        return False
    if not arc_info.get("from_template"):
        return False
    parent_id = arc_info.get("parent_id")
    if parent_id is None:
        return False
    parent = arc_manager.get_arc(parent_id)
    if parent is None:
        return False
    parent_name = parent.get("name", "")
    return parent_name.endswith("-reflection")


async def handle_save_reflection(arc_id: int, arc_info: dict):
    """Python-only handler for the save-reflection step.

    Reads the AI output from the sibling reflect arc's arc_state,
    saves the reflection, runs post-processing, and completes the arc.
    """
    # Activate the arc (pending -> active)
    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    parent_id = arc_info["parent_id"]

    # Read metadata from parent arc_state
    cadence = _get_arc_state(parent_id, "cadence", "daily")
    period_start = _get_arc_state(parent_id, "period_start", "")
    period_end = _get_arc_state(parent_id, "period_end", "")

    # Find sibling reflect arc (same parent, name="reflect")
    with db_connection() as db:
        reflect_row = db.execute(
            "SELECT id FROM arcs WHERE parent_id = ? AND name = ?",
            (parent_id, "reflect"),
        ).fetchone()

    response_text = "(No reflection output)"
    if reflect_row:
        reflect_arc_id = reflect_row["id"]
        raw = _get_arc_state(reflect_arc_id, "_agent_response")
        if raw:
            response_text = raw
        else:
            logger.warning(
                "save-reflection arc %d: no _agent_response found on reflect arc %d",
                arc_id, reflect_arc_id,
            )
    else:
        logger.warning(
            "save-reflection arc %d: no sibling reflect arc found under parent %d",
            arc_id, parent_id,
        )

    # Resolve model name for recording
    model = model_resolver.get_model_for_role(f"reflection_{cadence}")

    # Save the reflection
    reflection_id = save_reflection(
        cadence, period_start, period_end, response_text,
        model=model,
    )

    # Process auto-actions if config says so
    if config.CONFIG.get("reflection", {}).get("auto_action", False):
        try:
            from ...agent import reflection_action
            reflection_action.process_reflection_actions(reflection_id)
        except Exception:
            logger.exception(
                "Reflection auto-action processing failed for reflection %d",
                reflection_id,
            )

    # Update model speed measurements if daily cadence
    if cadence == "daily":
        try:
            from ...core.models.speed_tracker import update_registry_speeds
            updated = update_registry_speeds()
            if updated:
                logger.info("Daily reflection: updated speed for %d model(s)", updated)
        except (ImportError, OSError, ValueError) as _exc:
            logger.exception("Failed to update model speed measurements")

    # Complete the arc
    arc_manager.update_status(arc_id, "completed")
    arc_manager.freeze_arc(arc_id)

    # Propagate completion (freeze parent, enqueue siblings)
    from ..arcs.dispatch_handler import _propagate_completion
    _propagate_completion(arc_id)

    # Archive the conversation linked to the parent arc
    with db_connection() as db:
        conv_row = db.execute(
            "SELECT conversation_id FROM conversation_arcs WHERE arc_id = ?",
            (parent_id,),
        ).fetchone()

    if conv_row:
        conv_module.archive_conversation(conv_row["conversation_id"])

    logger.info(
        "save-reflection arc %d completed: %s reflection saved (id=%d)",
        arc_id, cadence, reflection_id,
    )


# ── Registration ─────────────────────────────────────────────────────────

def register_handlers(register_fn):
    """Register the reflection trigger handler."""
    register_fn("reflection.trigger", handle_reflection_trigger)
