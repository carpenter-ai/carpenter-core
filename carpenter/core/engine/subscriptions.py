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
    # B-full (D24): when a capability package contributes a subscription
    # the installer tags it with ``source_package`` so
    # :func:`unregister_for_package` can drop matching entries on
    # uninstall.  Subscriptions registered from platform config or
    # built-ins leave this ``None``.
    source_package: str | None = None


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
        # Accept "on", "event", or the literal ``True`` key (YAML 1.1 parses
        # bare ``on:`` as a boolean, which templates declared from YAML are
        # prone to; quoting the key is another option but this avoids the
        # footgun).
        # Fallback keys: Python ``True`` (dict-form configs loaded direct
        # from YAML), and the string ``"true"`` (same keys after a
        # JSON round-trip, since JSON has no bool keys).
        event_type = (
            cfg.get("on")
            or cfg.get("event")
            or cfg.get(True)
            or cfg.get("true")
        )
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


def _source_package_matches(
    sub: "Subscription", payload: dict,
) -> bool:
    """I9 cross-package isolation gate (Phase 3a PR-B).

    Enforces: if an event was emitted by a packaged trigger (carries
    ``_source_package`` in its payload), then a packaged subscription
    can only match it when ``sub.source_package == _source_package``.

    The check is **only active when both sides are tagged** so that:

    * Platform-builtin and config-defined subscriptions
      (``sub.source_package=None``) continue to match every event,
      including those emitted by packaged triggers — needed so the
      built-in ``timer_forward`` / ``webhook-dispatch`` subscriptions
      still work for packaged trigger output if anyone wires it.
    * Packaged subscriptions can still match events from non-package
      sources (raw ``event_bus.record_event`` calls, HTTP webhooks,
      external systems) which leave ``_source_package`` absent.  This
      is the "package responds to external event" pattern that
      ``trigger_subscriptions`` was designed around in B-full.

    The case the check is designed to block is the cross-package one:
    package X's trigger emits an event ⇒ package Y's subscription
    must NOT receive it.  That's the new I9 surface (least privilege
    between packages) the plan calls out.
    """
    sub_pkg = sub.source_package
    if sub_pkg is None:
        # Untagged subscription: legacy semantics, match everything.
        return True
    event_pkg = payload.get("_source_package")
    if event_pkg is None:
        # Untagged event (platform / external source): permissive — let
        # the packaged subscription see it.  Closes a back-compat gap
        # without weakening cross-package isolation.
        return True
    return event_pkg == sub_pkg


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
    elif sub.action_type == "package_dispatch":
        return _action_package_dispatch(db, sub, event, payload)
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
    transactional logic), we enqueue a work item that the
    ``subscription.create_arc`` handler will process.

    Optional ``template_name`` or ``template_id`` in the action config
    names a workflow template. When provided, the template is resolved
    here and its ``template_id`` is stored in the enqueued payload so
    the downstream handler can call ``instantiate_template`` without
    repeating the lookup. Resolution failures skip the action (and log)
    rather than creating an arc with a missing/invalid template.
    """
    arc_config = dict(sub.action_config)
    arc_config["_subscription"] = sub.name
    arc_config["_event_id"] = event["id"]
    arc_config["_event_payload"] = payload

    template_name = arc_config.get("template_name")
    template_id = arc_config.get("template_id")
    if template_name or template_id is not None:
        resolved = _resolve_template(template_name, template_id)
        if resolved is None:
            logger.warning(
                "Subscription %s: create_arc skipped, template not found "
                "(template_name=%r, template_id=%r)",
                sub.name, template_name, template_id,
            )
            return False
        arc_config["template_id"] = resolved["id"]
        arc_config["template_name"] = resolved["name"]

    idempotency_key = f"sub-arc-{sub.name}-event-{event['id']}"

    cursor = db.execute(
        "INSERT OR IGNORE INTO work_queue "
        "(event_type, payload_json, idempotency_key, max_retries) "
        "VALUES (?, ?, ?, ?)",
        ("subscription.create_arc", json.dumps(arc_config), idempotency_key, 3),
    )
    return cursor.rowcount > 0


def _resolve_template(name: str | None, template_id: int | None) -> dict | None:
    """Resolve a template by name or id. Returns the template row dict or None.

    If both are given, both must agree. Imported lazily to avoid a circular
    import at module load time.
    """
    from . import template_manager

    if template_id is not None:
        tmpl = template_manager.get_template(template_id)
        if tmpl is None:
            return None
        if name and tmpl.get("name") != name:
            logger.warning(
                "Template id %d resolves to name %r but action config says %r",
                template_id, tmpl.get("name"), name,
            )
            return None
        return tmpl
    if name:
        return template_manager.get_template_by_name(name)
    return None


def _substitute_event_refs(value, event_payload: dict):
    """Expand ``{event.payload.KEY}`` placeholders in subscription config.

    Supports string values and recurses into list/dict containers. An
    unmatched placeholder is left in place (no exception). Only the
    exact-match single-placeholder case preserves the original value's
    type; mixed strings return strings.
    """
    if isinstance(value, str):
        import re
        pattern = re.compile(r"\{event\.payload\.([A-Za-z0-9_]+)\}")
        # Exact single-placeholder match: preserve value type.
        m = pattern.fullmatch(value)
        if m and m.group(1) in event_payload:
            return event_payload[m.group(1)]
        return pattern.sub(
            lambda mm: str(event_payload.get(mm.group(1), mm.group(0))),
            value,
        )
    if isinstance(value, list):
        return [_substitute_event_refs(v, event_payload) for v in value]
    if isinstance(value, dict):
        return {k: _substitute_event_refs(v, event_payload) for k, v in value.items()}
    return value


def handle_subscription_create_arc(payload: dict) -> int | None:
    """Handle a ``subscription.create_arc`` work item.

    Creates a parent arc (named/goaled from the action config) and, if a
    template was resolved at enqueue time, instantiates the template's
    steps as children on that parent. The event payload is stored on the
    parent arc under the ``event_payload`` key for downstream handlers.

    Optional action config fields:

    - ``priority``: integer passed through to ``arc_manager.create_arc``
      (lower = more urgent, Unix-nice style).
    - ``agent_type``: agent type for the root arc (e.g. ``SUPERVISOR``
      for passive coordinator roots whose work is entirely carried out
      by template step handlers). Defaults to ``EXECUTOR``.
    - ``initial_arc_state``: dict of ``key -> value`` pairs written to
      ``arc_state`` on the parent arc after creation. Values may contain
      ``{event.payload.KEY}`` placeholders, substituted from the
      triggering event's payload.

    Returns the created parent arc ID, or ``None`` if creation was
    skipped (which should not normally happen — resolution failures are
    caught upstream in ``_action_create_arc``).
    """
    from ..arcs import manager as arc_manager
    from ..workflows._arc_state import set_arc_state
    from . import template_manager
    from .. import budget

    # Budget safety net: trigger-driven arc creation is autonomous work and
    # is exactly how a runaway feedback loop propagates. Refuse it while the
    # breaker is restricting/capping so a loop cannot keep spawning roots.
    allowed, reason = budget.autonomous_allowed()
    if not allowed:
        logger.warning(
            "Subscription %s: create_arc suppressed by budget breaker (%s)",
            payload.get("_subscription"), reason,
        )
        return None

    template_id = payload.get("template_id")
    template_name = payload.get("template_name")
    arc_name = payload.get("arc_name") or template_name or "subscription-arc"
    arc_goal = payload.get("arc_goal")
    priority = payload.get("priority")
    agent_type = payload.get("agent_type")
    event_payload = payload.get("_event_payload", {}) or {}

    create_kwargs = {}
    if priority is not None:
        create_kwargs["priority"] = priority
    if agent_type is not None:
        create_kwargs["agent_type"] = agent_type

    # Provenance: this root arc was spawned by a trigger/event subscription.
    # The whole tree inherits this origin via create_arc.
    subscription_name = payload.get("_subscription")
    origin_ref = json.dumps(
        {k: v for k, v in {"subscription": subscription_name}.items() if v}
    )

    parent_id = arc_manager.create_arc(
        name=arc_name,
        goal=arc_goal,
        template_id=template_id,
        origin_kind="trigger",
        origin_ref=origin_ref,
        **create_kwargs,
    )

    set_arc_state(parent_id, "event_payload", event_payload)
    set_arc_state(parent_id, "subscription_name", payload.get("_subscription"))

    initial_arc_state = payload.get("initial_arc_state") or {}
    for key, value in initial_arc_state.items():
        resolved = _substitute_event_refs(value, event_payload)
        set_arc_state(parent_id, key, resolved)

    if template_id is not None:
        template_manager.instantiate_template(template_id, parent_id)

    return parent_id


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


def _action_package_dispatch(
    db, sub: Subscription, event: dict, payload: dict,
) -> bool:
    """Dispatch an event to a package-shipped Python handler.

    Capability packages that declare ``trigger_subscriptions`` in their
    manifest get one of these subscriptions registered per entry at
    install time.  Rather than running the handler synchronously inside
    the subscription loop (which would block all subscription
    processing on a slow package), we enqueue a work item with the
    handler ref and let the work-queue dispatcher pick it up.

    The work item's ``event_type`` is always ``package.dispatch`` so a
    single handler in the platform can route to the package-specific
    Python handler the manifest declared.  See
    :func:`carpenter.packages.subscription_handler.dispatch_package_handler`
    for the work-side handler.
    """
    cfg = sub.action_config or {}
    package_name = cfg.get("package")
    handler_ref = cfg.get("handler")
    if not package_name or not handler_ref:
        logger.warning(
            "package_dispatch subscription %r missing 'package' or "
            "'handler' in action_config: %s",
            sub.name, cfg,
        )
        return False

    work_payload = {
        "_subscription": sub.name,
        "_event_id": event["id"],
        "package": package_name,
        "handler": handler_ref,
        "event_type": event["event_type"],
        "event_payload": payload,
    }
    idempotency_key = f"sub-pkg-{sub.name}-event-{event['id']}"
    cursor = db.execute(
        "INSERT OR IGNORE INTO work_queue "
        "(event_type, payload_json, idempotency_key, max_retries) "
        "VALUES (?, ?, ?, ?)",
        ("package.dispatch", json.dumps(work_payload), idempotency_key, 1),
    )
    return cursor.rowcount > 0


def unregister_for_package(package_name: str) -> int:
    """Drop in-memory subscriptions whose ``source_package`` matches.

    Called from :func:`carpenter.packages.installer.uninstall_package`
    on a clean uninstall (and from re-install before the new
    subscriptions are loaded, so we don't double-register).  Returns
    the number of subscriptions removed.  Idempotent.
    """
    if not package_name:
        return 0
    before = len(_subscriptions)
    _subscriptions[:] = [
        s for s in _subscriptions if s.source_package != package_name
    ]
    removed = before - len(_subscriptions)
    if removed:
        logger.info(
            "Removed %d subscription(s) for package %r",
            removed, package_name,
        )
    return removed


def load_package_subscriptions(records: list[tuple[str, list[dict]]]) -> int:
    """Re-register package subscriptions from on-disk JSON records.

    Called at server startup to rebuild the in-memory subscription list
    from each installed package's ``_subscriptions.json``.  ``records``
    is a list of ``(package_name, [{event, handler}, ...])`` tuples
    (the caller is responsible for walking the install dir; this
    function only handles the in-memory side).

    Returns the total number of subscriptions registered across all
    packages.
    """
    total = 0
    for package_name, entries in records:
        # Drop any prior registrations for this package so this is
        # idempotent across reload calls.
        unregister_for_package(package_name)
        for i, entry in enumerate(entries):
            event = entry.get("event")
            handler = entry.get("handler")
            if not event or not handler:
                logger.warning(
                    "package %r: subscription entry %d missing event/handler",
                    package_name, i,
                )
                continue
            sub = Subscription(
                name=f"_pkg.{package_name}.{i}",
                event_type=event,
                event_filter=None,
                action_type="package_dispatch",
                action_config={
                    "package": package_name,
                    "handler": handler,
                },
                enabled=True,
                source_package=package_name,
            )
            _subscriptions.append(sub)
            total += 1
    if total:
        logger.info(
            "Loaded %d package subscription(s) from %d package(s)",
            total, len(records),
        )
    return total


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
                # I9 cross-package isolation: a packaged subscription
                # can only fire on events tagged with the same package.
                # No-op for untagged (platform / config) subscriptions.
                if not _source_package_matches(sub, payload):
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
