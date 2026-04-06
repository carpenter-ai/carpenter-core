"""Connector ABC and HealthStatus dataclass."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class HealthStatus:
    """Health check result for a connector."""

    healthy: bool
    detail: str = ""
    last_seen: datetime | None = None


class Connector(ABC):
    """Base class for all connectors (tool and channel).

    A connector wraps an external service (tool IPC, chat channel, etc.)
    behind a uniform lifecycle: start, stop, health_check.
    """

    name: str
    kind: str       # "tool" | "channel"
    enabled: bool

    @abstractmethod
    async def start(self, config: dict) -> None:
        """Start the connector (verify resources, open connections)."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the connector (release resources)."""

    @abstractmethod
    async def health_check(self) -> HealthStatus:
        """Return current health status."""
