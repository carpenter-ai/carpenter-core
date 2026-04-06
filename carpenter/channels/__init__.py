"""Channel abstraction for Carpenter.

Unifies tool connectors (plugin IPC) and channel connectors (chat channels)
under a single lifecycle: start, stop, health_check.
"""

from .base import Connector, HealthStatus
from .registry import get_connector_registry, initialize_connector_registry
from .formatting import split_message

__all__ = [
    "Connector",
    "HealthStatus",
    "get_connector_registry",
    "initialize_connector_registry",
    "split_message",
]
