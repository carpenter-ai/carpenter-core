"""Handler for arc completion/failure → chat conversation notification.

When a root arc completes or fails, this handler injects a *hidden*
system message into the originating conversation and re-invokes the
chat agent so it can relay the result to the user.  The hidden message
is included in the LLM context but not rendered in the chat UI.
"""

import logging

from ..arcs import manager as arc_manager
from ..arcs.dispatch_handler import _find_arc_conversation
from ..workflows._arc_state import get_arc_state
from ...agent import conversation, invocation
from ... import thread_pools

logger = logging.getLogger(__name__)

RESULT_PREVIEW_MAX = 500


async def handle_arc_chat_notify(work_id: int, payload: dict) -> None:
    """Handle an ``arc.chat_notify`` work item.

    Looks up the completed/failed arc, finds (or creates) the linked
    conversation, injects a system message with the result preview,
    and invokes the chat agent so it can relay the information to the user.
    """
    arc_id = payload["arc_id"]

    arc = arc_manager.get_arc(arc_id)
    if not arc:
        logger.warning("arc.chat_notify: arc %d not found, skipping", arc_id)
        return

    # Silent arcs skip notification — unless they failed
    is_silent = get_arc_state(arc_id, "_silent", False)
    if is_silent and arc["status"] != "failed":
        logger.debug("arc.chat_notify: arc %d is silent, skipping", arc_id)
        return

    # Find the originating conversation
    conv_id = _find_arc_conversation(arc_id)
    if conv_id:
        conv = conversation.get_conversation(conv_id)
        if conv and conv.get("archived"):
            conv_id = None

    if not conv_id:
        conv_id = conversation.get_last_conversation()

    if not conv_id:
        conv_id = conversation.get_or_create_conversation()

    # Build notification message
    name = arc.get("name") or f"#{arc_id}"
    status = arc["status"]

    if status == "completed":
        result = get_arc_state(arc_id, "_agent_response", "") or ""
        # If root arc has no response, check children (agent response is
        # stored on the child arc that actually ran the agent)
        if not result:
            children = arc_manager.get_children(arc_id) or []
            for child in children:
                child_resp = get_arc_state(child["id"], "_agent_response", "") or ""
                if child_resp:
                    result = child_resp
                    break
        if len(result) > RESULT_PREVIEW_MAX:
            result = result[:RESULT_PREVIEW_MAX] + "..."
        if result:
            msg = (
                f'[System notification: Arc "{name}" completed. Result: {result}]\n'
                f"[Relay the result to the user concisely. For simple factual queries, "
                f"give a brief summary — not a lengthy formatted response.]"
            )
        else:
            msg = f'[System notification: Arc "{name}" completed.]'
    else:
        msg = f'[System notification: Arc "{name}" failed.]'

    # Inject system message as hidden — included in LLM context but
    # not rendered in the chat UI.  The chat agent will relay the
    # information to the user in its own response.
    conversation.add_message(conv_id, "system", msg, arc_id=arc_id, hidden=True)

    await thread_pools.run_in_work_pool(
        invocation.invoke_for_chat,
        msg,
        conversation_id=conv_id,
        _message_already_saved=True,
        _system_triggered=True,
    )
    logger.info(
        "arc.chat_notify: notified conversation %d about arc %d (%s)",
        conv_id, arc_id, status,
    )


def register_handlers(register_fn) -> None:
    """Register arc chat notification handler with the main loop."""
    register_fn("arc.chat_notify", handle_arc_chat_notify)
