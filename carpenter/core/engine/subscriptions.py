"""Subscription system for Carpenter.

Config-driven event→action mappings. Subscriptions are persistent (unlike
one-shot event matchers) — they remain active and match against every new
event until disabled or removed.

Processing flow (called from main_loop each heartbeat):
1. Query unprocessed events ordered by priority DESC, created_at ASC
2. For each event, find matching subscriptions (event_type + filter)
3. In one transaction: create all work_queue items + mark event processed
4. Return count of actions created

Predefined action types:
- enqueue_work: create a work_queue item
- forward_timer: route timer.fired events to work_queue using the cron
  entry's original event_type (built-in, used by the timer pipeline)
- create_arc: create an arc from parameters
- send_notification: send a notification
"""

import json
import logging
from dataclasses import dataclass, field

from ...db import get_db
from ._utils import filter_matches

logger = logging.getLogger(__name__)


@dataclass
class Subscription:
    """A persistent event→action mapping."""

    name: str
    event_type: str  # which event type to match
    event_filter: dict | None = None  # optional payload subset match
    action_type: str = "enqueue_work"  # predefined action type
    action_config: dict = field(default_factory=dict)  # parameters for the action
    enabled: bool = True


# In-memory subscription list, loaded from config at startup
_subscriptions: list[Subscription] = []


def load_subscriptions(sub_configs: list[dict]) -> int:
    """Load subscriptions from config dicts.

    Each config dict should have:
        - name: unique subscription name
        - on: event type to match
        - filter: optional payload filter dict
        - action: dict with 'type' and action-specific params
        - enabled: bool (default True)

    Returns count of subscriptions loaded.
    """
    loaded = 0
    for cfg in sub_configs:
        name = cfg.get("name")
        event_type = cfg.get("on")
        enabled = cfg.get("enabled", True)

        if not name or not event_type:
            logger.warning("Subscription config missing name or event type: %s", cfg)
            continue

        action = cfg.get("action", {})
        action_type = action.get("type", "enqueue_work")

        # Build action_config from action dict minus the 'type' key
        action_config = {k: v for k, v in action.items() if k != "type"}

        sub = Subscription(
            name=name,
            event_type=event_type,
            event_filter=cfg.get("filter"),
            action_type=action_type,
            action_config=action_config,
            enabled=enabled,
        )
        _subscriptions.append(sub)
        loaded += 1
        logger.debug("Loaded subscription: %s (on=%s, action=%s)", name, event_type, action_type)

    if loaded:
        logger.info("Loaded %d subscription(s)", loaded)
    return loaded


def get_subscriptions() -> list[Subscription]:
    """Return all loaded subscriptions."""
    return list(_subscriptions)


def _filter_matches(event_filter: dict | None, payload: dict) -> bool:
    """Check if a subscription's filter matches an event payload.

    Thin wrapper around ``filter_matches`` from ``._utils`` for backward
    compatibility (tests reference ``subscriptions._filter_matches``).
    """
    return filter_matches(event_filter, payload)


def _execute_action(db, sub: Subscription, event: dict, payload: dict) -> bool:
    """Execute a subscription's action within the current transaction.

    Args:
        db: Database connection (in transaction).
        sub: The matched subscription.
        event: The event row dict.
        payload: Parsed event payload.

    Returns:
        True if action was created, False if skipped.
    """
    if sub.action_type == "enqueue_work":
        return _action_enqueue_work(db, sub, event, payload)
    elif sub.action_type == "forward_timer":
        return _action_forward_timer(db, sub, event, payload)
    elif sub.action_type == "create_arc":
        return _action_create_arc(db, sub, event, payload)
    elif sub.action_type == "send_notification":
        return _action_send_notification(db, sub, event, payload)
    else:
        logger.warning("Unknown action type %r in subscription %s", sub.action_type, sub.name)
        return False


