"""Web channel connector — bridges the HTMX chat UI.

The web channel is a special case: it doesn't push messages to the client.
Instead, the HTMX frontend polls for new messages. So send_message() is
a no-op and all the work happens in deliver_inbound() (via ChannelConnector).
"""

import logging

from .base import HealthStatus
from .channel import ChannelConnector, get_invocation_tracker

logger = logging.getLogger(__name__)


class WebChannelConnector(ChannelConnector):
    """Channel connector for the built-in web UI.

    The web UI polls for messages, so send_message is a no-op.
    This connector is auto-registered by the connector registry
    if no "web" connector is in the config.
    """

    channel_type = "web"

    def __init__(self, name: str = "web", connector_config: dict | None = None):
        self.name = name
        self.enabled = True
        self._config = connector_config or {}

    async def start(self, config: dict) -> None:
        """No-op — web UI requires no external connections."""

    async def stop(self) -> None:
        """Cancel any pending invocations on shutdown."""
        get_invocation_tracker().cancel_all()

    async def health_check(self) -> HealthStatus:
        """Web channel is always healthy when the server is running."""
        return HealthStatus(healthy=True, detail="web")

    async def send_message(self, conversation_id: int, text: str,
                           metadata: dict | None = None) -> bool:
        """No-op — web clients poll for messages."""
        return True
