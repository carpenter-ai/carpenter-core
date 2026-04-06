"""Read-only webhook tools. Tier 1: callback to platform."""
from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=True, side_effects=False,
      param_types={"source_type": "Label"})
def list_subscriptions(source_type: str | None = None) -> list[dict]:
    """List active webhook subscriptions.

    Args:
        source_type: Optional filter by source type (e.g. 'forgejo').

    Returns:
        List of subscription dicts.
    """
    result = callback("webhook.list", {
        "source_type": source_type,
    })
    return result.get("subscriptions", [])
