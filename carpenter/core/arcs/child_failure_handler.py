"""Handler for child failure re-invocation.

When a child arc fails and the parent's escalation policy is "replan"
(the default), a work item is enqueued. This handler processes it by
transitioning the parent back to active, gathering failure context,
and invoking the chat agent so the parent can create alternative children
or propagate failure.

Follows the same pattern as reflection_template_handler.py.
"""

import asyncio
import json
import logging

from ... import config
from ...db import get_db

logger = logging.getLogger(__name__)


async def handle_child_failed(work_id: int, payload: dict):
    """Handle an arc.child_failed work item.

    1. Verify parent still in waiting status
    2. Transition parent: waiting → active
    3. Gather failure context
    4. Create dedicated conversation
    5. Invoke chat agent with failure context
    6. Archive conversation
    """
    parent_id = payload.get("parent_id")
    failed_child_id = payload.get("failed_child_id")
    failed_child_name = payload.get("failed_child_name", "")
    failed_child_goal = payload.get("failed_child_goal", "")

    if parent_id is None:
        logger.warning("arc.child_failed work item missing parent_id")
        return

    from . import manager as arc_manager

    # 1. Verify parent still waiting
    parent = arc_manager.get_arc(parent_id)
    if parent is None:
        logger.warning("Parent arc %d not found for child failure handling", parent_id)
        return
    if parent["status"] != "waiting":
        logger.info(
            "Parent arc %d no longer waiting (status=%s), skipping re-invocation",
            parent_id, parent["status"],
        )
        return

    # 2. Transition parent: waiting → active
    try:
        arc_manager.update_status(parent_id, "active")
    except ValueError:
        logger.warning("Could not transition parent %d to active", parent_id)
        return

    # 3. Gather failure context
    failure_context = _gather_failure_context(parent_id, failed_child_id)

    # 4. Create dedicated conversation
    from ...agent import conversation as conv_module
    conv_id = conv_module.create_conversation()
    conv_module.set_conversation_title(
        conv_id, f"[Child Failure] Arc #{parent_id}: child #{failed_child_id} failed"
    )

    # 5. Build message with failure info and options
    message = (
        f"## Child Arc Failed\n\n"
        f"Your child arc **#{failed_child_id}** ({failed_child_name}) has failed.\n\n"
        f"**Child goal:** {failed_child_goal}\n\n"
        f"**Your arc:** #{parent_id} — {parent['name']}\n"
        f"**Your goal:** {parent['goal']}\n\n"
        f"{failure_context}\n\n"
        f"## Options\n\n"
        f"You can:\n"
        f"1. Create alternative child arcs to achieve the same goal differently\n"
        f"2. Mark yourself as failed (use `arc.update_status` with status='failed') "
        f"to escalate the failure to your parent\n"
        f"3. Create new children to work around the failure\n\n"
        f"Analyze the failure and take the most appropriate action."
    )

    conv_module.add_message(conv_id, "user", message)

    # 6. Invoke chat agent
    from ...agent import invocation
    try:
        await asyncio.to_thread(
            invocation.invoke_for_chat, message,
            conversation_id=conv_id,
            _message_already_saved=True,
            _system_triggered=True,
        )
    except Exception:  # broad catch: AI agent invocation may raise anything
        logger.exception(
            "Child failure re-invocation failed for parent %d", parent_id,
        )

    # Archive conversation
    conv_module.archive_conversation(conv_id)
    logger.info(
        "Completed child failure handling for parent %d (failed child %d)",
        parent_id, failed_child_id,
    )


def _gather_failure_context(parent_id: int, failed_child_id: int) -> str:
    """Gather context about the failure for the parent agent."""
    from . import manager as arc_manager

    parts = []

    # Failed child details
    failed_child = arc_manager.get_arc(failed_child_id)
    if failed_child:
        parts.append("### Failed Child Details")
        parts.append(f"- **Name:** {failed_child['name']}")
        parts.append(f"- **Goal:** {failed_child['goal']}")
        parts.append(f"- **Agent type:** {failed_child['agent_type']}")

    # Error history from failed child
    history = arc_manager.get_history(failed_child_id)
    error_entries = [
        h for h in history
        if h["entry_type"] in ("error", "status_changed")
    ]
    if error_entries:
        parts.append("\n### Error History")
        for entry in error_entries[-5:]:  # Last 5 entries
            content = json.loads(entry["content_json"])
            parts.append(f"- [{entry['entry_type']}] {json.dumps(content)}")

    # Sibling summary
    siblings = arc_manager.get_children(parent_id)
    if siblings:
        parts.append("\n### Sibling Status")
        for sib in siblings:
            marker = " **[FAILED]**" if sib["id"] == failed_child_id else ""
            parts.append(f"- #{sib['id']} {sib['name']} — {sib['status']}{marker}")

    return "\n".join(parts) if parts else "(No additional context available)"


def register_handlers(register_fn):
    """Register the child failure handler with the main loop."""
    register_fn("arc.child_failed", handle_child_failed)
