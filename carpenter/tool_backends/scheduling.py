"""Scheduling tool backend — wraps trigger_manager for cron management."""
from ..config import get_config
from ..core.engine import trigger_manager

# Built-in default for allowed event types.
_DEFAULT_ALLOWED_EVENT_TYPES = {"cron.message", "arc.dispatch"}


def _get_allowed_event_types() -> set[str]:
    """Return the effective set of allowed scheduling event types.

    Uses config ``scheduling_allowed_event_types`` when non-empty,
    otherwise falls back to the built-in default.
    """
    override = get_config("scheduling_allowed_event_types", [])
    if override:
        return set(override)
    return set(_DEFAULT_ALLOWED_EVENT_TYPES)


# Module-level alias for backward compatibility with imports (e.g. tests).
ALLOWED_EVENT_TYPES = _DEFAULT_ALLOWED_EVENT_TYPES


def _validate_event_type(event_type: str) -> None:
    """Raise ValueError if event_type is not a registered handler."""
    allowed = _get_allowed_event_types()
    if event_type not in allowed:
        allowed_str = ", ".join(sorted(allowed))
        raise ValueError(
            f"Invalid event_type '{event_type}'. Must be one of: {allowed_str}"
        )


def _merge_context(params: dict) -> dict | None:
    """Merge conversation_id (auto-injected by callback) into event_payload."""
    event_payload = params.get("event_payload") or {}
    if "conversation_id" in params:
        event_payload["conversation_id"] = params["conversation_id"]
    return event_payload if event_payload else None


def handle_add_once(params: dict) -> dict:
    """Add a one-shot trigger. Params: name, at_iso, event_type, event_payload (opt)."""
    _validate_event_type(params["event_type"])
    cron_id = trigger_manager.add_once(
        name=params["name"],
        at_iso=params["at_iso"],
        event_type=params["event_type"],
        event_payload=_merge_context(params),
    )
    return {"cron_id": cron_id}


def handle_add_cron(params: dict) -> dict:
    """Add a cron entry. Params: name, cron_expr, event_type, event_payload (opt)."""
    _validate_event_type(params["event_type"])
    cron_id = trigger_manager.add_cron(
        name=params["name"],
        cron_expr=params["cron_expr"],
        event_type=params["event_type"],
        event_payload=_merge_context(params),
    )
    return {"cron_id": cron_id}


def handle_remove_cron(params: dict) -> dict:
    """Remove a cron entry. Params: name."""
    removed = trigger_manager.remove_cron(params["name"])
    return {"removed": removed}


def handle_list_cron(params: dict) -> dict:
    """List all cron entries."""
    entries = trigger_manager.list_cron()
    return {"entries": entries}


def handle_enable_cron(params: dict) -> dict:
    """Enable/disable a cron entry. Params: name, enabled."""
    found = trigger_manager.enable_cron(params["name"], params.get("enabled", True))
    return {"found": found}
