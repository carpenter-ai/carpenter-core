"""Tests for health_monitor heartbeat hook."""

import time
import pytest
from unittest.mock import patch, MagicMock

from carpenter.core.models import monitor as health_monitor
from carpenter.core.models.health import (
    ModelHealth,
    ModelHealthState,
    ProviderHealthState,
)


@pytest.fixture(autouse=True)
def reset_monitor_state():
    """Reset dedup state before each test."""
    health_monitor.reset()
    yield
    health_monitor.reset()


def _make_state(model_id, health, **kwargs):
    """Helper to build a ModelHealthState."""
    defaults = {
        "model_id": model_id,
        "health": health,
        "success_rate": 1.0,
        "consecutive_failures": 0,
        "total_attempts": 10,
        "backoff_multiplier": 1.0,
    }
    defaults.update(kwargs)
    return ModelHealthState(**defaults)


@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_circuit_breaker_notification(mock_notif, mock_health):
    """Circuit breaker open triggers urgent notification."""
    mock_health.return_value = [
        _make_state("claude-sonnet", ModelHealth.CIRCUIT_OPEN,
                    consecutive_failures=5, success_rate=0.0),
    ]

    health_monitor.check_health()

    mock_notif.notify.assert_called_once()
    call_args = mock_notif.notify.call_args
    assert "Circuit breaker OPEN" in call_args[0][0]
    assert "claude-sonnet" in call_args[0][0]
    assert call_args[1]["priority"] == "urgent"
    assert call_args[1]["category"] == "circuit_breaker"


@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_circuit_breaker_dedup(mock_notif, mock_health):
    """Same circuit breaker should not notify twice."""
    mock_health.return_value = [
        _make_state("claude-sonnet", ModelHealth.CIRCUIT_OPEN,
                    consecutive_failures=5, success_rate=0.0),
    ]

    health_monitor.check_health()
    health_monitor.check_health()

    assert mock_notif.notify.call_count == 1


@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_circuit_breaker_recovery_clears_dedup(mock_notif, mock_health):
    """When circuit breaker recovers, dedup state is cleared for re-notification."""
    # First: circuit open
    mock_health.return_value = [
        _make_state("claude-sonnet", ModelHealth.CIRCUIT_OPEN,
                    consecutive_failures=5, success_rate=0.0),
    ]
    health_monitor.check_health()
    assert mock_notif.notify.call_count == 1

    # Recovered: model is now healthy
    mock_health.return_value = [
        _make_state("claude-sonnet", ModelHealth.HEALTHY),
    ]
    health_monitor.check_health()

    # Circuit opens again — should notify again
    mock_health.return_value = [
        _make_state("claude-sonnet", ModelHealth.CIRCUIT_OPEN,
                    consecutive_failures=5, success_rate=0.0),
    ]
    health_monitor.check_health()
    assert mock_notif.notify.call_count == 2


@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_unhealthy_notification(mock_notif, mock_health):
    """Unhealthy model triggers normal notification."""
    mock_health.return_value = [
        _make_state("claude-haiku", ModelHealth.UNHEALTHY,
                    success_rate=0.3),
    ]

    health_monitor.check_health()

    mock_notif.notify.assert_called_once()
    call_args = mock_notif.notify.call_args
    assert "unhealthy" in call_args[0][0]
    assert "30%" in call_args[0][0]
    assert call_args[1]["priority"] == "normal"
    assert call_args[1]["category"] == "model_health"


@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_unhealthy_dedup(mock_notif, mock_health):
    """Same unhealthy model should not notify twice."""
    mock_health.return_value = [
        _make_state("claude-haiku", ModelHealth.UNHEALTHY, success_rate=0.3),
    ]

    health_monitor.check_health()
    health_monitor.check_health()

    assert mock_notif.notify.call_count == 1