def _action_enqueue_work(db, sub: Subscription, event: dict, payload: dict) -> bool:
    """Create a work_queue item from subscription config."""
    event_type = sub.action_config.get("event_type", event["event_type"])
    work_payload = dict(sub.action_config.get("payload", {}))

    # Optionally merge event payload into work item payload
    if sub.action_config.get("payload_merge", False):
        work_payload.update(payload)

    # Always include subscription metadata
    work_payload["_subscription"] = sub.name
    work_payload["_event_id"] = event["id"]

    idempotency_key = f"sub-{sub.name}-event-{event['id']}"

    cursor = db.execute(
        "INSERT OR IGNORE INTO work_queue "
        "(event_type, payload_json, idempotency_key, max_retries) "
        "VALUES (?, ?, ?, ?)",
        (event_type, json.dumps(work_payload), idempotency_key, 3),
    )
    return cursor.rowcount > 0


def _action_forward_timer(db, sub: Subscription, event: dict, payload: dict) -> bool:
    """Route a timer.fired event to the work_queue using the cron entry's event_type.

    This is the built-in action that bridges the timer/cron system with the
    work_queue. When a cron entry fires, ``check_cron()`` emits a ``timer.fired``
    event. This action extracts the cron entry's original ``event_type``
    (e.g., ``cron.message`` or ``arc.dispatch``) from the event payload and
    creates a work_queue item with that event_type.

    The work_queue payload is structured so that existing handlers
    (``cron.message``, ``arc.dispatch``) work unchanged.
    """
    # Extract the target event_type from the timer event payload
    target_event_type = payload.get("cron_event_type")
    if not target_event_type:
        logger.warning(
            "forward_timer: timer.fired event %d missing cron_event_type in payload",
            event["id"],
        )
        return False

    # Build the work payload in the same format handlers expect
    work_payload = {
        "cron_id": payload.get("cron_id"),
        "cron_name": payload.get("cron_name"),
        "fire_time": payload.get("fire_time"),
    }
    if "event_payload" in payload:
        work_payload["event_payload"] = payload["event_payload"]

    idempotency_key = f"sub-{sub.name}-event-{event['id']}"

    cursor = db.execute(
        "INSERT OR IGNORE INTO work_queue "
        "(event_type, payload_json, idempotency_key, max_retries) "
        "VALUES (?, ?, ?, ?)",
        (target_event_type, json.dumps(work_payload), idempotency_key, 4),
    )
    return cursor.rowcount > 0


def _action_create_arc(db, sub: Subscription, event: dict, payload: dict) -> bool:
    """Enqueue an arc creation work item.

    Rather than creating the arc directly (which requires complex
    transactional logic), we enqueue a work item that the arc dispatch
    handler will process.
    """
    arc_config = dict(sub.action_config)
    arc_config["_subscription"] = sub.name
    arc_config["_event_id"] = event["id"]
    arc_config["_event_payload"] = payload

    idempotency_key = f"sub-arc-{sub.name}-event-{event['id']}"

    cursor = db.execute(
        "INSERT OR IGNORE INTO work_queue "
        "(event_type, payload_json, idempotency_key, max_retries) "
        "VALUES (?, ?, ?, ?)",
        ("subscription.create_arc", json.dumps(arc_config), idempotency_key, 3),
    )
    return cursor.rowcount > 0


def _action_send_notification(db, sub: Subscription, event: dict, payload: dict) -> bool:
    """Enqueue a notification work item."""
    notif_config = dict(sub.action_config)
    notif_config["_subscription"] = sub.name
    notif_config["_event_id"] = event["id"]

    # Template the message with event payload
    message = notif_config.get("message", "")
    if "{" in message:
        try:
            message = message.format(**payload)
            notif_config["message"] = message
        except (KeyError, IndexError):
            pass  # leave unformatted

    idempotency_key = f"sub-notif-{sub.name}-event-{event['id']}"

    cursor = db.execute(
        "INSERT OR IGNORE INTO work_queue "
        "(event_type, payload_json, idempotency_key, max_retries) "
        "VALUES (?, ?, ?, ?)",
        ("subscription.notification", json.dumps(notif_config), idempotency_key, 1),
    )
    return cursor.rowcount > 0


