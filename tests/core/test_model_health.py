"""Tests for carpenter.core.models.health."""

import pytest
from unittest.mock import patch

from carpenter.core.models.health import (
    ModelHealth,
    ModelHealthState,
    ProviderHealthState,
    _health_cache,
    all_cloud_models_circuit_open,
    get_provider_health,
    get_all_provider_health,
    record_model_call,
)
from carpenter.core.models.registry import ModelEntry


@pytest.fixture(autouse=True)
def clear_health_cache():
    """Clear the health cache before and after each test."""
    _health_cache.clear()
    yield
    _health_cache.clear()


def _make_state(model_id, health, **kwargs):
    """Helper to build a ModelHealthState and insert into cache."""
    defaults = {
        "model_id": model_id,
        "health": health,
        "success_rate": 1.0 if health == ModelHealth.HEALTHY else 0.0,
        "consecutive_failures": 5 if health == ModelHealth.CIRCUIT_OPEN else 0,
        "total_attempts": 10,
        "backoff_multiplier": 1.0,
    }
    defaults.update(kwargs)
    state = ModelHealthState(**defaults)
    _health_cache[model_id] = state
    return state


def _make_registry():
    """Standard test registry with cloud + local models."""
    return {
        "opus": ModelEntry(
            key="opus", provider="anthropic", model_id="claude-opus-4-6",
            quality_tier=5, cost_per_mtok_in=15.0, cost_per_mtok_out=75.0,
            cached_cost_per_mtok_in=1.5, context_window=200000,
        ),
        "sonnet": ModelEntry(
            key="sonnet", provider="anthropic", model_id="claude-sonnet-4-5-20250929",
            quality_tier=4, cost_per_mtok_in=3.0, cost_per_mtok_out=15.0,
            cached_cost_per_mtok_in=0.3, context_window=200000,
        ),
        "haiku": ModelEntry(
            key="haiku", provider="anthropic", model_id="claude-haiku-4-5-20251001",
            quality_tier=2, cost_per_mtok_in=0.8, cost_per_mtok_out=4.0,
            cached_cost_per_mtok_in=0.08, context_window=200000,
        ),
        "local": ModelEntry(
            key="local", provider="ollama", model_id="qwen3.5:9b",
            quality_tier=1, cost_per_mtok_in=0.0, cost_per_mtok_out=0.0,
            cached_cost_per_mtok_in=0.0, context_window=16384,
        ),
    }


# ── all_cloud_models_circuit_open tests ──────────────────────────


class TestAllCloudCircuitOpen:
    @patch("carpenter.core.models.registry.get_registry")
    def test_all_cloud_circuit_open_true(self, mock_reg):
        """Returns True when all cloud models are CIRCUIT_OPEN."""
        mock_reg.return_value = _make_registry()
        # Mark all three anthropic models as CIRCUIT_OPEN
        _make_state("anthropic:claude-opus-4-6", ModelHealth.CIRCUIT_OPEN)
        _make_state("anthropic:claude-sonnet-4-5-20250929", ModelHealth.CIRCUIT_OPEN)
        _make_state("anthropic:claude-haiku-4-5-20251001", ModelHealth.CIRCUIT_OPEN)
        assert all_cloud_models_circuit_open() is True

    @patch("carpenter.core.models.registry.get_registry")
    def test_all_cloud_circuit_open_false_one_healthy(self, mock_reg):
        """Returns False when at least one cloud model is not CIRCUIT_OPEN."""
        mock_reg.return_value = _make_registry()
        _make_state("anthropic:claude-opus-4-6", ModelHealth.CIRCUIT_OPEN)
        _make_state("anthropic:claude-sonnet-4-5-20250929", ModelHealth.HEALTHY)
        _make_state("anthropic:claude-haiku-4-5-20251001", ModelHealth.CIRCUIT_OPEN)
        assert all_cloud_models_circuit_open() is False

    @patch("carpenter.core.models.registry.get_registry")
    def test_all_cloud_circuit_open_no_models_tracked(self, mock_reg):
        """Returns False when no cloud models have been tracked (no cache entries)."""
        mock_reg.return_value = _make_registry()
        # No states in cache
        assert all_cloud_models_circuit_open() is False

    @patch("carpenter.core.models.registry.get_registry")
    def test_all_cloud_circuit_open_excludes_free(self, mock_reg):
        """Free models (cost=0) are excluded from cloud model check."""
        mock_reg.return_value = _make_registry()
        # Only the local (free) model is tracked — but it's not a cloud model
        _make_state("ollama:qwen3.5:9b", ModelHealth.CIRCUIT_OPEN)
        assert all_cloud_models_circuit_open() is False


