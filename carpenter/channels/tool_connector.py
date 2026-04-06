"""File-watch tool connector — wraps FileWatchTransport in the Connector ABC."""

import logging

from .base import Connector, HealthStatus
from .transports.file_watch import FileWatchTransport

logger = logging.getLogger(__name__)


class FileWatchToolConnector(Connector):
    """Tool connector using file-watch IPC.

    Wraps a FileWatchTransport behind the unified Connector lifecycle.
    """

    kind = "tool"

    def __init__(self, name: str, connector_config: dict):
        self.name = name
        self.enabled = connector_config.get("enabled", False)
        self._config = connector_config

        # Build transport config from connector config
        transport_config = {
            "shared_folder": connector_config.get("shared_folder", ""),
            "timeout_seconds": connector_config.get("timeout_seconds", 600),
        }

        self.transport = FileWatchTransport(name, transport_config) if self.enabled else None

    async def start(self, config: dict) -> None:
        """Verify the shared folder exists."""
        if self.transport and self.transport.shared_folder:
            self.transport.shared_folder.mkdir(parents=True, exist_ok=True)

    async def stop(self) -> None:
        """No-op — file-watch transport has no persistent connections."""

    async def health_check(self) -> HealthStatus:
        """Delegate health check to the transport."""
        if not self.transport:
            return HealthStatus(healthy=False, detail="transport not configured")

        health = self.transport.check_health()
        return HealthStatus(
            healthy=health.get("healthy", False),
            detail=f"age={health.get('age_seconds')}s" if health.get("age_seconds") is not None else "no heartbeat",
        )
