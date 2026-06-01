"""Scheduling tool backend — wraps trigger_manager for cron management."""
import logging

from ..config import get_config
from ..core.engine import trigger_manager

logger = logging.getLogger(__name__)

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


def _normalize_event_payload(event_type: str, event_payload: dict | None) -> dict | None:
    """Validate and normalize event_payload for known event types.

    Catches common agent-side mistakes BEFORE the cron is persisted so the
    error is visible to the chat agent (and the cron isn't silently broken
    at fire time).

    - ``arc.dispatch``: requires ``event_payload["arc_id"]`` to be a positive
      int.  Agents often paste the raw ``arc.create()`` return value (a
      ``{"arc_id": <int>}`` dict) producing a nested
      ``{"arc_id": {"arc_id": <int>}}``.  Unwrap one level with a warning
      to keep the cron functional, but raise on anything else.
    - ``cron.message``: requires ``event_payload["message"]`` to be a
      non-empty string.  Empty messages silently deliver nothing and look
      identical to a broken cron, so reject up front.
    """
    payload = event_payload or {}
    if event_type == "arc.dispatch":
        arc_id = payload.get("arc_id")
        if isinstance(arc_id, dict) and "arc_id" in arc_id and isinstance(arc_id["arc_id"], int):
            logger.warning(
                "scheduling.add_cron: unwrapping nested {'arc_id': {'arc_id': %d}} — "
                "caller passed raw arc.create() result. Use result['arc_id'].",
                arc_id["arc_id"],
            )
            payload = {**payload, "arc_id": arc_id["arc_id"]}
        elif not isinstance(arc_id, int) or arc_id <= 0:
            raise ValueError(
                "scheduling.add_cron with event_type='arc.dispatch' "
                "requires event_payload={'arc_id': <int>}. Got: "
                f"{arc_id!r}. arc.create() returns a dict; pass "
                "result['arc_id']."
            )
    elif event_type == "cron.message":
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError(
                "scheduling.add_cron with event_type='cron.message' "
                "requires a non-empty event_payload={'message': <str>}. "
                f"Got: {message!r}. Empty messages deliver nothing and look "
                "like a broken cron."
            )
    return payload if payload else None


def handle_add_once(params: dict) -> dict:
    """Add a one-shot trigger. Params: name, at_iso, event_type, event_payload (opt)."""
    _validate_event_type(params["event_type"])
    payload = _normalize_event_payload(params["event_type"], _merge_context(params))
    cron_id = trigger_manager.add_once(
        name=params["name"],
        at_iso=params["at_iso"],
        event_type=params["event_type"],
        event_payload=payload,
    )
    return {"cron_id": cron_id}


def handle_add_cron(params: dict) -> dict:
    """Add a cron entry. Params: name, cron_expr, event_type, event_payload (opt)."""
    _validate_event_type(params["event_type"])
    payload = _normalize_event_payload(params["event_type"], _merge_context(params))
    cron_id = trigger_manager.add_cron(
        name=params["name"],
        cron_expr=params["cron_expr"],
        event_type=params["event_type"],
        event_payload=payload,
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
