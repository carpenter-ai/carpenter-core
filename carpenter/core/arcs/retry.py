"""Arc-level retry decision logic and state management.

Provides intelligent retry strategies for arc dispatch failures based on error
classification. Tracks per-arc retry budgets and backoff timers in arc_state.

Key concepts:
- ErrorInfo (from error_classifier): Structured error with type, retry_after, etc.
- RetryDecision: Decision on whether/how to retry based on error type and arc state
- Retry state: Stored in arc_state table (_retry_count, _max_retries, _last_error, etc.)
- Backoff strategies: Per-error-type exponential or fixed backoff with jitter
"""

import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ...db import get_db, db_connection, db_transaction
from ...agent.error_classifier import ErrorInfo

logger = logging.getLogger(__name__)


@dataclass
class RetryDecision:
    """Decision on whether/how to retry an arc dispatch.

    Attributes:
        should_retry: True if arc should be retried
        backoff_seconds: How long to wait before retrying
        reason: Human-readable explanation of the decision
        escalate_on_exhaust: Whether to escalate if retries exhausted
    """
    should_retry: bool
    backoff_seconds: float
    reason: str
    escalate_on_exhaust: bool


# Hardcoded fallback defaults — overridable via config["arc_retry"] keys:
#   arc_retry.max_retries      -> _DEFAULT_ERROR_MAX_RETRIES
#   arc_retry.escalate_on_exhaust -> _DEFAULT_ERROR_ESCALATE_ON_EXHAUST
#   arc_retry.backoff_caps     -> _DEFAULT_BACKOFF_CAPS
_DEFAULT_ERROR_MAX_RETRIES = {
    "RateLimitError": 5,
    "APIOutageError": 4,
    "NetworkError": 3,
    "UnknownError": 2,
    "VerificationError": 2,
    "AuthError": 0,
    "ModelError": 0,
    "ClientError": 0,
}

_DEFAULT_ERROR_ESCALATE_ON_EXHAUST = {
    "APIOutageError": True,
    "ModelError": True,
    "RateLimitError": False,
    "NetworkError": False,
    "AuthError": False,
    "ClientError": False,
    "UnknownError": False,
    "VerificationError": False,
}

_DEFAULT_BACKOFF_CAPS = {
    "RateLimitError": 600,    # 10 min
    "APIOutageError": 300,    # 5 min
    "NetworkError": 60,       # 1 min
    "UnknownError": 120,      # 2 min
    "VerificationError": 0,   # immediate retry (no backoff)
}


def _load_config_dict(config_key: str, defaults: dict) -> dict:
    """Load a config dict from arc_retry config, merging with defaults.

    Reads ``config["arc_retry"][config_key]``, merges its entries (excluding
    any "default" key) over the provided ``defaults``, and returns the result.

    Args:
        config_key: Sub-key under ``arc_retry`` to read (e.g. "max_retries").
        defaults: Hardcoded default dict to merge config overrides into.

    Returns:
        Merged dict with config overrides applied on top of defaults.
    """
    from ...config import get_config
    arc_retry_config = get_config("arc_retry", {})
    config_overrides = arc_retry_config.get(config_key, {})
    merged = dict(defaults)
    for k, v in config_overrides.items():
        if k != "default":
            merged[k] = v
    return merged


def _get_error_max_retries() -> dict:
    """Return error-type max retries, merging config overrides with defaults."""
    return _load_config_dict("max_retries", _DEFAULT_ERROR_MAX_RETRIES)


def _get_escalate_on_exhaust() -> dict:
    """Return escalation flags, merging config overrides with defaults."""
    return _load_config_dict("escalate_on_exhaust", _DEFAULT_ERROR_ESCALATE_ON_EXHAUST)


def _get_backoff_caps() -> dict:
    """Return backoff caps, merging config overrides with defaults."""
    return _load_config_dict("backoff_caps", _DEFAULT_BACKOFF_CAPS)


