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

RESULT_PREVIEW_MAX = 4000


def _build_resource_preview(arc_id: int, arc_name: str) -> str | None:
    """Build a completion message from the arc's ``_primary_resource_id``.

    Returns the fully-formatted message string, or ``None`` if the arc
    has no ``_primary_resource_id`` (caller should fall back to the
    ``_agent_response`` path).  When the Resource exists but is not
    trusted (pending/rejected/missing), returns a hybrid message that
    still includes the ``_agent_response`` body plus a note about the
    pending Resource.
    """
    primary_id = get_arc_state(arc_id, "_primary_resource_id", None)
    if primary_id is None:
        return None

    # Import here to avoid widening module-import surface on cold start.
    from ..resources import (
        get_resource,
        is_trusted,
        read_resource_content,
    )

    row = get_resource(primary_id)
    if row is None:
        # Resource was cleaned up or the id is stale — fall back.
        logger.warning(
            "arc.chat_notify: arc %d has _primary_resource_id=%s but "
            "Resource not found; falling back to _agent_response",
            arc_id, primary_id,
        )
        return None

    content_type = row.get("content_type") or "unknown"
    total_bytes = row.get("byte_size")

    if is_trusted(primary_id) and row.get("deleted_at") is None:
        try:
            preview = read_resource_content(
                primary_id, 0, RESULT_PREVIEW_MAX, caller_arc_id=None,
            )
        except (FileNotFoundError, ValueError) as e:
            logger.warning(
                "arc.chat_notify: arc %d primary Resource %d read failed: %s; "
                "falling back to _agent_response",
                arc_id, primary_id, e,
            )
            return None
        total_bytes_str = (
            str(total_bytes) if total_bytes is not None else "unknown"
        )
        msg = (
            f'[Arc "{arc_name}" completed: {preview}]\n'
            f"[Primary resource: #{primary_id} ({content_type}, trusted, "
            f"{total_bytes_str} bytes total). Use read_resource("
            f"{primary_id}) for full content.]\n[Be concise.]"
        )
        return msg

    # Untrusted / pending / rejected / deleted — surface the fact and
    # still show the _agent_response body so the user can see *something*.
    verdict = row.get("template_verdict")
    if row.get("deleted_at") is not None:
        verdict_descr = "deleted"
    else:
        verdict_descr = verdict if verdict else "none (raw ingest)"
    note = (
        f"[Note: this arc produced a Resource but it was not approved "
        f"(verdict={verdict_descr}). Raw summary from _agent_response "
        "shown below.]"
    )
    body_response = get_arc_state(arc_id, "_agent_response", "") or ""
    if not body_response:
        children = arc_manager.get_children(arc_id) or []
        for child in reversed(children):
            child_resp = get_arc_state(
                child["id"], "_agent_response", ""
            ) or ""
            if child_resp:
                body_response = child_resp
                break
    full_length = len(body_response)
    was_truncated = full_length > RESULT_PREVIEW_MAX
    if was_truncated:
        body_response = body_response[:RESULT_PREVIEW_MAX] + "..."
    if body_response:
        msg = f'{note}\n[Arc "{arc_name}" completed: {body_response}]'
        if was_truncated:
            msg += (
                f"\n[Truncated — full result is {full_length} chars. "
                f"Use read_arc_result(arc_id={arc_id}) for complete output.]"
            )
        msg += "\n[Be concise.]"
    else:
        msg = f'{note}\n[Arc "{arc_name}" completed.]'
    return msg


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
        # Prefer a trusted primary Resource preview when the arc set one.
        resource_msg = _build_resource_preview(arc_id, name)
        if resource_msg is not None:
            msg = resource_msg
        else:
            result = get_arc_state(arc_id, "_agent_response", "") or ""
            # If root arc has no response, check children (agent response is
            # stored on the child arc that actually ran the agent)
            if not result:
                children = arc_manager.get_children(arc_id) or []
                # Iterate in reverse step_order so the JUDGE/REVIEWER
                # response (the most refined summary) is preferred over
                # the EXECUTOR's.
                for child in reversed(children):
                    child_resp = get_arc_state(
                        child["id"], "_agent_response", ""
                    ) or ""
                    if child_resp:
                        result = child_resp
                        break
            full_length = len(result)
            was_truncated = full_length > RESULT_PREVIEW_MAX
            if was_truncated:
                result = result[:RESULT_PREVIEW_MAX] + "..."
            if result:
                msg = f'[Arc "{name}" completed: {result}]'
                if was_truncated:
                    msg += (
                        f"\n[Truncated — full result is {full_length} "
                        f"chars. Use read_arc_result(arc_id={arc_id}) "
                        "for complete output.]"
                    )
                msg += "\n[Be concise.]"
            else:
                msg = f'[Arc "{name}" completed.]'
    else:
        msg = f'[Arc "{name}" failed.]'

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
