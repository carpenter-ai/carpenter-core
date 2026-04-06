"""Scheduling tools. Tier 1: callback to platform (delayed execution)."""
from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_types={"name": "Label", "event_type": "Label"})
def add_once(name: str, at_iso: str, event_type: str, event_payload: dict | None = None) -> int:
    """Add a one-shot trigger that fires once at the given ISO timestamp.

    After firing, the entry is automatically deleted. Returns cron ID.

    Args:
        name: Unique name for this trigger.
        at_iso: ISO datetime string for when to fire.
            Use LOCAL time without timezone suffix — the platform converts
            to UTC automatically.  Example: '2026-04-05T14:30:00'
            (fires at 2:30 PM local time).
            Do NOT append 'Z' or '+00:00' unless you mean UTC.
        event_type: Must be 'cron.message' or 'arc.dispatch'.
            Use 'cron.message' for simple message delivery (with event_payload
            containing {"message": "..."}). conversation_id is auto-injected.
            Use 'arc.dispatch' to execute an arc (with event_payload
            containing {"arc_id": <id>}).
        event_payload: Payload dict — required keys depend on event_type.
    """
    result = callback("scheduling.add_once", {
        "name": name, "at_iso": at_iso,
        "event_type": event_type, "event_payload": event_payload,
    })
    return result["cron_id"]


@tool(local=True, readonly=False, side_effects=True,
      param_types={"name": "Label", "event_type": "Label"})
def add_cron(name: str, cron_expr: str, event_type: str, event_payload: dict | None = None) -> int:
    """Add a recurring cron entry. Returns cron ID.

    Args:
        name: Unique name for this cron entry.
        cron_expr: Cron expression (e.g. '*/5 * * * *' for every 5 minutes).
        event_type: Must be 'cron.message' or 'arc.dispatch'.
            Use 'cron.message' for simple message delivery (with event_payload
            containing {"message": "..."}). conversation_id is auto-injected.
            Use 'arc.dispatch' to execute an arc (with event_payload
            containing {"arc_id": <id>}).
        event_payload: Payload dict — required keys depend on event_type.
    """
    result = callback("scheduling.add_cron", {
        "name": name, "cron_expr": cron_expr,
        "event_type": event_type, "event_payload": event_payload,
    })
    return result["cron_id"]


@tool(local=True, readonly=False, side_effects=True,
      param_types={"name": "Label"})
def remove_cron(name: str) -> bool:
    """Remove a cron entry by name. Returns True if found."""
    result = callback("scheduling.remove_cron", {"name": name})
    return result["removed"]


@tool(local=True, readonly=False, side_effects=True)
def list_cron() -> list[dict]:
    """List all cron entries."""
    result = callback("scheduling.list_cron", {})
    return result["entries"]


@tool(local=True, readonly=False, side_effects=True,
      param_types={"name": "Label"})
def enable_cron(name: str, enabled: bool = True) -> bool:
    """Enable or disable a cron entry."""
    result = callback("scheduling.enable_cron", {"name": name, "enabled": enabled})
    return result["found"]
