"""Webhook subscription management tool backend.

Provides subscribe/list/delete operations for webhook subscriptions.
Subscribe also registers the webhook on the Forgejo side so everything
is wired up in a single call.
"""
import json
import logging
import secrets

from ..core.workflows import webhook_dispatch_handler
from . import forgejo_api as forgejo_api_backend
from .. import config

logger = logging.getLogger(__name__)


def _build_webhook_target_url(webhook_id: str) -> str:
    """Construct the URL that Forgejo should POST to for this webhook.

    Uses tls_domain if configured (public-facing HTTPS), otherwise
    falls back to host:port (local/dev).
    """
    tls_domain = config.CONFIG.get("tls_domain", "")
    if tls_domain:
        scheme = "https"
        host_part = tls_domain
    else:
        host = config.CONFIG.get("host", "127.0.0.1")
        port = config.CONFIG.get("port", 7842)
        scheme = "https" if config.CONFIG.get("tls_enabled") else "http"
        host_part = f"{host}:{port}"

    return f"{scheme}://{host_part}/api/webhooks/{webhook_id}"


def handle_subscribe(params: dict) -> dict:
    """Create a webhook subscription and optionally register on Forgejo.

    params:
        source_type: e.g. "forgejo"
        event_filter: list of event types, e.g. ["pull_request"]
        action_type: e.g. "create_arc"
        action_config: dict, e.g. {"template_name": "pr-review", ...}
        repo_owner: Forgejo repo owner (required for Forgejo registration)
        repo_name: Forgejo repo name (required for Forgejo registration)
        conversation_id: optional, links subscription to a conversation

    Returns: {webhook_id, subscription_id, forge_hook_id}
    """
    source_type = params.get("source_type", "forgejo")
    event_filter = params.get("event_filter", [])
    action_type = params.get("action_type", "create_arc")
    action_config = params.get("action_config", {})
    repo_owner = params.get("repo_owner", "")
    repo_name = params.get("repo_name", "")
    conversation_id = params.get("conversation_id")

    # Generate a unique webhook ID
    webhook_id = secrets.token_hex(16)

    # Register webhook on Forgejo if repo info is provided
    forge_hook_id = None
    if source_type == "forgejo" and repo_owner and repo_name:
        target_url = _build_webhook_target_url(webhook_id)
        hook_result = forgejo_api_backend.handle_create_repo_webhook({
            "repo_owner": repo_owner,
            "repo_name": repo_name,
            "target_url": target_url,
            "events": event_filter or ["push"],
        })
        if "error" in hook_result:
            return {"error": f"Failed to register Forgejo webhook: {hook_result['error']}"}
        forge_hook_id = hook_result.get("hook_id")

    # Create the subscription in our database
    subscription_id = webhook_dispatch_handler.create_subscription(
        webhook_id=webhook_id,
        source_type=source_type,
        action_type=action_type,
        action_config=action_config,
        source_config={"repo_owner": repo_owner, "repo_name": repo_name} if repo_owner else None,
        event_filter=event_filter,
        conversation_id=conversation_id,
        forge_hook_id=forge_hook_id,
    )

    logger.info(
        "Created webhook subscription %d (webhook_id=%s, forge_hook_id=%s)",
        subscription_id, webhook_id, forge_hook_id,
    )

    return {
        "webhook_id": webhook_id,
        "subscription_id": subscription_id,
        "forge_hook_id": forge_hook_id,
    }


def handle_list(params: dict) -> dict:
    """List active webhook subscriptions.

    params:
        source_type: optional filter by source type

    Returns: {subscriptions: [...]}
    """
    source_type = params.get("source_type")
    subs = webhook_dispatch_handler.list_subscriptions(source_type=source_type)

    # Parse JSON fields for readability
    result = []
    for sub in subs:
        entry = dict(sub)
        for json_field in ("source_config", "event_filter", "action_config"):
            if isinstance(entry.get(json_field), str):
                try:
                    entry[json_field] = json.loads(entry[json_field])
                except (json.JSONDecodeError, TypeError):
                    pass
        result.append(entry)

    return {"subscriptions": result}


def handle_delete(params: dict) -> dict:
    """Delete a webhook subscription and optionally remove Forgejo webhook.

    params:
        webhook_id: the webhook identifier to delete

    Returns: {deleted: bool}
    """
    webhook_id = params.get("webhook_id", "")
    if not webhook_id:
        return {"deleted": False, "error": "webhook_id is required"}

    # Look up the subscription to get forge_hook_id and repo info
    sub = webhook_dispatch_handler.get_subscription(webhook_id)
    if sub is None:
        # Try to delete anyway (might be disabled)
        deleted = webhook_dispatch_handler.delete_subscription(webhook_id)
        return {"deleted": deleted}

    # Delete Forgejo-side webhook if we have the hook ID and repo info
    forge_hook_id = sub.get("forge_hook_id")
    if forge_hook_id:
        source_config = sub.get("source_config", "{}")
        if isinstance(source_config, str):
            try:
                source_config = json.loads(source_config)
            except (json.JSONDecodeError, TypeError):
                source_config = {}

        repo_owner = source_config.get("repo_owner", "")
        repo_name = source_config.get("repo_name", "")

        if repo_owner and repo_name:
            delete_result = forgejo_api_backend.handle_delete_repo_webhook({
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "hook_id": forge_hook_id,
            })
            if "error" in delete_result:
                logger.warning(
                    "Failed to delete Forgejo webhook %s: %s",
                    forge_hook_id, delete_result["error"],
                )
                # Continue with local deletion even if Forgejo cleanup fails

    deleted = webhook_dispatch_handler.delete_subscription(webhook_id)

    logger.info("Deleted webhook subscription %s: %s", webhook_id, deleted)
    return {"deleted": deleted}
