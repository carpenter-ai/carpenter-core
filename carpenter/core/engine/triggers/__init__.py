"""Trigger system for Carpenter.

Triggers produce events that flow through the event pipeline:
Trigger → Event Queue → Subscriptions → Action Queue (work_queue).

Built-in trigger types:
- TimerTrigger: cron-based schedules (wraps trigger_manager)
- CounterTrigger: fires when event count reaches threshold
- ArcLifecycleTrigger: hooks into arc state transitions
- WebhookTrigger: HTTP endpoint for external events

Users can define custom triggers by subclassing Trigger or PollableTrigger.
"""

from .base import Trigger, PollableTrigger, EndpointTrigger  # noqa: F401
from .registry import (  # noqa: F401
    register_trigger_type,
    load_triggers,
    load_user_triggers,
    get_trigger_instances,
    get_pollable_triggers,
    get_endpoint_triggers,
    start_all,
    stop_all,
    check_pollable_triggers,
)