# ── Provider health tests ────────────────────────────────────────


class TestProviderHealth:
    def test_provider_health_all_healthy(self):
        """All models healthy → provider HEALTHY."""
        _make_state("anthropic:opus", ModelHealth.HEALTHY)
        _make_state("anthropic:sonnet", ModelHealth.HEALTHY)
        result = get_provider_health("anthropic")
        assert result.health == ModelHealth.HEALTHY
        assert result.model_count == 2
        assert result.circuit_open_count == 0

    def test_provider_health_mixed(self):
        """Mixed states → worst non-circuit state."""
        _make_state("anthropic:opus", ModelHealth.HEALTHY)
        _make_state("anthropic:sonnet", ModelHealth.DEGRADED, success_rate=0.6)
        _make_state("anthropic:haiku", ModelHealth.UNHEALTHY, success_rate=0.3)
        result = get_provider_health("anthropic")
        assert result.health == ModelHealth.UNHEALTHY
        assert result.model_count == 3
        assert result.circuit_open_count == 0

    def test_provider_health_all_circuit_open(self):
        """All models CIRCUIT_OPEN → provider CIRCUIT_OPEN."""
        _make_state("anthropic:opus", ModelHealth.CIRCUIT_OPEN)
        _make_state("anthropic:sonnet", ModelHealth.CIRCUIT_OPEN)
        result = get_provider_health("anthropic")
        assert result.health == ModelHealth.CIRCUIT_OPEN
        assert result.circuit_open_count == 2

    def test_provider_health_mixed_with_circuit_open(self):
        """Some CIRCUIT_OPEN + some healthy → worst non-circuit state."""
        _make_state("anthropic:opus", ModelHealth.CIRCUIT_OPEN)
        _make_state("anthropic:sonnet", ModelHealth.DEGRADED, success_rate=0.7)
        result = get_provider_health("anthropic")
        assert result.health == ModelHealth.DEGRADED
        assert result.circuit_open_count == 1

    def test_provider_health_no_models_tracked(self):
        """Unknown provider → HEALTHY with 0 models."""
        result = get_provider_health("unknown_provider")
        assert result.health == ModelHealth.HEALTHY
        assert result.model_count == 0

    def test_get_all_provider_health(self):
        """Returns health for all tracked providers."""
        _make_state("anthropic:opus", ModelHealth.HEALTHY)
        _make_state("ollama:qwen", ModelHealth.DEGRADED, success_rate=0.6)
        result = get_all_provider_health()
        providers = {s.provider for s in result}
        assert providers == {"anthropic", "ollama"}

    def test_get_all_provider_health_empty(self):
        """Returns empty list when no models tracked."""
        result = get_all_provider_health()
        assert result == []


# ── record_model_call provider storage tests ─────────────────────


class TestRecordModelCallProvider:
    def test_record_model_call_stores_provider(self):
        """record_model_call stores provider extracted from model_id."""
        record_model_call("anthropic:claude-sonnet", success=True)

        from carpenter.db import get_db
        db = get_db()
        try:
            row = db.execute(
                "SELECT provider FROM model_calls WHERE model_id = ?",
                ("anthropic:claude-sonnet",),
            ).fetchone()
            assert row is not None
            assert row["provider"] == "anthropic"
        finally:
            db.close()

    def test_record_model_call_default_provider(self):
        """Bare model name (no colon) defaults to 'anthropic'."""
        record_model_call("claude-haiku", success=True)

        from carpenter.db import get_db
        db = get_db()
        try:
            row = db.execute(
                "SELECT provider FROM model_calls WHERE model_id = ?",
                ("claude-haiku",),
            ).fetchone()
            assert row is not None
            assert row["provider"] == "anthropic"
        finally:
            db.close()
