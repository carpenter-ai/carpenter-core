"""Scheduling tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_types={"name": "Label", "event_type": "Label"})
def add_once(name: str, at_iso: str, event_type: str, event_payload: dict | None = None) -> int:
    """Add a one-shot trigger that fires once at the given ISO timestamp.

    Auto-deletes after firing. Returns cron ID. PREFER
    ``event_type='cron.message'`` for reminders. Use ``'arc.dispatch'``
    only when the fire must run code.

    Args:
        name: Unique name for this trigger.
        at_iso: ISO datetime in LOCAL time, no timezone suffix (platform
            converts to UTC). Example: '2026-04-05T14:30:00'.
        event_type: 'cron.message' or 'arc.dispatch'.
        event_payload: For 'cron.message': {"message": "<non-empty string>"}.
            For 'arc.dispatch': {"arc_id": <int from arc.create()['arc_id']>}.
            conversation_id is auto-injected.
    """
    ...


@tool(local=True, readonly=False, side_effects=True,
      param_types={"name": "Label", "event_type": "Label"})
def add_cron(name: str, cron_expr: str, event_type: str, event_payload: dict | None = None) -> int:
    """Add a recurring cron entry. Returns cron ID.

    PREFER ``event_type='cron.message'`` for any "monitor / ping / remind /
    tell me each time" request — one call, no per-fire code, cannot fail
    at runtime. Use ``'arc.dispatch'`` only when each fire must run code
    (e.g. "alert me when price < X").

    Cron's finest granularity is 1 minute. Sub-minute requests round UP
    to ``'* * * * *'`` — reviewer-approved; don't refuse or present options.

    Args:
        name: Unique name for this cron entry.
        cron_expr: e.g. '*/5 * * * *' (every 5 min), '* * * * *' (every min).
        event_type: 'cron.message' or 'arc.dispatch'.
        event_payload: For 'cron.message': {"message": "<non-empty string>"}.
            For 'arc.dispatch': {"arc_id": <int from arc.create()['arc_id']>}.
            The arc MUST itself call messaging.send() in its goal or the
            user sees nothing. conversation_id is auto-injected.
    """
    ...


@tool(local=True, readonly=False, side_effects=True,
      param_types={"name": "Label"})
def remove_cron(name: str) -> bool:
    """Remove a cron entry by name. Returns ``{"removed": True}`` if found.

    This is the ONLY cancel API — no ``cancel_cron`` / ``delete_cron`` /
    ``stop_cron`` exists. No-op (``{"removed": False}``) if name missing.

    Args:
        name: The exact ``name`` originally given to ``add_cron`` /
            ``add_once``. Example:
            ``scheduling.remove_cron(name="s031-monitor-httpbin")``.
    """
    ...


@tool(local=True, readonly=False, side_effects=True)
def list_cron() -> list[dict]:
    """List all cron entries. Useful before ``remove_cron`` to confirm names."""
    ...


@tool(local=True, readonly=False, side_effects=True,
      param_types={"name": "Label"})
def enable_cron(name: str, enabled: bool = True) -> bool:
    """Enable or disable a cron entry."""
    ...
