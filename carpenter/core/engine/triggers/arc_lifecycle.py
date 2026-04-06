"""Arc lifecycle trigger — emits events on arc state transitions.

Not a PollableTrigger — hooks directly into manager.py's update_status().
When an arc transitions, record_event() is called with structured
payload including arc metadata.

This makes arc lifecycle events visible to the entire event pipeline,
enabling subscriptions to react to arc completions, failures, etc.
"""

import logging

from .. import event_bus

logger = logging.getLogger(__name__)


def emit_status_changed(
    arc_id: int,
    old_status: str,
    new_status: str,
    arc_name: str | None = None,
    arc_role: str | None = None,
    parent_id: int | None = None,
    agent_type: str | None = None,
) -> int | None:
    """Emit an arc.status_changed event.

    Called from manager.py update_status() after a successful transition.

    Args:
        arc_id: The arc that changed status.
        old_status: Previous status.
        new_status: New status.
        arc_name: Arc name (for filtering).
        arc_role: Arc role (worker, coordinator, verifier).
        parent_id: Parent arc ID (None for root arcs).
        agent_type: Agent type (EXECUTOR, PLANNER, etc.).

    Returns:
        Event ID, or None if duplicate.
    """
    payload = {
        "arc_id": arc_id,
        "old_status": old_status,
        "new_status": new_status,
        "is_root": parent_id is None,
    }

    if arc_name is not None:
        payload["arc_name"] = arc_name
    if arc_role is not None:
        payload["arc_role"] = arc_role
    if parent_id is not None:
        payload["parent_id"] = parent_id
    if agent_type is not None:
        payload["agent_type"] = agent_type

    # Deterministic idempotency key prevents duplicate events if
    # transition code runs twice
    idempotency_key = f"arc-{arc_id}-{old_status}-{new_status}"

    return event_bus.record_event(
        event_type="arc.status_changed",
        payload=payload,
        source=f"arc:{arc_id}",
        idempotency_key=idempotency_key,
    )
