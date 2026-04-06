"""Webhook subscription management tools. Tier 1: callback to platform.

Action tools only — list_subscriptions is in carpenter_tools/read/webhook.py.
"""
from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_types={"source_type": "Label", "action_type": "Label", "repo_owner": "Label", "repo_name": "Label"})
def subscribe(
    source_type: str,
    event_filter: list,
    action_type: str,
    action_config: dict,
    repo_owner: str,
    repo_name: str,
    conversation_id: int | None = None,
) -> dict:
    """Create a webhook subscription and register on the git server.

    Registers a webhook on the git server side and creates a local subscription
    that maps incoming events to arc creation or work items.

    Args:
        source_type: Webhook source type (e.g. 'forgejo').
        event_filter: Event types to subscribe to (e.g. ['pull_request']).
        action_type: Action on event: 'create_arc' or 'enqueue_work'.
        action_config: Action configuration dict.
        repo_owner: Repository owner for forge webhook registration.
        repo_name: Repository name for forge webhook registration.
        conversation_id: Optional conversation to link subscription to.

    Returns:
        Dict with webhook_id, subscription_id, and forge_hook_id.
    """
    return callback("webhook.subscribe", {
        "source_type": source_type,
        "event_filter": event_filter,
        "action_type": action_type,
        "action_config": action_config,
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "conversation_id": conversation_id,
    })


@tool(local=True, readonly=False, side_effects=True,
      param_types={"webhook_id": "Label"})
def delete(webhook_id: str) -> dict:
    """Delete a webhook subscription and remove from forge.

    Args:
        webhook_id: The webhook_id to delete.

    Returns:
        Dict with deleted: bool.
    """
    return callback("webhook.delete", {"webhook_id": webhook_id})
