"""Provider error detection, model failover, and model-health recording.

Supports the dispatch handler's failover path: when the primary model
fails with a provider-connectivity error, try the ranked list of
fallback models selected from the arc's model policy.
"""

import logging

from ..models import health as model_health

logger = logging.getLogger(__name__)


# Error types that indicate provider unavailability (transient/connectivity).
# These map to error_classifier.classify_error() output types.
_PROVIDER_ERROR_TYPES = frozenset({
    "NetworkError",       # ConnectError, TimeoutException (from error_classifier)
    "APIOutageError",     # HTTP 5xx, CircuitBreakerError (from error_classifier)
    "ConnectionError",    # Raw exception type name
    "ConnectTimeout",     # Raw exception type name
    "TimeoutError",       # Raw exception type name
    "ConnectError",       # Raw exception type name (httpx)
    "ServerError",        # Generic server error
    "ServiceUnavailable", # HTTP 503
})


def _is_provider_error(error_info) -> bool:
    """Check if an error indicates provider unavailability.

    Returns True for connection errors, timeouts, and server errors
    that suggest the provider is offline or unreachable. Returns False
    for client errors (4xx), rate limits, auth failures, etc. that
    would likely affect all providers or are not transient.
    """
    if error_info is None:
        return False
    error_type = getattr(error_info, "type", "") or ""
    # Direct match against known provider error types
    if error_type in _PROVIDER_ERROR_TYPES:
        return True
    # Heuristic: check for connection/timeout in the error type name
    lower = error_type.lower()
    return any(kw in lower for kw in ("connect", "timeout", "unavailable", "unreachable"))


def _record_model_call(
    model_id: str | None,
    success: bool,
    error_type: str | None = None,
) -> None:
    """Record a model call outcome for health tracking.

    Swallows ImportError/KeyError/ValueError so callers don't need their
    own try/except. No-op if model_id is falsy.
    """
    if not model_id:
        return
    try:
        model_health.record_model_call(
            model_id=model_id,
            success=success,
            error_type=error_type,
        )
    except (ImportError, KeyError, ValueError):
        pass  # Don't fail dispatch over health tracking


async def _try_fallback_models(
    arc_id: int,
    fallback_models: list,
    original_agent_config: dict | None,
    original_error_info,
) -> bool:
    """Try fallback models after primary model fails with a provider error.

    Iterates through the remaining ranked models. For each, attempts to
    invoke the agent. On success, records the model call as successful
    and completes the arc. On provider error, records the failure and
    continues to the next fallback.

    Args:
        arc_id: The arc being dispatched.
        fallback_models: List of SelectionResult alternatives (score-descending).
        original_agent_config: The model policy dict from the primary attempt.
        original_error_info: ErrorInfo from the primary failure.

    Returns:
        True if a fallback model succeeded, False if all fallbacks failed.
    """
    from . import manager as arc_manager
    from .dispatch_handler import (
        _find_arc_conversation,
        _run_arc_agent,
        _propagate_completion,
        _extract_error_info,
    )

    if not fallback_models or original_agent_config is None:
        return False

    arc_info = arc_manager.get_arc(arc_id)
    if not arc_info:
        return False

    goal = arc_info.get("goal") or arc_info.get("name") or f"Arc #{arc_id}"
    conv_id = _find_arc_conversation(arc_id)
    if not conv_id:
        return False

    for fallback in fallback_models:
        fallback_config = dict(original_agent_config)
        fallback_config["model"] = fallback.model_id

        logger.info(
            "Arc %d: trying fallback model %s (%s)",
            arc_id, fallback.model_key, fallback.reason,
        )

        try:
            await _run_arc_agent(
                arc_id, goal, conv_id, agent_config=fallback_config,
            )

            # Fallback succeeded — record success and complete the arc
            _record_model_call(fallback.model_id, success=True)

            arc_manager.freeze_arc(arc_id)
            _propagate_completion(arc_id)

            logger.info(
                "Arc %d: fallback model %s succeeded",
                arc_id, fallback.model_key,
            )
            return True

        except Exception as fb_exc:
            fb_error = _extract_error_info(arc_id, fb_exc)

            # Record the fallback model failure
            _record_model_call(
                fallback.model_id, success=False, error_type=fb_error.type,
            )

            if _is_provider_error(fb_error):
                logger.warning(
                    "Arc %d: fallback model %s also failed (provider error: %s), "
                    "trying next",
                    arc_id, fallback.model_key, fb_error.type,
                    exc_info=True,
                )
                continue
            else:
                # Non-provider error (e.g., auth, rate limit, content filter)
                # — don't continue failover, let normal retry logic handle it
                logger.warning(
                    "Arc %d: fallback model %s failed with non-provider error "
                    "(%s), stopping failover",
                    arc_id, fallback.model_key, fb_error.type,
                    exc_info=True,
                )
                return False

    logger.warning(
        "Arc %d: all %d fallback models exhausted",
        arc_id, len(fallback_models),
    )
    return False
