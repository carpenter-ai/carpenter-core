"""Read-only webhook tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""
from ..tool_meta import tool


@tool(local=True, readonly=True, side_effects=False,
      param_types={"source_type": "Label"})
def list_subscriptions(source_type: str | None = None) -> list[dict]:
    """List active webhook subscriptions.

    Args:
        source_type: Optional filter by configured forge name (e.g. 'forgejo').

    Returns:
        List of subscription dicts.
    """
    ...
