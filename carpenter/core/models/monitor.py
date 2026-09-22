"""Health monitor heartbeat hook — detects notable model health events and notifies.

Runs alongside scan_for_ready_arcs every ~5s. Checks model_health state
for circuit breaker activations and unhealthy models.
Sends notifications via notifications.notify() with deduplication.

Dedup state lives in the ``health_notify_state`` table, not just in memory,
so a restart cannot re-announce a situation the user has already been told
about. Health itself is derived from the persistent ``model_calls`` window,
so a model that was circuit-open before a restart is still circuit-open
after one; with in-memory-only dedup that produced a fresh "model is down"
alert on every restart, for weeks, with nothing having changed. An alert
fires again only after the model recovers and then fails anew.
"""

import logging
import sqlite3
from datetime import datetime, timezone

from .. import notifications
from ...db import db_transaction
from .health import ModelHealth, get_all_model_health, get_all_provider_health

logger = logging.getLogger(__name__)

# Dedup ``kind`` values in health_notify_state (one keyspace per check).
_KIND_CIRCUIT = "circuit_open"
_KIND_UNHEALTHY = "unhealthy"
_KIND_PROVIDER_DOWN = "provider_down"


class _MonitorState:
    """Dedup state, mirrored from health_notify_state so restarts stay quiet."""

    __slots__ = (
        "notified_circuits",
        "notified_unhealthy",
        "notified_provider_down",
        "loaded",
    )

    def __init__(self):
        self.notified_circuits: set[str] = set()
        self.notified_unhealthy: set[str] = set()
        self.notified_provider_down: set[str] = set()
        self.loaded: bool = False

    def reset(self):
        """Clear all dedup state, in memory and on disk."""
        self.notified_circuits.clear()
        self.notified_unhealthy.clear()
        self.notified_provider_down.clear()
        self.loaded = False
        try:
            with db_transaction() as db:
                db.execute("DELETE FROM health_notify_state")
        except sqlite3.Error:
            logger.debug("health_monitor: could not clear dedup state", exc_info=True)


_state = _MonitorState()


def _sets_by_kind() -> dict[str, set[str]]:
    return {
        _KIND_CIRCUIT: _state.notified_circuits,
        _KIND_UNHEALTHY: _state.notified_unhealthy,
        _KIND_PROVIDER_DOWN: _state.notified_provider_down,
    }


def _load_state() -> None:
    """Hydrate the in-memory sets from the database, once per process.

    A failure here leaves ``loaded`` False so the next heartbeat retries;
    until it succeeds the monitor behaves as it did before — dedup within
    the process only.
    """
    if _state.loaded:
        return
    try:
        with db_transaction() as db:
            rows = db.execute(
                "SELECT kind, key FROM health_notify_state"
            ).fetchall()
    except sqlite3.Error:
        logger.debug("health_monitor: could not load dedup state", exc_info=True)
        return

    by_kind = _sets_by_kind()
    for row in rows:
        target = by_kind.get(row["kind"])
        if target is not None:
            target.add(row["key"])
    _state.loaded = True


def _remember(kind: str, key: str) -> None:
    """Record that the user has been notified about (kind, key)."""
    try:
        with db_transaction() as db:
            db.execute(
                "INSERT INTO health_notify_state (kind, key, notified_at) "
                "VALUES (?, ?, ?) ON CONFLICT(kind, key) DO NOTHING",
                (kind, key, datetime.now(timezone.utc).isoformat()),
            )
    except sqlite3.Error:
        logger.debug(
            "health_monitor: could not persist dedup state for %s/%s",
            kind, key, exc_info=True,
        )


def _forget(kind: str, key: str) -> None:
    """Drop the dedup record so a future recurrence notifies again."""
    try:
        with db_transaction() as db:
            db.execute(
                "DELETE FROM health_notify_state WHERE kind = ? AND key = ?",
                (kind, key),
            )
    except sqlite3.Error:
        logger.debug(
            "health_monitor: could not clear dedup state for %s/%s",
            kind, key, exc_info=True,
        )


def check_health():
    """Heartbeat hook: detect notable health events and notify.

    Three checks per heartbeat:
    1. Circuit breaker opened — model in CIRCUIT_OPEN not yet notified
    2. Model unhealthy — model in UNHEALTHY not yet notified
    3. Provider outage — all models for a provider are CIRCUIT_OPEN
    """
    _load_state()

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
                _remember(_KIND_CIRCUIT, model_id)
                notifications.notify(
                    _circuit_open_message(state),
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
                _remember(_KIND_UNHEALTHY, model_id)
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
        _forget(_KIND_CIRCUIT, model_id)
        logger.info("health_monitor: circuit breaker recovered for %s", model_id)

    recovered_unhealthy = _state.notified_unhealthy - current_unhealthy
    for model_id in recovered_unhealthy:
        _state.notified_unhealthy.discard(model_id)
        _forget(_KIND_UNHEALTHY, model_id)
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
                _remember(_KIND_PROVIDER_DOWN, prov_state.provider)
                notifications.notify(
                    f"All {prov_state.model_count} {prov_state.provider} model(s) "
                    f"Carpenter uses are failing every request, so work that needs "
                    f"{prov_state.provider} will fail until one of them succeeds "
                    f"again. See the preceding model alerts for the reason.",
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
        _forget(_KIND_PROVIDER_DOWN, provider)
        logger.info("health_monitor: provider %s recovered from outage", provider)


def _circuit_open_message(state) -> str:
    """Explain an open circuit breaker in terms of what the user will notice."""
    message = (
        f"Model {state.model_id} has failed its last "
        f"{state.consecutive_failures} requests in a row, so Carpenter now "
        f"treats it as down: tasks that fail on it switch straight to another "
        f"model (or fail) instead of retrying. This clears by itself once a "
        f"request to it succeeds."
    )
    if state.last_error:
        message += f"\n\nLast error: {state.last_error}"
    return message


def reset():
    """Reset all dedup state. Mainly for testing."""
    _state.reset()
