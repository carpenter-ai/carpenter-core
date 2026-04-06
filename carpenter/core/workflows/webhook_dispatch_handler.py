"""Webhook dispatch handler — routes incoming webhooks to arc/work actions.

When a webhook event is received (via /api/webhooks/{webhook_id}), this
handler looks up the subscription, parses the payload by source_type,
and executes the configured action (create arc from template or enqueue
a work item).

Source type parsers are pluggable. This module ships with Forgejo support;
GitHub, GitLab, and generic parsers can be added later.
"""

import json
import logging

from ...db import get_db, db_connection, db_transaction
from ..arcs import manager as arc_manager
from ..engine import work_queue

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Payload parsers — one per source_type
# ---------------------------------------------------------------------------


def _parse_forgejo_payload(data: dict, event_filter: list) -> dict | None:
    """Parse a Forgejo webhook payload.

    Returns a normalized event dict or None if the event should be ignored.
    """
    action = data.get("action", "")
    event_type = None

    # Determine event type from payload structure
    if "pull_request" in data:
        event_type = "pull_request"
    elif "ref" in data and "commits" in data:
        event_type = "push"
    elif "issue" in data:
        event_type = "issues"
    elif "release" in data:
        event_type = "release"
    else:
        event_type = "unknown"

    # Filter check
    if event_filter and event_type not in event_filter:
        return None

    result = {
        "source_type": "forgejo",
        "event_type": event_type,
        "action": action,
    }

    # Extract PR details
    if event_type == "pull_request":
        pr = data.get("pull_request", {})
        # Only process opened/synchronize/reopened actions
        if action not in ("opened", "synchronize", "reopened", "edited"):
            if event_filter and "pull_request" in event_filter:
                # User wants PR events but this action isn't interesting
                return None
        result["pr_number"] = pr.get("number")
        result["pr_title"] = pr.get("title", "")
        result["pr_body"] = pr.get("body", "")
        result["pr_state"] = pr.get("state", "")
        result["pr_user"] = pr.get("user", {}).get("login", "")
        result["head_branch"] = pr.get("head", {}).get("ref", "")
        result["base_branch"] = pr.get("base", {}).get("ref", "")
        result["html_url"] = pr.get("html_url", "")
        # Repo info
        repo = data.get("repository", {})
        result["repo_owner"] = repo.get("owner", {}).get("login", "")
        result["repo_name"] = repo.get("name", "")

    elif event_type == "push":
        result["ref"] = data.get("ref", "")
        result["commits"] = len(data.get("commits", []))

    return result


_PARSERS = {
    "forgejo": _parse_forgejo_payload,
}


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
) -> int:
    """Create a webhook subscription.

    Returns the subscription ID.
    """
    with db_transaction() as db:
        cursor = db.execute(
            "INSERT INTO webhook_subscriptions "
            "(webhook_id, source_type, source_config, event_filter, "
            " action_type, action_config, conversation_id, forge_hook_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                webhook_id,
                source_type,
                json.dumps(source_config or {}),
                json.dumps(event_filter or []),
                action_type,
                json.dumps(action_config or {}),
                conversation_id,
                forge_hook_id,
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

    # Parse payload
    parser = _PARSERS.get(source_type)
    if parser is None:
        logger.warning("No parser for source_type '%s'", source_type)
        return

    parsed = parser(data, event_filter)
    if parsed is None:
        logger.debug("Webhook %s event filtered out", webhook_id)
        return

    logger.info(
        "Webhook %s: %s %s (action=%s)",
        webhook_id, parsed.get("event_type"), parsed.get("action", ""),
        action_type,
    )

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
