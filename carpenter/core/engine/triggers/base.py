"""Base classes for Carpenter triggers.

Triggers are event sources that emit events into the event pipeline.
Users subclass these to create custom trigger types.

Three base classes:
- Trigger: base for all triggers (start/stop lifecycle)
- PollableTrigger: checked each heartbeat cycle (check → emit)
- EndpointTrigger: exposes an HTTP endpoint (handle_request → emit)

D24 Phase 3a (PR-B) extensions
------------------------------
Capability-package-contributed triggers receive two extra kwargs at
construction:

* ``source_package``: the manifest name of the package that shipped the
  trigger.  Stamped on every emitted event payload (``_source_package``)
  so the subscription layer can enforce the **I9** cross-package
  isolation invariant: a subscription tagged with ``source_package=X``
  only matches events from a trigger that also carries
  ``source_package=X``.

* ``package_state``: a :class:`PackageStateHandle` bound to the same
  package.  Lets the trigger persist watermarks / backoff windows /
  poll-in-progress flags across server restarts without sharing tables
  with other packages.  See :mod:`carpenter.packages.state` for the
  isolation primitive.

Both kwargs are **optional** and default to ``None`` so existing
platform-shipped triggers (TimerTrigger, CounterTrigger, WebhookTrigger,
plus user-defined ones) keep working unchanged.  Subclasses that don't
override ``__init__`` inherit the new signature for free.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any
import logging

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ....packages.state import PackageStateHandle
    from ....packages.vectors import PackageVectorStore


class Trigger(ABC):
    """Base class for all triggers.

    Subclass this to create custom trigger types. Each trigger type must
    define a unique ``trigger_type()`` string (e.g., "timer", "counter").

    Triggers emit events via ``self.emit()``, which records the event
    in the event bus with an optional idempotency key.

    Args:
        name: Trigger instance name (must be unique within the process).
        config: Trigger-type-specific configuration dict.
        source_package: Optional manifest name of the capability package
            that shipped this trigger.  ``None`` for platform-builtin and
            config-defined triggers.  When set, the package name is
            stamped on every emitted event as ``_source_package`` so
            subscriptions can enforce I9 isolation.
        package_state: Optional :class:`PackageStateHandle` bound to
            ``source_package``.  ``None`` when ``source_package`` is
            ``None`` (no package context).  The installer wires this up
            so triggers can persist state without knowing the platform's
            DB internals.
        package_vectors: Optional :class:`PackageVectorStore` bound to
            ``source_package`` (Phase 2 PR-2 / D10).  Same I9 invariant
            as ``package_state``: the loader cross-checks that the
            handle's bound name matches ``source_package``.  Available
            so package-shipped triggers (e.g. an email indexer) can
            persist embeddings without knowing the platform's DB
            internals or being able to touch another package's vectors.
    """

    def __init__(
        self,
        name: str,
        config: dict,
        *,
        source_package: str | None = None,
        package_state: "PackageStateHandle | None" = None,
        package_vectors: "PackageVectorStore | None" = None,
    ):
        self.name = name
        self.config = config
        self.source_package = source_package
        # Sanity: if a package_state handle is provided it must be bound
        # to the same package the trigger claims to come from.  This is
        # the structural guarantee that backs I9 — a trigger from pkg-A
        # cannot ever receive pkg-B's state handle through the loader.
        if package_state is not None and source_package is not None:
            handle_pkg = getattr(package_state, "package_name", None)
            if handle_pkg != source_package:
                raise ValueError(
                    f"Trigger {name!r}: package_state handle bound to "
                    f"{handle_pkg!r} does not match source_package "
                    f"{source_package!r}",
                )
        self.package_state = package_state
        # Same cross-check for the vector handle.  Both checks together
        # mean the loader cannot accidentally hand a trigger from pkg-A
        # a handle bound to pkg-B for either state or vectors.
        if package_vectors is not None and source_package is not None:
            handle_pkg = getattr(package_vectors, "package_name", None)
            if handle_pkg != source_package:
                raise ValueError(
                    f"Trigger {name!r}: package_vectors handle bound to "
                    f"{handle_pkg!r} does not match source_package "
                    f"{source_package!r}",
                )
        self.package_vectors = package_vectors

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
        # I9: stamp the originating package on every event so the
        # subscription layer can enforce the cross-package check.
        # Platform-builtin triggers leave this absent (their
        # ``source_package`` is ``None``).
        if self.source_package is not None:
            payload["_source_package"] = self.source_package

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
