"""Webhook dispatch handler — routes incoming webhooks to arc/work actions.

When a webhook event is received (via /api/webhooks/{webhook_id}), this
handler looks up the subscription, parses the payload via the configured
forge provider, and executes the configured action (create arc from
template or enqueue a work item).

Forge-specific payload parsing now lives on each
:class:`carpenter.forges.protocol.ForgeProvider` implementation
(``parse_webhook_legacy``).  ``"generic"`` source_type is handled inline.
"""

import json
import logging

from ...db import get_db, db_connection, db_transaction
from ...forges import get_forge_provider
from ..arcs import manager as arc_manager
from ..engine import work_queue

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Built-in parsers
# ---------------------------------------------------------------------------


def _parse_generic_payload(data: dict, event_filter: list) -> dict | None:
    """Passthrough parser for arbitrary JSON webhooks.

    Returns a minimal normalized event dict.  The raw payload body is
    preserved in ``data`` so downstream handlers (e.g. the Resource
    wrap path) can write it untouched to the Resource blob.
    """
    return {
        "source_type": "generic",
        "event_type": "generic",
        "action": "",
    }


def _parse_payload(source_type: str, data: dict, event_filter: list) -> dict | None:
    """Resolve the parser for ``source_type`` and apply it.

    For ``"generic"`` the built-in parser is used.  Any other
    ``source_type`` is looked up in the forge-provider registry and the
    provider's ``parse_webhook_legacy`` is invoked.  Returns ``None`` when
    no provider handles the type (callers log + skip).
    """
    if source_type == "generic":
        return _parse_generic_payload(data, event_filter)
    provider = get_forge_provider(source_type)
    if provider is None:
        return None
    return provider.parse_webhook_legacy(data, event_filter)


# ---------------------------------------------------------------------------
# Subscription management
# ---------------------------------------------------------------------------


def create_subscription(
    webhook_id: str,
    source_type: str,
    action_type: str,
    action_config: dict | None = None,
    source_config: dict | None = None,
    event_filter: list | None = None,
    conversation_id: int | None = None,
    forge_hook_id: int | None = None,
    resource_content_type: str | None = None,
    auto_approve_verdict: bool = False,
) -> int:
    """Create a webhook subscription.

    Optional Phase B PR B2 params:
        resource_content_type: when set, incoming webhook payloads are
            wrapped as raw-ingest Resources with this content_type and a
            REVIEWER (+JUDGE) template pipeline is spawned to process
            them.  Must match a template's ``consumes_content_type`` in
            ``config_seed/resource_templates.yaml`` — otherwise the
            wrap path falls back to legacy behaviour.
        auto_approve_verdict: config override to the "nothing starts
            trusted" default.  When True, no JUDGE arc is spawned; the
            REVIEWER's completion auto-marks the derived Resource as
            approved.

    Returns the subscription ID.
    """
    with db_transaction() as db:
        cursor = db.execute(
            "INSERT INTO webhook_subscriptions "
            "(webhook_id, source_type, source_config, event_filter, "
            " action_type, action_config, conversation_id, forge_hook_id, "
            " resource_content_type, auto_approve_verdict) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                webhook_id,
                source_type,
                json.dumps(source_config or {}),
                json.dumps(event_filter or []),
                action_type,
                json.dumps(action_config or {}),
                conversation_id,
                forge_hook_id,
                resource_content_type,
                1 if auto_approve_verdict else 0,
            ),
        )
        return cursor.lastrowid