def process_subscriptions() -> int:
    """Process unprocessed events against persistent subscriptions.

    For each unprocessed event (ordered by priority DESC, created_at ASC):
    1. Find matching subscriptions (event_type + filter)
    2. Execute actions (create work items)
    3. Mark event as subscription-processed

    All actions for one event are created in a single transaction.

    Returns the number of actions created.

    Note: This runs alongside process_events() (one-shot matchers).
    Events are only marked processed by process_events(); subscription
    processing uses a separate marker to avoid conflicts. Events that
    have been matcher-processed are still eligible for subscription
    matching — subscriptions look at all events not yet subscription-processed.
    """
    if not _subscriptions:
        return 0

    enabled_subs = [s for s in _subscriptions if s.enabled]
    if not enabled_subs:
        return 0

    db = get_db()
    actions_created = 0
    try:
        # Get unprocessed events — process_events() uses 'processed' column
        # for one-shot matchers. Subscriptions process all events including
        # those already matcher-processed. We rely on idempotency keys to
        # prevent duplicate work items.
        events = db.execute(
            "SELECT id, event_type, payload_json FROM events "
            "WHERE processed = FALSE "
            "ORDER BY priority DESC, created_at ASC"
        ).fetchall()

        for event in events:
            payload = json.loads(event["payload_json"])

            # Find all matching subscriptions
            for sub in enabled_subs:
                if sub.event_type != event["event_type"]:
                    continue
                if not filter_matches(sub.event_filter, payload):
                    continue

                try:
                    if _execute_action(db, sub, dict(event), payload):
                        actions_created += 1
                except Exception:
                    logger.exception(
                        "Error executing action for subscription %s on event %d",
                        sub.name, event["id"],
                    )

        db.commit()
        return actions_created
    finally:
        db.close()


def load_builtin_subscriptions() -> int:
    """Register built-in subscriptions required for core system functionality.

    Currently registers:
    - ``_builtin.timer_forward``: routes ``timer.fired`` events from the cron
      system to the work_queue using the cron entry's original event_type.
      This is the bridge that makes ``check_cron()`` -> event pipeline ->
      work_queue work transparently.
    - ``webhook-dispatch``: routes ``webhook.received`` events from the
      webhook API endpoint to the work_queue for the webhook dispatch handler.

    Idempotent: skips if already loaded (checks by name).

    Returns count of subscriptions added.
    """
    from .trigger_manager import TIMER_FIRED_EVENT
    from ...api.webhooks import WEBHOOK_DISPATCH_SUBSCRIPTION

    existing_names = {s.name for s in _subscriptions}
    added = 0

    # Timer forwarding: cron -> timer.fired event -> work_queue
    builtin_name = "_builtin.timer_forward"
    if builtin_name not in existing_names:
        sub = Subscription(
            name=builtin_name,
            event_type=TIMER_FIRED_EVENT,
            event_filter=None,  # match all timer.fired events
            action_type="forward_timer",
            action_config={},
            enabled=True,
        )
        _subscriptions.append(sub)
        added += 1
        logger.debug("Loaded built-in subscription: %s", builtin_name)

    # Webhook dispatch: webhook.received event -> work_queue
    wh = WEBHOOK_DISPATCH_SUBSCRIPTION
    wh_name = wh["name"]
    if wh_name not in existing_names:
        action = wh.get("action", {})
        sub = Subscription(
            name=wh_name,
            event_type=wh["on"],
            action_type=action.get("type", "enqueue_work"),
            action_config={k: v for k, v in action.items() if k != "type"},
            enabled=True,
        )
        _subscriptions.append(sub)
        added += 1
        logger.debug("Loaded built-in subscription: %s", wh_name)

    if added:
        logger.info("Loaded %d built-in subscription(s)", added)
    return added


def reset() -> None:
    """Clear all subscriptions. For testing only."""
    _subscriptions.clear()