def should_retry_arc(arc_id: int, error_info: ErrorInfo) -> RetryDecision:
    """Decide if arc should retry based on error type and retry budget.

    Decision process:
    1. Load arc retry state (_retry_count, _max_retries, _retry_policy)
    2. Check error_info.type against retry eligibility matrix
    3. Check retry_count < max_retries
    4. Calculate backoff based on error type + attempt number
    5. Return RetryDecision with recommendation

    Args:
        arc_id: The arc that failed
        error_info: Structured error information from error_classifier

    Returns:
        RetryDecision indicating whether to retry and with what backoff
    """
    # Load current retry state
    state = get_retry_state(arc_id)
    retry_count = state.get("_retry_count", 0)
    arc_max_retries = state.get("_max_retries")

    # Load config-aware mappings
    error_max_retries = _get_error_max_retries()
    escalate_map = _get_escalate_on_exhaust()

    # Get error-type-specific max retries
    error_max = error_max_retries.get(error_info.type)

    # Check if error type is retriable
    if error_max == 0:
        # Non-retriable error
        escalate = escalate_map.get(error_info.type, False)
        reason = f"{error_info.type} is not retriable"
        return RetryDecision(
            should_retry=False,
            backoff_seconds=0,
            reason=reason,
            escalate_on_exhaust=escalate,
        )

    # Determine effective max_retries (use minimum of arc limit and error-type limit)
    if arc_max_retries is None:
        # No arc-specific limit, use error-type limit or config default
        if error_max is not None:
            max_retries = error_max
        else:
            from ...config import get_config
            arc_retry_config = get_config("arc_retry", {})
            max_retries = arc_retry_config.get("max_retries", {}).get("default", 3)
    else:
        # Arc has a specific limit, but respect error-type limits if stricter
        if error_max is not None:
            max_retries = min(arc_max_retries, error_max)
        else:
            max_retries = arc_max_retries

    # Check retry budget
    if retry_count >= max_retries:
        escalate = escalate_map.get(error_info.type, False)
        reason = f"Retry budget exhausted ({retry_count}/{max_retries})"
        return RetryDecision(
            should_retry=False,
            backoff_seconds=0,
            reason=reason,
            escalate_on_exhaust=escalate,
        )

    # Calculate backoff (with model health multiplier if available)
    backoff = calculate_backoff(
        error_info.type,
        retry_count,
        error_info.retry_after,
        model_id=error_info.model,
    )

    reason = f"Retrying {error_info.type} (attempt {retry_count + 1}/{max_retries})"
    return RetryDecision(
        should_retry=True,
        backoff_seconds=backoff,
        reason=reason,
        escalate_on_exhaust=False,
    )


def calculate_backoff(
    error_type: str,
    attempt: int,
    retry_after: float | None = None,
    model_id: str | None = None,
) -> float:
    """Calculate backoff seconds based on error type and attempt number.

    Strategies:
    - RateLimitError: max(10, retry_after) + jitter
    - APIOutageError: min(2^attempt, 300) + jitter  # 5 min cap
    - NetworkError: min(2^attempt, 60) + jitter     # 1 min cap
    - UnknownError: 5 + jitter                       # fixed 5s

    Backoff is multiplied by model health multiplier (1x-4x based on recent failures).

    Args:
        error_type: Error category (RateLimitError, APIOutageError, etc.)
        attempt: Current retry attempt (0-based)
        retry_after: Optional retry-after header value (seconds)
        model_id: Optional model ID for adaptive backoff

    Returns:
        Backoff duration in seconds with jitter and health multiplier
    """
    # Load config for backoff parameters
    from ...config import get_config
    arc_retry_config = get_config("arc_retry", {})
    jitter_percent = arc_retry_config.get("jitter_percent", 10)
    backoff_base = arc_retry_config.get("backoff_base", 2)
    caps = _get_backoff_caps()

    if error_type == "RateLimitError":
        # Use retry_after header if provided, else default to 10s
        base = max(10.0, retry_after or 10.0)
    elif error_type in ("APIOutageError", "NetworkError"):
        # Exponential backoff with cap
        cap = caps.get(error_type, caps.get("default", 120))
        base = min(backoff_base ** attempt, cap)
    elif error_type == "VerificationError":
        # Immediate retry — no backoff needed for verification reworks
        base = 0.0
    elif error_type == "UnknownError":
        # Fixed 5s backoff
        base = 5.0
    else:
        # Default exponential with 2 min cap
        cap = caps.get("default", 120)
        base = min(backoff_base ** attempt, cap)

    # Apply model health multiplier (Phase 3: adaptive backoff)
    if model_id:
        try:
            from ..models import health as model_health
            multiplier = model_health.get_backoff_multiplier(model_id)
            base *= multiplier
        except (ImportError, KeyError, ValueError) as _exc:
            pass  # Fallback to base backoff if model_health unavailable

    # Add jitter (±jitter_percent)
    jitter = base * (jitter_percent / 100.0) * (random.random() * 2 - 1)
    return max(1.0, base + jitter)


