"""Health monitor heartbeat hook — detects notable model health events and notifies.

Runs alongside scan_for_ready_arcs every ~5s. Checks model_health state
for circuit breaker activations and unhealthy models.
Sends notifications via notifications.notify() with deduplication.
"""

import logging

from .. import notifications
from .health import ModelHealth, get_all_model_health, get_all_provider_health

logger = logging.getLogger(__name__)


class _MonitorState:
    """In-memory dedup state (resets on restart — acceptable for notification throttling)."""

    __slots__ = (
        "notified_circuits",
        "notified_unhealthy",
        "notified_provider_down",
    )

    def __init__(self):
        self.notified_circuits: set[str] = set()
        self.notified_unhealthy: set[str] = set()
        self.notified_provider_down: set[str] = set()

    def reset(self):
        """Clear all dedup state."""
        self.notified_circuits.clear()
        self.notified_unhealthy.clear()
        self.notified_provider_down.clear()


_state = _MonitorState()


def check_health():
    """Heartbeat hook: detect notable health events and notify.

    Three checks per heartbeat:
    1. Circuit breaker opened — model in CIRCUIT_OPEN not yet notified
    2. Model unhealthy — model in UNHEALTHY not yet notified
    3. Provider outage — all models for a provider are CIRCUIT_OPEN
    """

    try:
        all_health = get_all_model_health()
    except Exception as _exc:  # broad catch: heartbeat hook must never crash
        # Suppression intentional: heartbeat must not crash; surface the failure
        # at INFO so the traceback is visible in normal logs.
        logger.info("health_monitor: failed to fetch model health", exc_info=True)
        return

    current_circuit_open = set()
    current_unhealthy = set()

    for state in all_health:
        model_id = state.model_id

        # Check 1: Circuit breaker opened
        if state.health == ModelHealth.CIRCUIT_OPEN:
            current_circuit_open.add(model_id)
            if model_id not in _state.notified_circuits:
                _state.notified_circuits.add(model_id)
                notifications.notify(
                    f"Circuit breaker OPEN for {model_id} "
                    f"({state.consecutive_failures} consecutive failures)",
                    priority="urgent",
                    category="circuit_breaker",
                )
                logger.warning(
                    "health_monitor: circuit breaker OPEN for %s", model_id
                )

        # Check 2: Model unhealthy
        elif state.health == ModelHealth.UNHEALTHY:
            current_unhealthy.add(model_id)
            if model_id not in _state.notified_unhealthy:
                _state.notified_unhealthy.add(model_id)
                pct = int(state.success_rate * 100)
                notifications.notify(
                    f"Model {model_id} unhealthy (success rate: {pct}%)",
                    priority="normal",
                    category="model_health",
                )
                logger.warning(
                    "health_monitor: model %s unhealthy (success rate: %d%%)",
                    model_id, pct,
                )

    # Clear dedup state for models that have recovered
    recovered_circuits = _state.notified_circuits - current_circuit_open
    for model_id in recovered_circuits:
        _state.notified_circuits.discard(model_id)
        logger.info("health_monitor: circuit breaker recovered for %s", model_id)

    recovered_unhealthy = _state.notified_unhealthy - current_unhealthy
    for model_id in recovered_unhealthy:
        _state.notified_unhealthy.discard(model_id)
        logger.info("health_monitor: model %s recovered from unhealthy", model_id)

    # Check 3: Provider outage — detect when ALL models for a provider are CIRCUIT_OPEN
    try:
        provider_health_list = get_all_provider_health()
    except (KeyError, ValueError, RuntimeError) as _exc:
        logger.debug("health_monitor: failed to fetch provider health", exc_info=True)
        provider_health_list = []

    current_provider_down = set()
    for prov_state in provider_health_list:
        if prov_state.health == ModelHealth.CIRCUIT_OPEN and prov_state.model_count > 0:
            current_provider_down.add(prov_state.provider)
            if prov_state.provider not in _state.notified_provider_down:
                _state.notified_provider_down.add(prov_state.provider)
                notifications.notify(
                    f"Provider outage: all {prov_state.model_count} model(s) for "
                    f"'{prov_state.provider}' are CIRCUIT_OPEN",
                    priority="urgent",
                    category="provider_outage",
                )
                logger.warning(
                    "health_monitor: provider outage for %s (%d models)",
                    prov_state.provider, prov_state.model_count,
                )

    # Clear provider dedup on recovery
    recovered_providers = _state.notified_provider_down - current_provider_down
    for provider in recovered_providers:
        _state.notified_provider_down.discard(provider)
        logger.info("health_monitor: provider %s recovered from outage", provider)


def reset():
    """Reset all dedup state. Mainly for testing."""
    _state.reset()
