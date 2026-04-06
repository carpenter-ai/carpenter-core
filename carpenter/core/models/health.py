"""Model health tracking and circuit breaker for adaptive retry backoff.

Tracks per-model failure rates and adjusts backoff multipliers dynamically.
Implements circuit breaker pattern to temporarily blacklist unhealthy models.

Health states:
- HEALTHY: Normal operation (success rate >= 80%)
- DEGRADED: Increased failures (50% <= success rate < 80%), 2x backoff
- UNHEALTHY: Severe failures (success rate < 50%), 4x backoff + consider escalation
- CIRCUIT_OPEN: Temporary blacklist (5+ consecutive failures), refuse requests

Sliding window: Last 20 attempts per model, with time decay (older = less weight).
"""

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from ...config import get_config
from ...db import get_db, db_connection, db_transaction

logger = logging.getLogger(__name__)


class ModelHealth(Enum):
    """Model health status."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    CIRCUIT_OPEN = "circuit_open"


@dataclass
class ModelHealthState:
    """Current health state for a model.

    Attributes:
        model_id: Model identifier (e.g., "claude-sonnet-4-5-20250929")
        health: Current health status
        success_rate: Success rate over sliding window (0.0-1.0)
        consecutive_failures: Count of failures since last success
        total_attempts: Total attempts in sliding window
        backoff_multiplier: Multiplier for base backoff (1.0-4.0)
        circuit_open_until: Timestamp when circuit will half-open (None if not open)
        last_success_at: Timestamp of last successful call
        last_failure_at: Timestamp of last failed call
    """
    model_id: str
    health: ModelHealth
    success_rate: float
    consecutive_failures: int
    total_attempts: int
    backoff_multiplier: float
    circuit_open_until: Optional[str] = None
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None


@dataclass
class ProviderHealthState:
    """Aggregated health state for a provider.

    Attributes:
        provider: Provider name (e.g., "anthropic", "ollama")
        health: Aggregated health — CIRCUIT_OPEN only if ALL models are CIRCUIT_OPEN,
                otherwise worst non-circuit state
        model_count: Number of models tracked for this provider
        circuit_open_count: Number of models with CIRCUIT_OPEN
    """
    provider: str
    health: ModelHealth
    model_count: int
    circuit_open_count: int


# In-memory cache of model health states (model_id -> ModelHealthState)
_health_cache: dict[str, ModelHealthState] = {}

# Config keys and their built-in defaults (used only when config is unavailable)
_CONFIG_DEFAULTS: dict[str, int] = {
    "model_health_window_size": 20,
    "circuit_breaker_threshold": 5,
    "circuit_breaker_recovery_seconds": 60,
}


def _get_health_config(key: str) -> int:
    """Return a health config value, falling back to built-in default."""
    return get_config(key, _CONFIG_DEFAULTS[key])


def record_model_call(
    model_id: str,
    success: bool,
    error_type: Optional[str] = None,
) -> None:
    """Record the outcome of a model API call.

    Updates sliding window, recalculates health state, and adjusts backoff multiplier.

    Args:
        model_id: Model identifier
        success: True if call succeeded, False if failed
        error_type: Error type if failed (for analytics)
    """
    now = datetime.now(timezone.utc).isoformat()

    # Extract provider from model_id (e.g., "anthropic:claude-sonnet" → "anthropic")
    if ":" in model_id:
        provider = model_id.split(":", 1)[0]
    else:
        provider = "anthropic"  # default for bare model names

    # Update sliding window in database
    with db_transaction() as db:
        # Insert new call record
        db.execute(
            "INSERT INTO model_calls (model_id, success, error_type, provider, called_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (model_id, 1 if success else 0, error_type, provider, now),
        )

        # Keep only last N calls per model (sliding window)
        db.execute(
            "DELETE FROM model_calls WHERE id IN ("
            "  SELECT id FROM model_calls WHERE model_id = ? "
            "  ORDER BY called_at DESC LIMIT -1 OFFSET ?"
            ")",
            (model_id, _get_health_config("model_health_window_size")),
        )


    # Recalculate health state
    _recalculate_health(model_id, success, now)


def get_model_health(model_id: str) -> ModelHealthState:
    """Get current health state for a model.

    Returns cached state if available, otherwise calculates from database.

    Args:
        model_id: Model identifier

    Returns:
        Current health state
    """
    # Check cache first
    if model_id in _health_cache:
        state = _health_cache[model_id]
        # Invalidate if circuit should half-open
        if state.circuit_open_until:
            open_until = datetime.fromisoformat(state.circuit_open_until)
            if datetime.now(timezone.utc) >= open_until:
                # Circuit half-open, recalculate
                _recalculate_health(model_id, success=None, timestamp=None)
                return _health_cache[model_id]
        return state

    # Calculate from database
    _recalculate_health(model_id, success=None, timestamp=None)
    return _health_cache.get(model_id, ModelHealthState(
        model_id=model_id,
        health=ModelHealth.HEALTHY,
        success_rate=1.0,
        consecutive_failures=0,
        total_attempts=0,
        backoff_multiplier=1.0,
    ))


def get_backoff_multiplier(model_id: str) -> float:
    """Get backoff multiplier for a model based on health.

    Args:
        model_id: Model identifier

    Returns:
        Multiplier (1.0 = healthy, 2.0 = degraded, 4.0 = unhealthy)
    """
    state = get_model_health(model_id)
    return state.backoff_multiplier


def should_circuit_break(model_id: str) -> bool:
    """Check if circuit breaker is open for a model.

    Args:
        model_id: Model identifier

    Returns:
        True if circuit is open (refuse requests)
    """
    state = get_model_health(model_id)
    return state.health == ModelHealth.CIRCUIT_OPEN


def all_cloud_models_circuit_open() -> bool:
    """Check whether ALL cloud models have open circuit breakers.

    A "cloud model" is any model in the registry with cost > 0.
    Returns True only if at least one cloud model is tracked AND all
    tracked cloud models are CIRCUIT_OPEN.

    Returns:
        True if all cloud models are circuit-broken.
    """
    try:
        from .registry import get_registry
    except ImportError:
        return False

    registry = get_registry()
    if not registry:
        return False

    # Collect cloud model IDs (cost > 0)
    cloud_model_ids = set()
    for entry in registry.values():
        if entry.cost_per_mtok_out > 0:
            cloud_model_ids.add(f"{entry.provider}:{entry.model_id}")

    if not cloud_model_ids:
        return False

    # Check how many are tracked and circuit-open
    tracked = 0
    for mid in cloud_model_ids:
        if mid in _health_cache:
            tracked += 1
            if _health_cache[mid].health != ModelHealth.CIRCUIT_OPEN:
                return False

    # Must have at least one tracked cloud model, all must be CIRCUIT_OPEN
    return tracked > 0


def get_provider_health(provider: str) -> ProviderHealthState:
    """Get aggregated health for a provider.

    Provider is CIRCUIT_OPEN only if ALL its models are CIRCUIT_OPEN.
    Otherwise returns the worst non-circuit state
    (UNHEALTHY > DEGRADED > HEALTHY).

    Args:
        provider: Provider name (e.g., "anthropic")

    Returns:
        Aggregated ProviderHealthState.
    """
    # Collect health for all models of this provider
    model_states = [
        state for state in _health_cache.values()
        if _extract_provider(state.model_id) == provider
    ]

    if not model_states:
        return ProviderHealthState(
            provider=provider,
            health=ModelHealth.HEALTHY,
            model_count=0,
            circuit_open_count=0,
        )

    circuit_open_count = sum(
        1 for s in model_states if s.health == ModelHealth.CIRCUIT_OPEN
    )

    if circuit_open_count == len(model_states):
        health = ModelHealth.CIRCUIT_OPEN
    else:
        # Worst non-circuit state
        severity = {
            ModelHealth.HEALTHY: 0,
            ModelHealth.DEGRADED: 1,
            ModelHealth.UNHEALTHY: 2,
            ModelHealth.CIRCUIT_OPEN: -1,  # excluded from worst calc
        }
        worst = ModelHealth.HEALTHY
        for s in model_states:
            if s.health != ModelHealth.CIRCUIT_OPEN:
                if severity[s.health] > severity[worst]:
                    worst = s.health
        health = worst

    return ProviderHealthState(
        provider=provider,
        health=health,
        model_count=len(model_states),
        circuit_open_count=circuit_open_count,
    )


def get_all_provider_health() -> list[ProviderHealthState]:
    """Get health for all providers that have tracked models.

    Returns:
        List of ProviderHealthState, sorted by provider name.
    """
    providers = set()
    for state in _health_cache.values():
        providers.add(_extract_provider(state.model_id))

    return sorted(
        [get_provider_health(p) for p in providers],
        key=lambda s: s.provider,
    )


def _extract_provider(model_id: str) -> str:
    """Extract provider from a model_id string."""
    if ":" in model_id:
        return model_id.split(":", 1)[0]
    return "anthropic"


def _recalculate_health(
    model_id: str,
    success: Optional[bool],
    timestamp: Optional[str],
) -> None:
    """Recalculate health state from sliding window.

    Args:
        model_id: Model identifier
        success: Most recent call result (None if just recalculating)
        timestamp: Timestamp of most recent call (None if just recalculating)
    """
    with db_connection() as db:
        # Get sliding window
        rows = db.execute(
            "SELECT success, error_type, called_at FROM model_calls "
            "WHERE model_id = ? ORDER BY called_at DESC LIMIT ?",
            (model_id, _get_health_config("model_health_window_size")),
        ).fetchall()

        total = len(rows)
        if total == 0:
            # No history, assume healthy
            _health_cache[model_id] = ModelHealthState(
                model_id=model_id,
                health=ModelHealth.HEALTHY,
                success_rate=1.0,
                consecutive_failures=0,
                total_attempts=0,
                backoff_multiplier=1.0,
                last_success_at=timestamp if success else None,
                last_failure_at=timestamp if success is False else None,
            )
            return

        # Calculate success rate
        successes = sum(1 for row in rows if row["success"])
        success_rate = successes / total

        # Count consecutive failures (from most recent)
        consecutive_failures = 0
        for row in rows:
            if row["success"]:
                break
            consecutive_failures += 1

        # Find last success/failure timestamps
        last_success = None
        last_failure = None
        for row in rows:
            if row["success"] and last_success is None:
                last_success = row["called_at"]
            if not row["success"] and last_failure is None:
                last_failure = row["called_at"]
            if last_success and last_failure:
                break

        # Determine health state and backoff multiplier
        if consecutive_failures >= _get_health_config("circuit_breaker_threshold"):
            # Circuit breaker opens
            health = ModelHealth.CIRCUIT_OPEN
            backoff_multiplier = 4.0
            circuit_open_until = (
                datetime.now(timezone.utc) + timedelta(seconds=_get_health_config("circuit_breaker_recovery_seconds"))
            ).isoformat()
        elif success_rate < 0.5:
            # Unhealthy: severe failures
            health = ModelHealth.UNHEALTHY
            backoff_multiplier = 4.0
            circuit_open_until = None
        elif success_rate < 0.8:
            # Degraded: increased failures
            health = ModelHealth.DEGRADED
            backoff_multiplier = 2.0
            circuit_open_until = None
        else:
            # Healthy
            health = ModelHealth.HEALTHY
            backoff_multiplier = 1.0
            circuit_open_until = None

        # Cache the state
        _health_cache[model_id] = ModelHealthState(
            model_id=model_id,
            health=health,
            success_rate=success_rate,
            consecutive_failures=consecutive_failures,
            total_attempts=total,
            backoff_multiplier=backoff_multiplier,
            circuit_open_until=circuit_open_until,
            last_success_at=last_success or (timestamp if success else None),
            last_failure_at=last_failure or (timestamp if success is False else None),
        )

        logger.debug(
            "Model %s health: %s (success_rate=%.2f, consecutive_failures=%d, backoff_multiplier=%.1fx)",
            model_id, health.value, success_rate, consecutive_failures, backoff_multiplier,
        )



def get_all_model_health() -> list[ModelHealthState]:
    """Get health states for all tracked models.

    Returns:
        List of health states, sorted by model_id
    """
    with db_connection() as db:
        # Get distinct models
        rows = db.execute(
            "SELECT DISTINCT model_id FROM model_calls ORDER BY model_id"
        ).fetchall()

        result = []
        for row in rows:
            model_id = row["model_id"]
            state = get_model_health(model_id)
            result.append(state)

        return result


def reset_circuit_breaker(model_id: str) -> None:
    """Manually reset circuit breaker for a model.

    Useful for testing or when you know a model is back online.

    Args:
        model_id: Model identifier
    """
    if model_id in _health_cache:
        del _health_cache[model_id]
    _recalculate_health(model_id, success=None, timestamp=None)
    logger.info("Circuit breaker reset for model %s", model_id)


def cleanup_old_calls(days: int = 7) -> int:
    """Remove model call records older than N days.

    Args:
        days: Age threshold in days

    Returns:
        Number of records deleted
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with db_transaction() as db:
        cursor = db.execute(
            "DELETE FROM model_calls WHERE called_at < ?",
            (cutoff,),
        )
        deleted = cursor.rowcount
        logger.info("Cleaned up %d old model call records (older than %d days)", deleted, days)
        return deleted