@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_unhealthy_recovery_clears_dedup(mock_notif, mock_health):
    """When model recovers from unhealthy, dedup state is cleared."""
    mock_health.return_value = [
        _make_state("claude-haiku", ModelHealth.UNHEALTHY, success_rate=0.3),
    ]
    health_monitor.check_health()
    assert mock_notif.notify.call_count == 1

    # Recovered to degraded
    mock_health.return_value = [
        _make_state("claude-haiku", ModelHealth.DEGRADED, success_rate=0.6),
    ]
    health_monitor.check_health()

    # Unhealthy again — should notify
    mock_health.return_value = [
        _make_state("claude-haiku", ModelHealth.UNHEALTHY, success_rate=0.2),
    ]
    health_monitor.check_health()
    assert mock_notif.notify.call_count == 2


@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_fallback_notification(mock_notif, mock_health):
    """Fallback model triggers throttled notification."""
    mock_health.return_value = [
        _make_state("fallback:local-llama", ModelHealth.HEALTHY,
                    total_attempts=5),
    ]

    health_monitor.check_health()

    mock_notif.notify.assert_called_once()
    call_args = mock_notif.notify.call_args
    assert "fallback active" in call_args[0][0].lower() or "Local fallback active" in call_args[0][0]
    assert call_args[1]["category"] == "local_fallback"


@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_fallback_throttle(mock_notif, mock_health):
    """Fallback notifications are throttled to 5-minute intervals."""
    mock_health.return_value = [
        _make_state("fallback:local-llama", ModelHealth.HEALTHY,
                    total_attempts=5),
    ]

    health_monitor.check_health()
    assert mock_notif.notify.call_count == 1

    # Second call within throttle window — should not notify
    health_monitor.check_health()
    assert mock_notif.notify.call_count == 1


@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_fallback_throttle_expired(mock_notif, mock_health):
    """Fallback notifies again after throttle window expires."""
    mock_health.return_value = [
        _make_state("fallback:local-llama", ModelHealth.HEALTHY,
                    total_attempts=5),
    ]

    health_monitor.check_health()
    assert mock_notif.notify.call_count == 1

    # Simulate throttle window expiry
    health_monitor._state.last_fallback_notify = time.time() - 301

    health_monitor.check_health()
    assert mock_notif.notify.call_count == 2


@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_healthy_model_no_notification(mock_notif, mock_health):
    """Healthy models should not trigger any notification."""
    mock_health.return_value = [
        _make_state("claude-sonnet", ModelHealth.HEALTHY),
    ]

    health_monitor.check_health()

    mock_notif.notify.assert_not_called()


@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_degraded_model_no_notification(mock_notif, mock_health):
    """Degraded models should not trigger notification (only UNHEALTHY and CIRCUIT_OPEN)."""
    mock_health.return_value = [
        _make_state("claude-sonnet", ModelHealth.DEGRADED, success_rate=0.7),
    ]

    health_monitor.check_health()

    mock_notif.notify.assert_not_called()


@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_multiple_models_multiple_notifications(mock_notif, mock_health):
    """Multiple unhealthy models should each trigger their own notification."""
    mock_health.return_value = [
        _make_state("claude-sonnet", ModelHealth.CIRCUIT_OPEN,
                    consecutive_failures=5, success_rate=0.0),
        _make_state("claude-haiku", ModelHealth.UNHEALTHY,
                    success_rate=0.3),
    ]

    health_monitor.check_health()

    assert mock_notif.notify.call_count == 2


@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_get_all_health_exception_handled(mock_notif, mock_health):
    """Exception from get_all_model_health should not crash heartbeat."""
    mock_health.side_effect = Exception("DB error")

    # Should not raise
    health_monitor.check_health()

    mock_notif.notify.assert_not_called()


# ── Cloud recovery notification tests ─────────────────────────────


@patch("carpenter.core.models.monitor.get_all_provider_health")
@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_cloud_recovery_notification(mock_notif, mock_health, mock_prov):
    """Cloud recovery triggers notification after fallback was active."""
    mock_prov.return_value = []

    # First: activate fallback
    mock_health.return_value = [
        _make_state("fallback:local-llama", ModelHealth.HEALTHY, total_attempts=5),
    ]
    health_monitor.check_health()
    assert mock_notif.notify.call_count == 1  # fallback notification

    # Cloud model recovers
    mock_health.return_value = [
        _make_state("anthropic:sonnet", ModelHealth.HEALTHY),
    ]
    health_monitor.check_health()
    assert mock_notif.notify.call_count == 2  # + cloud recovery
    recovery_call = mock_notif.notify.call_args_list[1]
    assert "recovered" in recovery_call[0][0].lower() or "Cloud model recovered" in recovery_call[0][0]
    assert recovery_call[1]["category"] == "cloud_recovery"


