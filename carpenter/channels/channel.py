"""Channel connector base class and InvocationTracker.

Channel connectors bridge external chat platforms (web, Telegram, Signal,
WhatsApp) to Carpenter conversations. They handle identity resolution,
conversation routing, message persistence, and AI invocation.
"""

import asyncio
import logging
from typing import Optional

from .base import Connector, HealthStatus

logger = logging.getLogger(__name__)


class InvocationTracker:
    """Per-conversation serialization of AI invocations.

    Ensures only one AI invocation runs per conversation at a time.
    Shared across all channel connectors.
    """

    def __init__(self):
        self._pending: dict[int, asyncio.Task] = {}

    def is_pending(self, conv_id: int) -> bool:
        """Check if an invocation is running for this conversation."""
        return conv_id in self._pending

    def track(self, conv_id: int, task: asyncio.Task) -> None:
        """Register an invocation task for a conversation.

        Adds a done callback to auto-remove when the task completes.
        """
        self._pending[conv_id] = task
        task.add_done_callback(lambda t: self._pending.pop(conv_id, None))

    def cancel_all(self) -> None:
        """Cancel all pending invocation tasks."""
        for task in self._pending.values():
            task.cancel()
        self._pending.clear()

    def clear(self) -> None:
        """Clear all tracked tasks without cancelling (for tests)."""
        self._pending.clear()


# Module-level singleton
_tracker: Optional[InvocationTracker] = None


def get_invocation_tracker() -> InvocationTracker:
    """Get or create the global InvocationTracker singleton."""
    global _tracker
    if _tracker is None:
        _tracker = InvocationTracker()
    return _tracker


class ChannelConnector(Connector):
    """Base class for chat channel connectors.

    Subclasses implement send_message() for outbound delivery.
    The inbound path (deliver_inbound → AI invocation → send_message)
    is shared across all channel types.
    """

    kind = "channel"
    channel_type: str = ""

    async def send_message(self, conversation_id: int, text: str,
                           metadata: dict | None = None) -> bool:
        """Send a message to the external channel.

        Override in subclasses. Default is no-op (e.g., web channel
        relies on polling instead of push).
        """
        return True

    async def deliver_inbound(self, channel_user_id: str, text: str,
                              display_name: str | None = None,
                              metadata: dict | None = None,
                              conversation_id: int | None = None) -> int:
        """Process an inbound message from an external channel.

        Steps:
        1. Identity resolution (channel_bindings lookup/create)
        2. Conversation resolution (get_or_create scoped to channel+user)
        3. Message persistence
        4. AI invocation (async, tracked per-conversation)

        Args:
            channel_user_id: User ID on the external platform.
            text: Message text.
            display_name: Optional display name for the user.
            metadata: Optional per-message metadata.
            conversation_id: Explicit conversation ID (e.g., web channel).

        Returns:
            The conversation_id used.
        """
        from ..agent import conversation, invocation
        from ..db import get_db

        # 1. Identity resolution
        conv_id = conversation_id
        if conv_id is None:
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT conversation_id FROM channel_bindings "
                    "WHERE channel_type = ? AND channel_user_id = ?",
                    (self.channel_type, channel_user_id),
                ).fetchone()

                if row and row["conversation_id"]:
                    # Check conversation exists and is not archived
                    existing = conversation.get_conversation(row["conversation_id"])
                    if existing:
                        conv_id = row["conversation_id"]

                if conv_id is None:
                    conv_id = conversation.get_or_create_conversation()

                # Upsert channel binding
                conn.execute(
                    "INSERT INTO channel_bindings (channel_type, channel_user_id, display_name, conversation_id) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(channel_type, channel_user_id) "
                    "DO UPDATE SET conversation_id = ?, display_name = COALESCE(?, display_name)",
                    (self.channel_type, channel_user_id, display_name, conv_id,
                     conv_id, display_name),
                )
                conn.commit()
            finally:
                conn.close()

        # 2. Message persistence
        conversation.add_message(conv_id, "user", text)

        # 3. AI invocation (per-conversation serialization)
        tracker = get_invocation_tracker()
        if not tracker.is_pending(conv_id):
            task = asyncio.create_task(
                self._run_and_respond(text, conv_id)
            )
            tracker.track(conv_id, task)

        return conv_id

    async def _run_and_respond(self, user_message: str, conv_id: int) -> None:
        """Run AI invocation and deliver the response via send_message."""
        from ..agent import invocation, conversation

        try:
            await asyncio.to_thread(
                invocation.invoke_for_chat,
                user_message,
                conversation_id=conv_id,
                _message_already_saved=True,
            )

            # Get the latest assistant message to send back
            messages = conversation.get_messages(conv_id)
            assistant_msgs = [m for m in messages if m["role"] == "assistant"]
            if assistant_msgs:
                latest = assistant_msgs[-1]
                from .formatting import format_for_channel
                formatted = format_for_channel(latest["content"], self.channel_type)
                await self.send_message(conv_id, formatted)

        except Exception:
            logger.exception("Channel invocation failed for conv %s", conv_id)
            try:
                from ..agent import conversation as conv_mod
                conv_mod.add_message(
                    conv_id, "assistant",
                    "Sorry, an error occurred while processing your message.",
                )
            except Exception:
                logger.exception("Failed to save error message")