def get_subscription(webhook_id: str) -> dict | None:
    """Look up a subscription by webhook_id."""
    with db_connection() as db:
        row = db.execute(
            "SELECT * FROM webhook_subscriptions WHERE webhook_id = ? AND enabled = 1",
            (webhook_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)


def list_subscriptions(source_type: str | None = None) -> list[dict]:
    """List webhook subscriptions, optionally filtered by source_type.

    Returns a list of subscription dicts.
    """
    with db_connection() as db:
        if source_type:
            rows = db.execute(
                "SELECT * FROM webhook_subscriptions WHERE source_type = ? "
                "ORDER BY id DESC",
                (source_type,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM webhook_subscriptions ORDER BY id DESC",
            ).fetchall()
        return [dict(row) for row in rows]


def delete_subscription(webhook_id: str) -> bool:
    """Delete a subscription by webhook_id."""
    with db_transaction() as db:
        cursor = db.execute(
            "DELETE FROM webhook_subscriptions WHERE webhook_id = ?",
            (webhook_id,),
        )
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Dispatch handler
# ---------------------------------------------------------------------------


async def handle_webhook_received(work_id: int, payload: dict):
    """Handle a webhook.received work item.

    Payload keys (from webhooks.py):
        webhook_id: The webhook identifier from the URL
        data: The parsed JSON body of the webhook request
    """
    webhook_id = payload.get("webhook_id")
    data = payload.get("data", {})

    if not webhook_id:
        logger.warning("webhook.received with no webhook_id")
        return

    # Look up subscription
    sub = get_subscription(webhook_id)
    if sub is None:
        logger.info("No subscription for webhook %s (unregistered or disabled)", webhook_id)
        return

    source_type = sub["source_type"]
    event_filter = json.loads(sub["event_filter"]) if isinstance(sub["event_filter"], str) else sub["event_filter"]
    action_type = sub["action_type"]
    action_config = json.loads(sub["action_config"]) if isinstance(sub["action_config"], str) else sub["action_config"]

    # Parse payload via the forge-provider registry (or built-in generic).
    parsed = _parse_payload(source_type, data, event_filter)
    if parsed is None:
        # Either no provider handles source_type, or the parser filtered
        # the event out.  Log unknown-provider case loudly; filter case
        # quietly.
        if source_type != "generic" and get_forge_provider(source_type) is None:
            logger.warning("No provider for source_type '%s'", source_type)
        else:
            logger.debug("Webhook %s event filtered out", webhook_id)
        return

    logger.info(
        "Webhook %s: %s %s (action=%s)",
        webhook_id, parsed.get("event_type"), parsed.get("action", ""),
        action_type,
    )

    # Phase B PR B2: Resource-wrap path.  When the subscription configures
    # a ``resource_content_type``, wrap the payload as a raw-ingest
    # Resource and spawn a template pipeline.  Falls back to legacy
    # behaviour when the content_type has no registered template
    # (wrap_webhook_as_resource returns None in that case).
    if sub.get("resource_content_type"):
        from .webhook_resource_wrap import wrap_webhook_as_resource
        result = wrap_webhook_as_resource(
            webhook_id=webhook_id,
            payload=data,
            parsed=parsed,
            subscription=sub,
        )
        if result is not None:
            return
        # else: template missing — fall through to legacy path.

    # Execute action
    if action_type == "create_arc":
        _create_arc_from_webhook(sub, action_config, parsed)
    elif action_type == "enqueue_work":
        event_type = action_config.get("event_type", "webhook.action")
        work_payload = {**action_config.get("payload", {}), **parsed}
        work_queue.enqueue(event_type, work_payload)
    else:
        logger.warning("Unknown action_type '%s' for webhook %s", action_type, webhook_id)


def _create_arc_from_webhook(sub: dict, action_config: dict, parsed: dict):
    """Create an arc from a template based on webhook data."""
    template_name = action_config.get("template_name", "")
    arc_name = action_config.get("arc_name", f"webhook-{parsed.get('event_type', 'event')}")
    arc_goal = action_config.get("arc_goal", "")

    # Inject PR details into goal if available
    if parsed.get("pr_number"):
        arc_goal = arc_goal or (
            f"Review PR #{parsed['pr_number']}: {parsed.get('pr_title', '')}"
        )
        arc_name = f"pr-review-{parsed['pr_number']}"

    arc_id = arc_manager.create_arc(
        name=arc_name,
        goal=arc_goal,
    )

    # Store parsed webhook data as arc state
    from ._arc_state import set_arc_state as _set_arc_state
    _set_arc_state(arc_id, "webhook_data", parsed)
    _set_arc_state(arc_id, "template_name", template_name)

    if sub.get("conversation_id"):
        _set_arc_state(arc_id, "conversation_id", sub["conversation_id"])

    # Store PR-specific state for downstream handlers
    if parsed.get("pr_number"):
        _set_arc_state(arc_id, "pr_number", parsed["pr_number"])
        _set_arc_state(arc_id, "repo_owner", parsed.get("repo_owner", ""))
        _set_arc_state(arc_id, "repo_name", parsed.get("repo_name", ""))

    # Enqueue the first step of the template workflow
    if template_name:
        first_step = action_config.get("first_step", f"{template_name}.fetch-pr")
        work_queue.enqueue(first_step, {"arc_id": arc_id})

    logger.info("Created arc %d from webhook %s", arc_id, sub.get("webhook_id", "?"))


def register_handlers(register_fn):
    """Register webhook dispatch handler with the main loop.

    Args:
        register_fn: The main_loop.register_handler function.
    """
    register_fn("webhook.received", handle_webhook_received)