def record_retry_attempt(
    arc_id: int,
    error_info: ErrorInfo,
    backoff_seconds: float,
) -> None:
    """Update arc_state with retry metadata.

    Updates:
    1. Increment _retry_count
    2. Store _last_error (ErrorInfo.to_json())
    3. Set _last_attempt_at (now)
    4. Set _backoff_until (now + backoff_seconds)
    5. Create arc_history entry (type='retry_attempt', content_json=...)

    Args:
        arc_id: The arc being retried
        error_info: Error information from the failed attempt
        backoff_seconds: How long to wait before next attempt
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    backoff_until = now + timedelta(seconds=backoff_seconds)

    try:
        # Get current retry count
        current_count = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = '_retry_count'",
            (arc_id,),
        ).fetchone()

        new_count = 1
        if current_count:
            try:
                new_count = int(current_count["value_json"]) + 1
            except (ValueError, TypeError):
                new_count = 1

        # Update retry state
        _set_arc_state(db, arc_id, "_retry_count", new_count)
        _set_arc_state(db, arc_id, "_last_error", error_info.to_json())
        _set_arc_state(db, arc_id, "_last_attempt_at", now.isoformat())
        _set_arc_state(db, arc_id, "_backoff_until", backoff_until.isoformat())

        # Set first_error_at if not already set
        first_error = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = '_first_error_at'",
            (arc_id,),
        ).fetchone()
        if not first_error:
            _set_arc_state(db, arc_id, "_first_error_at", now.isoformat())

        # Log to arc_history
        db.execute(
            "INSERT INTO arc_history (arc_id, entry_type, content_json) "
            "VALUES (?, 'retry_attempt', ?)",
            (arc_id, json.dumps({
                "retry_count": new_count,
                "error_type": error_info.type,
                "backoff_seconds": backoff_seconds,
                "backoff_until": backoff_until.isoformat(),
                "error_message": error_info.message,
            })),
        )

        db.commit()
        logger.info(
            "Arc %d retry attempt %d recorded (backoff: %.1fs until %s)",
            arc_id, new_count, backoff_seconds, backoff_until.isoformat()
        )
    finally:
        db.close()


def get_retry_state(arc_id: int) -> dict:
    """Load all retry-related arc_state keys for an arc.

    Returns dict with keys: _retry_count, _max_retries, _last_error,
    _last_attempt_at, _backoff_until, _first_error_at, _retry_policy

    Returns:
        Dict with retry state (empty dict if arc has no retry state)
    """
    with db_connection() as db:
        rows = db.execute(
            "SELECT key, value_json FROM arc_state WHERE arc_id = ? "
            "AND key LIKE '\\_%' ESCAPE '\\'",
            (arc_id,),
        ).fetchall()

        result = {}
        for row in rows:
            key = row["key"]
            value_json = row["value_json"]
            try:
                # Try to parse as JSON first
                value = json.loads(value_json)
            except (json.JSONDecodeError, TypeError):
                # Fall back to raw string
                value = value_json

            result[key] = value

        return result


def initialize_retry_state(
    arc_id: int,
    retry_policy: str = "transient_only",
    max_retries: int | None = None,
) -> None:
    """Initialize retry state when arc is created.

    Sets default _max_retries based on retry_policy:
    - "transient_only": 3
    - "aggressive": 5
    - "conservative": 2

    Args:
        arc_id: The arc to initialize
        retry_policy: Policy name (transient_only, aggressive, conservative)
        max_retries: Optional explicit max_retries (overrides policy default)
    """
    # Load config for policy defaults
    from ...config import get_config
    arc_retry_config = get_config("arc_retry", {})

    if max_retries is None:
        # Determine max_retries from policy
        policy_defaults = {
            "transient_only": 3,
            "aggressive": 5,
            "conservative": 2,
        }
        max_retries = policy_defaults.get(retry_policy, 3)

    # Check if arc_retry is enabled
    if not arc_retry_config.get("enabled", True):
        max_retries = 0

    with db_transaction() as db:
        _set_arc_state(db, arc_id, "_retry_count", 0)
        _set_arc_state(db, arc_id, "_max_retries", max_retries)
        _set_arc_state(db, arc_id, "_retry_policy", retry_policy)

        # Determine escalation policy
        default_policy = arc_retry_config.get("default_policy", "transient_only")
        escalate = retry_policy == "aggressive"
        _set_arc_state(db, arc_id, "_escalation_on_exhaust", escalate)

        logger.debug(
            "Arc %d retry state initialized (policy=%s, max_retries=%d)",
            arc_id, retry_policy, max_retries
        )


def _set_arc_state(db, arc_id: int, key: str, value) -> None:
    """Helper to set an arc_state key-value pair.

    Args:
        db: Database connection
        arc_id: The arc ID
        key: State key
        value: State value (will be JSON-encoded if not a string)
    """
    if isinstance(value, str):
        value_json = json.dumps(value)
    elif isinstance(value, (int, float, bool)):
        value_json = json.dumps(value)
    elif isinstance(value, dict):
        value_json = json.dumps(value)
    else:
        value_json = json.dumps(str(value))

    db.execute(
        "INSERT INTO arc_state (arc_id, key, value_json) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(arc_id, key) DO UPDATE SET value_json = excluded.value_json, "
        "updated_at = CURRENT_TIMESTAMP",
        (arc_id, key, value_json),
    )
