"""Chat API endpoint for Carpenter."""
import logging
import attrs
import cattrs
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..core.engine import event_bus, main_loop
from ..agent import conversation
from ..channels.channel import get_invocation_tracker
from ..channels.web_channel import WebChannelConnector

logger = logging.getLogger(__name__)

# Module-level web channel instance for the HTTP chat endpoint.
# This is a separate instance from the one auto-registered in the
# ConnectorRegistry.  That's fine: WebChannelConnector is stateless
# (send_message is a no-op, health_check is trivially true) and both
# instances share the same InvocationTracker singleton, so pending-task
# tracking and cancellation on shutdown work correctly.
_web_channel = WebChannelConnector()


def is_pending(conv_id: int) -> bool:
    """Check if a chat invocation is already running for this conversation."""
    return get_invocation_tracker().is_pending(conv_id)


@attrs.define
class ChatMessage:
    text: str
    user: str = "default"
    conversation_id: int | None = None


@attrs.define
class ChatResponse:
    event_id: int
    conversation_id: int | None = None
    response_text: str | None = None


async def handle_chat(request: Request) -> JSONResponse:
    """Accept a chat message, save it, and start AI processing in background.

    Returns 202 immediately with the conversation_id so the frontend
    doesn't block. The user message is persisted to the DB before
    returning, so the next HTMX poll picks it up. The AI response
    arrives asynchronously and is picked up by subsequent polls.
    """
    message = cattrs.structure(await request.json(), ChatMessage)

    # Record the event
    event_id = event_bus.record_event(
        event_type="chat.message",
        payload={"text": message.text, "user": message.user},
        source="chat",
    )

    # Wake the main loop (for any side effects)
    main_loop.wake_signal.set()

    # Validate conversation_id if explicitly provided
    if message.conversation_id is not None:
        conv = conversation.get_conversation(message.conversation_id)
        if conv is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"Conversation #{message.conversation_id} not found"},
            )

    # Route through web channel connector
    conv_id = await _web_channel.deliver_inbound(
        channel_user_id=message.user,
        text=message.text,
        conversation_id=message.conversation_id,
    )

    return JSONResponse(
        status_code=202,
        content={
            "event_id": event_id,
            "conversation_id": conv_id,
        },
    )


async def chat_pending(request: Request):
    """Check if an AI response is still being generated.

    Returns {"pending": true/false} so the UI can show a thinking indicator.
    """
    conversation_id = request.query_params.get("conversation_id")
    c = request.query_params.get("c")
    effective_id = int(conversation_id) if conversation_id else (int(c) if c else None)
    pending = effective_id is not None and get_invocation_tracker().is_pending(effective_id)
    return JSONResponse(content={"pending": pending})


async def get_chat_history(request: Request):
    """Get chat message history."""
    conversation_id_param = request.query_params.get("conversation_id")
    conversation_id = int(conversation_id_param) if conversation_id_param else None
    if conversation_id is None:
        conversation_id = conversation.get_or_create_conversation()

    messages = conversation.get_messages(conversation_id)
    return JSONResponse(content={
        "conversation_id": conversation_id,
        "messages": messages,
    })


routes = [
    Route("/api/chat", handle_chat, methods=["POST"]),
    Route("/api/chat/pending", chat_pending, methods=["GET"]),
    Route("/api/chat/history", get_chat_history, methods=["GET"]),
]
