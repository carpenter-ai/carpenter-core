"""Base classes for Carpenter triggers.

Triggers are event sources that emit events into the event pipeline.
Users subclass these to create custom trigger types.

Three base classes:
- Trigger: base for all triggers (start/stop lifecycle)
- PollableTrigger: checked each heartbeat cycle (check → emit)
- EndpointTrigger: exposes an HTTP endpoint (handle_request → emit)
"""

from abc import ABC, abstractmethod
import logging

logger = logging.getLogger(__name__)


class Trigger(ABC):
    """Base class for all triggers.

    Subclass this to create custom trigger types. Each trigger type must
    define a unique ``trigger_type()`` string (e.g., "timer", "counter").

    Triggers emit events via ``self.emit()``, which records the event
    in the event bus with an optional idempotency key.
    """

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config

    @classmethod
    @abstractmethod
    def trigger_type(cls) -> str:
        """Return the trigger type identifier (e.g., 'timer', 'counter')."""
        ...

    def emit(
        self,
        event_type: str,
        payload: dict | None = None,
        idempotency_key: str | None = None,
        priority: int = 0,
    ) -> int | None:
        """Emit an event into the event bus.

        Args:
            event_type: The event type string.
            payload: Optional event payload dict.
            idempotency_key: Optional key for dedup (INSERT OR IGNORE).
            priority: Event priority (higher = processed first).

        Returns:
            Event ID, or None if duplicate (idempotency_key matched).
        """
        from .. import event_bus

        payload = payload or {}
        payload["_trigger"] = self.name
        payload["_trigger_type"] = self.trigger_type()

        event_id = event_bus.record_event(
            event_type=event_type,
            payload=payload,
            source=f"trigger:{self.name}",
            priority=priority,
            idempotency_key=idempotency_key,
        )
        if event_id is not None:
            logger.debug(
                "Trigger %s emitted %s (event_id=%d)",
                self.name, event_type, event_id,
            )
        return event_id

    def start(self) -> None:
        """Called once at startup. Override for initialization logic."""

    def stop(self) -> None:
        """Called on shutdown. Override for cleanup logic."""


class PollableTrigger(Trigger):
    """Trigger that is checked each heartbeat cycle.

    Subclass and implement ``check()`` — call ``self.emit()`` when
    conditions are met.
    """

    @abstractmethod
    def check(self) -> None:
        """Called each heartbeat. Check conditions and emit events."""
        ...


class EndpointTrigger(Trigger):
    """Trigger that exposes an HTTP endpoint.

    The platform registers the route at startup. When a request arrives,
    ``handle_request()`` is called — parse the payload and call
    ``self.emit()`` to inject into the event pipeline.
    """

    @property
    @abstractmethod
    def path(self) -> str:
        """HTTP path for this trigger (e.g., '/triggers/forgejo')."""
        ...

    @abstractmethod
    async def handle_request(self, request) -> dict:
        """Handle an incoming HTTP request.

        Parse the request, call self.emit() with structured event data,
        and return a response dict (will be JSON-encoded).

        Args:
            request: Starlette Request object.

        Returns:
            Dict to be returned as JSON response.
        """
        ...