@patch("carpenter.core.models.monitor.get_all_provider_health")
@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_cloud_recovery_throttle(mock_notif, mock_health, mock_prov):
    """Cloud recovery notifications are throttled to 5-minute intervals."""
    mock_prov.return_value = []

    # Activate fallback first
    mock_health.return_value = [
        _make_state("fallback:local", ModelHealth.HEALTHY, total_attempts=1),
    ]
    health_monitor.check_health()

    # Cloud recovers
    mock_health.return_value = [
        _make_state("anthropic:sonnet", ModelHealth.HEALTHY),
    ]
    health_monitor.check_health()
    count_after_first = mock_notif.notify.call_count

    # Second check within throttle window — should NOT send another recovery
    health_monitor.check_health()
    assert mock_notif.notify.call_count == count_after_first


@patch("carpenter.core.models.monitor.get_all_provider_health")
@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_cloud_recovery_only_after_fallback(mock_notif, mock_health, mock_prov):
    """Cloud recovery does NOT notify if fallback was never active."""
    mock_prov.return_value = []

    # Healthy cloud model but no prior fallback
    mock_health.return_value = [
        _make_state("anthropic:sonnet", ModelHealth.HEALTHY),
    ]
    health_monitor.check_health()

    # Should NOT have sent cloud_recovery
    for call in mock_notif.notify.call_args_list:
        assert call[1].get("category") != "cloud_recovery"


# ── Provider outage notification tests ──────────────────────────


@patch("carpenter.core.models.monitor.get_all_provider_health")
@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_provider_outage_notification(mock_notif, mock_health, mock_prov):
    """Provider outage triggers urgent notification."""
    mock_health.return_value = []
    mock_prov.return_value = [
        ProviderHealthState(
            provider="anthropic", health=ModelHealth.CIRCUIT_OPEN,
            model_count=3, circuit_open_count=3,
        ),
    ]

    health_monitor.check_health()

    mock_notif.notify.assert_called_once()
    call_args = mock_notif.notify.call_args
    assert "Provider outage" in call_args[0][0]
    assert "anthropic" in call_args[0][0]
    assert call_args[1]["priority"] == "urgent"
    assert call_args[1]["category"] == "provider_outage"


@patch("carpenter.core.models.monitor.get_all_provider_health")
@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_provider_outage_dedup(mock_notif, mock_health, mock_prov):
    """Same provider outage should not notify twice."""
    mock_health.return_value = []
    mock_prov.return_value = [
        ProviderHealthState(
            provider="anthropic", health=ModelHealth.CIRCUIT_OPEN,
            model_count=3, circuit_open_count=3,
        ),
    ]

    health_monitor.check_health()
    health_monitor.check_health()

    assert mock_notif.notify.call_count == 1


@patch("carpenter.core.models.monitor.get_all_provider_health")
@patch("carpenter.core.models.monitor.get_all_model_health")
@patch("carpenter.core.models.monitor.notifications")
def test_provider_recovery_clears_dedup(mock_notif, mock_health, mock_prov):
    """Provider recovery clears dedup state for re-notification."""
    mock_health.return_value = []

    # Provider down
    mock_prov.return_value = [
        ProviderHealthState(
            provider="anthropic", health=ModelHealth.CIRCUIT_OPEN,
            model_count=3, circuit_open_count=3,
        ),
    ]
    health_monitor.check_health()
    assert mock_notif.notify.call_count == 1

    # Provider recovers
    mock_prov.return_value = [
        ProviderHealthState(
            provider="anthropic", health=ModelHealth.DEGRADED,
            model_count=3, circuit_open_count=1,
        ),
    ]
    health_monitor.check_health()

    # Provider goes down again — should notify again
    mock_prov.return_value = [
        ProviderHealthState(
            provider="anthropic", health=ModelHealth.CIRCUIT_OPEN,
            model_count=3, circuit_open_count=3,
        ),
    ]
    health_monitor.check_health()
    assert mock_notif.notify.call_count == 2
