"""Tests for carpenter.core.models.selector."""

import json
import textwrap

import pytest

from carpenter.core.models.registry import ModelEntry
from carpenter.core.models.selector import (
    ModelPolicy,
    PolicyConstraints,
    SelectionResult,
    _passes_constraints,
    select_model,
    select_models,
    PRESETS,
)


# ── Fixtures ─────────────────────────────────────────────────────


def _make_registry():
    """Build a standard test registry."""
    return {
        "opus": ModelEntry(
            key="opus", provider="anthropic", model_id="claude-opus-4-6",
            quality_tier=5, cost_per_mtok_in=15.0, cost_per_mtok_out=75.0,
            cached_cost_per_mtok_in=1.5, context_window=200000,
            capabilities=["planning", "review", "code", "security_review"],
            measured_speed=2.0,
        ),
        "sonnet": ModelEntry(
            key="sonnet", provider="anthropic", model_id="claude-sonnet-4-5-20250929",
            quality_tier=4, cost_per_mtok_in=3.0, cost_per_mtok_out=15.0,
            cached_cost_per_mtok_in=0.3, context_window=200000,
            capabilities=["planning", "review", "code", "documentation"],
            measured_speed=1.0,
        ),
        "haiku": ModelEntry(
            key="haiku", provider="anthropic", model_id="claude-haiku-4-5-20251001",
            quality_tier=2, cost_per_mtok_in=0.8, cost_per_mtok_out=4.0,
            cached_cost_per_mtok_in=0.08, context_window=200000,
            capabilities=["summarization", "documentation", "simple_code"],
            measured_speed=0.3,
        ),
        "local": ModelEntry(
            key="local", provider="ollama", model_id="qwen3.5:9b",
            quality_tier=1, cost_per_mtok_in=0.0, cost_per_mtok_out=0.0,
            cached_cost_per_mtok_in=0.0, context_window=16384,
            capabilities=["chat", "summarization"],
            measured_speed=5.0,
        ),
    }


@pytest.fixture(autouse=True)
def mock_registry(monkeypatch):
    """Provide standard test registry."""
    monkeypatch.setattr(
        "carpenter.core.models.selector.get_registry",
        _make_registry,
    )


@pytest.fixture
def no_health_check(monkeypatch):
    """Disable health checks so tests don't need DB."""
    monkeypatch.setattr(
        "carpenter.core.models.health.should_circuit_break",
        lambda model_id: False,
    )


# ── Constraint filtering tests ───────────────────────────────────


class TestConstraints:
    def test_min_quality_filters_low(self):
        entry = _make_registry()["local"]
        assert not _passes_constraints(entry, PolicyConstraints(min_quality=2))

    def test_min_quality_passes_equal(self):
        entry = _make_registry()["haiku"]
        assert _passes_constraints(entry, PolicyConstraints(min_quality=2))

    def test_max_quality_filters_high(self):
        entry = _make_registry()["opus"]
        assert not _passes_constraints(entry, PolicyConstraints(max_quality=4))

    def test_max_quality_passes_equal(self):
        entry = _make_registry()["sonnet"]
        assert _passes_constraints(entry, PolicyConstraints(max_quality=4))

    def test_max_cost_filters(self):
        entry = _make_registry()["opus"]
        assert not _passes_constraints(entry, PolicyConstraints(max_cost_per_mtok_out=50.0))

    def test_max_cost_passes(self):
        entry = _make_registry()["haiku"]
        assert _passes_constraints(entry, PolicyConstraints(max_cost_per_mtok_out=5.0))

    def test_max_latency_filters(self):
        entry = _make_registry()["local"]  # measured_speed = 5.0
        assert not _passes_constraints(entry, PolicyConstraints(max_latency_s_per_ktok=3.0))

    def test_max_latency_passes(self):
        entry = _make_registry()["haiku"]  # measured_speed = 0.3
        assert _passes_constraints(entry, PolicyConstraints(max_latency_s_per_ktok=1.0))

    def test_max_latency_skips_unknown(self):
        """Models with unknown speed are NOT filtered by latency constraint."""
        entry = ModelEntry(
            key="unknown", provider="test", model_id="test",
            quality_tier=3, cost_per_mtok_in=1.0, cost_per_mtok_out=5.0,
            cached_cost_per_mtok_in=0.1, context_window=8000,
            capabilities=[], measured_speed=None,
        )
        assert _passes_constraints(entry, PolicyConstraints(max_latency_s_per_ktok=1.0))

    def test_required_capabilities_passes(self):
        entry = _make_registry()["opus"]
        assert _passes_constraints(
            entry, PolicyConstraints(required_capabilities=["code", "review"])
        )

    def test_required_capabilities_filters(self):
        entry = _make_registry()["haiku"]
        assert not _passes_constraints(
            entry, PolicyConstraints(required_capabilities=["code"])
        )

    def test_combined_constraints(self):
        entry = _make_registry()["sonnet"]
        # Quality 4, cost 15, speed 1.0, has "code"
        assert _passes_constraints(entry, PolicyConstraints(
            min_quality=3, max_cost_per_mtok_out=20.0,
            max_latency_s_per_ktok=2.0, required_capabilities=["code"],
        ))

    def test_no_constraints_passes_all(self):
        for entry in _make_registry().values():
            assert _passes_constraints(entry, PolicyConstraints())


# ── Selection scoring tests ──────────────────────────────────────


class TestSelection:
    def test_hard_pin_bypass(self, no_health_check):
        policy = ModelPolicy(model="anthropic:claude-opus-4-6")
        result = select_model(policy)
        assert result is not None
        assert result.model_id == "anthropic:claude-opus-4-6"
        assert result.reason == "hard-pinned"

    def test_quality_heavy_prefers_opus(self, no_health_check):
        policy = ModelPolicy(
            constraints=PolicyConstraints(min_quality=2),
            preference=(0.0, 1.0, 0.0),  # only quality matters
        )
        result = select_model(policy)
        assert result is not None
        assert result.model_key == "opus"

    def test_cost_heavy_prefers_free(self, no_health_check):
        policy = ModelPolicy(
            preference=(1.0, 0.0, 0.0),  # only cost matters
        )
        result = select_model(policy)
        assert result is not None
        assert result.model_key == "local"  # free model

    def test_speed_heavy_prefers_fastest(self, no_health_check):
        policy = ModelPolicy(
            constraints=PolicyConstraints(min_quality=2),
            preference=(0.0, 0.0, 1.0),  # only speed matters
        )
        result = select_model(policy)
        assert result is not None
        assert result.model_key == "haiku"  # speed 0.3 is fastest among quality>=2

    def test_no_eligible_returns_none(self, no_health_check):
        policy = ModelPolicy(
            constraints=PolicyConstraints(min_quality=10),  # impossible
        )
        result = select_model(policy)
        assert result is None

    def test_max_quality_cap(self, no_health_check):
        """Caretaker preset should only see quality <= 2 models."""
        policy = ModelPolicy(
            constraints=PolicyConstraints(max_quality=2),
            preference=(0.5, 0.3, 0.2),
        )
        result = select_model(policy)
        assert result is not None
        assert result.model_key in ("haiku", "local")

    def test_cost_cap_excludes_expensive(self, no_health_check):
        policy = ModelPolicy(
            constraints=PolicyConstraints(max_cost_per_mtok_out=5.0),
            preference=(0.3, 0.4, 0.3),
        )
        result = select_model(policy)
        assert result is not None
        assert result.model_key in ("haiku", "local")

    def test_balanced_preference(self, no_health_check):
        """Balanced preference should pick a good middle ground."""
        policy = ModelPolicy(
            preference=(0.33, 0.34, 0.33),
        )
        result = select_model(policy)
        assert result is not None
        # With balanced weights, the result should be one of the reasonable models
        assert result.model_key in ("opus", "sonnet", "haiku", "local")

    def test_capability_requirement(self, no_health_check):
        """Requiring 'security_review' should force opus."""
        policy = ModelPolicy(
            constraints=PolicyConstraints(required_capabilities=["security_review"]),
            preference=(0.3, 0.4, 0.3),
        )
        result = select_model(policy)
        assert result is not None
        assert result.model_key == "opus"


# ── Cache-loss penalty tests ─────────────────────────────────────


class TestCacheLossPenalty:
    def test_no_penalty_same_provider(self, no_health_check):
        """Switching within same provider has no penalty."""
        policy = ModelPolicy(
            constraints=PolicyConstraints(min_quality=2),
            preference=(0.0, 1.0, 0.0),
        )
        result_without = select_model(policy)
        result_with = select_model(
            policy,
            current_model="anthropic:claude-sonnet-4-5-20250929",
            cached_tokens=50000,
        )
        # Both should prefer opus (quality-heavy)
        assert result_without.model_key == "opus"
        assert result_with.model_key == "opus"

    def test_penalty_different_provider(self, no_health_check):
        """Large cache with different provider should reduce score."""
        # With huge cache, switching from ollama to anthropic is penalized
        policy = ModelPolicy(
            preference=(0.5, 0.3, 0.2),
        )
        # No cache — cost-heavy prefers local (free)
        result_no_cache = select_model(policy)
        # Large cache from anthropic provider — switching away is penalized
        result_cached = select_model(
            policy,
            current_model="anthropic:claude-sonnet-4-5-20250929",
            cached_tokens=100000,
        )
        # Both results should be valid
        assert result_no_cache is not None
        assert result_cached is not None


# ── Circuit breaker integration ──────────────────────────────────


class TestCircuitBreaker:
    def test_circuit_open_excluded(self, monkeypatch):
        """CIRCUIT_OPEN models should be excluded from selection."""
        def mock_circuit_break(model_id):
            return model_id == "anthropic:claude-opus-4-6"

        monkeypatch.setattr(
            "carpenter.core.models.health.should_circuit_break",
            mock_circuit_break,
        )
        policy = ModelPolicy(
            constraints=PolicyConstraints(min_quality=4),
            preference=(0.0, 1.0, 0.0),
        )
        result = select_model(policy)
        assert result is not None
        # Opus is excluded, should pick sonnet
        assert result.model_key == "sonnet"

    def test_all_circuit_open_keeps_eligible(self, monkeypatch):
        """When all models are CIRCUIT_OPEN, keep all (graceful degradation)."""
        monkeypatch.setattr(
            "carpenter.core.models.health.should_circuit_break",
            lambda model_id: True,
        )
        policy = ModelPolicy(
            preference=(0.0, 1.0, 0.0),
        )
        result = select_model(policy)
        assert result is not None
        # Should still return a result (opus for quality-heavy)
        assert result.model_key == "opus"


# ── Provider-level health filtering ──────────────────────────────


class TestProviderOutageFiltering:
    def test_provider_outage_filters_all_models(self, monkeypatch):
        """When a provider is CIRCUIT_OPEN, all its models are filtered."""
        from carpenter.core.models.health import (
            ModelHealth,
            ProviderHealthState,
        )

        monkeypatch.setattr(
            "carpenter.core.models.health.should_circuit_break",
            lambda model_id: False,
        )

        def mock_provider_health(provider):
            if provider == "anthropic":
                return ProviderHealthState(
                    provider="anthropic", health=ModelHealth.CIRCUIT_OPEN,
                    model_count=3, circuit_open_count=3,
                )
            return ProviderHealthState(
                provider=provider, health=ModelHealth.HEALTHY,
                model_count=1, circuit_open_count=0,
            )

        monkeypatch.setattr(
            "carpenter.core.models.health.get_provider_health",
            mock_provider_health,
        )

        policy = ModelPolicy(preference=(0.3, 0.4, 0.3))
        result = select_model(policy)
        assert result is not None
        # All anthropic models filtered, only ollama:local remains
        assert result.model_key == "local"

    def test_provider_outage_graceful_degrade(self, monkeypatch):
        """When ALL providers are CIRCUIT_OPEN, keep all (graceful degradation)."""
        from carpenter.core.models.health import (
            ModelHealth,
            ProviderHealthState,
        )

        monkeypatch.setattr(
            "carpenter.core.models.health.should_circuit_break",
            lambda model_id: False,
        )

        monkeypatch.setattr(
            "carpenter.core.models.health.get_provider_health",
            lambda provider: ProviderHealthState(
                provider=provider, health=ModelHealth.CIRCUIT_OPEN,
                model_count=2, circuit_open_count=2,
            ),
        )

        policy = ModelPolicy(
            preference=(0.0, 1.0, 0.0),  # quality-heavy → opus
        )
        result = select_model(policy)
        assert result is not None
        # Should keep all and still select opus
        assert result.model_key == "opus"


# ── ModelPolicy serialization ────────────────────────────────────


class TestPolicySerialization:
    def test_from_db_row_with_policy_json(self):
        row = {
            "id": 1,
            "name": "test",
            "model": None,
            "agent_role": None,
            "temperature": None,
            "max_tokens": None,
            "policy_json": json.dumps({
                "constraints": {
                    "min_quality": 3,
                    "max_cost_per_mtok_out": 20.0,
                    "required_capabilities": ["code"],
                },
                "preference": [0.2, 0.5, 0.3],
            }),
        }
        policy = ModelPolicy.from_db_row(row)
        assert policy.id == 1
        assert policy.constraints.min_quality == 3
        assert policy.constraints.max_cost_per_mtok_out == 20.0
        assert policy.constraints.required_capabilities == ["code"]
        assert policy.preference == (0.2, 0.5, 0.3)

    def test_from_db_row_without_policy_json(self):
        row = {
            "id": 2,
            "name": "",
            "model": "anthropic:claude-sonnet-4-5-20250929",
            "agent_role": None,
            "temperature": 0.7,
            "max_tokens": 4096,
            "policy_json": None,
        }
        policy = ModelPolicy.from_db_row(row)
        assert policy.model == "anthropic:claude-sonnet-4-5-20250929"
        assert policy.constraints is None
        assert policy.preference == (0.3, 0.4, 0.3)

    def test_to_policy_json_roundtrip(self):
        policy = ModelPolicy(
            constraints=PolicyConstraints(
                min_quality=4,
                max_cost_per_mtok_out=50.0,
            ),
            preference=(0.1, 0.6, 0.3),
        )
        json_str = policy.to_policy_json()
        assert json_str is not None
        data = json.loads(json_str)
        assert data["constraints"]["min_quality"] == 4
        assert data["preference"] == [0.1, 0.6, 0.3]

    def test_to_policy_json_none_for_defaults(self):
        policy = ModelPolicy()
        assert policy.to_policy_json() is None

    def test_from_db_row_invalid_json(self):
        row = {
            "id": 3,
            "name": "",
            "model": None,
            "agent_role": None,
            "temperature": None,
            "max_tokens": None,
            "policy_json": "not valid json{",
        }
        policy = ModelPolicy.from_db_row(row)
        assert policy.constraints is None  # Defaults on parse failure
        assert policy.preference == (0.3, 0.4, 0.3)


# ── Presets ──────────────────────────────────────────────────────


class TestPresets:
    def test_fast_chat_exists(self):
        assert "fast-chat" in PRESETS
        p = PRESETS["fast-chat"]
        assert p.constraints.min_quality == 2
        assert p.preference[2] > p.preference[0]  # speed > cost

    def test_careful_coding_exists(self):
        assert "careful-coding" in PRESETS
        p = PRESETS["careful-coding"]
        assert p.constraints.min_quality == 4
        assert p.preference[1] > p.preference[0]  # quality > cost

    def test_background_batch_exists(self):
        assert "background-batch" in PRESETS
        p = PRESETS["background-batch"]
        assert p.constraints.max_cost_per_mtok_out == 5.0
        assert p.preference[0] > p.preference[1]  # cost > quality

    def test_caretaker_exists(self):
        assert "caretaker" in PRESETS
        p = PRESETS["caretaker"]
        assert p.constraints.max_quality == 2

    def test_presets_produce_results(self, no_health_check):
        """All presets should produce a valid selection."""
        for name, policy in PRESETS.items():
            result = select_model(policy)
            assert result is not None, f"Preset {name!r} returned None"

    def test_careful_coding_selects_high_quality(self, no_health_check):
        result = select_model(PRESETS["careful-coding"])
        assert result is not None
        assert result.model_key in ("opus", "sonnet")

    def test_caretaker_selects_low_quality(self, no_health_check):
        result = select_model(PRESETS["caretaker"])
        assert result is not None
        assert result.model_key in ("haiku", "local")


# ── Edge cases ───────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_registry(self, monkeypatch, no_health_check):
        monkeypatch.setattr(
            "carpenter.core.models.selector.get_registry",
            lambda: {},
        )
        result = select_model(ModelPolicy())
        assert result is None

    def test_single_model_registry(self, monkeypatch, no_health_check):
        monkeypatch.setattr(
            "carpenter.core.models.selector.get_registry",
            lambda: {"only": ModelEntry(
                key="only", provider="test", model_id="test-1",
                quality_tier=3, cost_per_mtok_in=1.0, cost_per_mtok_out=5.0,
                cached_cost_per_mtok_in=0.1, context_window=8000,
                capabilities=["test"],
            )},
        )
        result = select_model(ModelPolicy())
        assert result is not None
        assert result.model_key == "only"

    def test_tied_scores(self, monkeypatch, no_health_check):
        """When models have identical scores, one should still be picked."""
        identical = ModelEntry(
            key="a", provider="test", model_id="a",
            quality_tier=3, cost_per_mtok_in=1.0, cost_per_mtok_out=5.0,
            cached_cost_per_mtok_in=0.1, context_window=8000,
            capabilities=["test"], measured_speed=1.0,
        )
        identical2 = ModelEntry(
            key="b", provider="test", model_id="b",
            quality_tier=3, cost_per_mtok_in=1.0, cost_per_mtok_out=5.0,
            cached_cost_per_mtok_in=0.1, context_window=8000,
            capabilities=["test"], measured_speed=1.0,
        )
        monkeypatch.setattr(
            "carpenter.core.models.selector.get_registry",
            lambda: {"a": identical, "b": identical2},
        )
        result = select_model(ModelPolicy())
        assert result is not None
        assert result.model_key in ("a", "b")

    def test_all_free_models(self, monkeypatch, no_health_check):
        free = ModelEntry(
            key="free", provider="ollama", model_id="free-1",
            quality_tier=1, cost_per_mtok_in=0.0, cost_per_mtok_out=0.0,
            cached_cost_per_mtok_in=0.0, context_window=4000,
            capabilities=["chat"],
        )
        monkeypatch.setattr(
            "carpenter.core.models.selector.get_registry",
            lambda: {"free": free},
        )
        result = select_model(ModelPolicy())
        assert result is not None
        assert result.model_key == "free"


# ── select_models (ranked list) tests ───────���────────────────────


class TestSelectModels:
    def test_returns_all_eligible_sorted(self, no_health_check):
        """select_models returns all eligible models sorted by score descending."""
        policy = ModelPolicy(preference=(0.3, 0.4, 0.3))
        results = select_models(policy)
        assert len(results) == 4  # opus, sonnet, haiku, local
        # Verify sorted by score descending
        for i in range(len(results) - 1):
            assert results[i].score >= results[i + 1].score

    def test_hard_pin_returns_single(self, no_health_check):
        """Hard-pinned policy returns single-element list."""
        policy = ModelPolicy(model="anthropic:claude-opus-4-6")
        results = select_models(policy)
        assert len(results) == 1
        assert results[0].model_id == "anthropic:claude-opus-4-6"
        assert results[0].reason == "hard-pinned"

    def test_empty_registry_returns_empty(self, monkeypatch, no_health_check):
        monkeypatch.setattr(
            "carpenter.core.models.selector.get_registry",
            lambda: {},
        )
        results = select_models(ModelPolicy())
        assert results == []

    def test_no_eligible_returns_empty(self, no_health_check):
        """Impossible constraints return empty list."""
        policy = ModelPolicy(constraints=PolicyConstraints(min_quality=10))
        results = select_models(policy)
        assert results == []

    def test_first_matches_select_model(self, no_health_check):
        """First element of select_models matches select_model result."""
        policy = ModelPolicy(
            constraints=PolicyConstraints(min_quality=2),
            preference=(0.1, 0.6, 0.3),
        )
        single = select_model(policy)
        ranked = select_models(policy)
        assert single is not None
        assert len(ranked) > 0
        assert single.model_key == ranked[0].model_key
        assert single.model_id == ranked[0].model_id
        assert abs(single.score - ranked[0].score) < 1e-9

    def test_constraint_filtering_reduces_list(self, no_health_check):
        """Constraints reduce the returned list."""
        # Only quality >= 4 → opus and sonnet
        policy = ModelPolicy(constraints=PolicyConstraints(min_quality=4))
        results = select_models(policy)
        assert len(results) == 2
        keys = {r.model_key for r in results}
        assert keys == {"opus", "sonnet"}

    def test_circuit_breaker_excludes_model(self, monkeypatch):
        """CIRCUIT_OPEN model is excluded from ranked list."""
        def mock_circuit_break(model_id):
            return model_id == "anthropic:claude-opus-4-6"

        monkeypatch.setattr(
            "carpenter.core.models.health.should_circuit_break",
            mock_circuit_break,
        )
        policy = ModelPolicy(preference=(0.3, 0.4, 0.3))
        results = select_models(policy)
        keys = {r.model_key for r in results}
        assert "opus" not in keys
        assert len(results) == 3  # sonnet, haiku, local

    def test_background_batch_ranks_free_first(self, no_health_check):
        """background-batch preset (60% cost weight) ranks free model first."""
        results = select_models(PRESETS["background-batch"])
        assert len(results) > 0
        # The free local model should score highest for cost-heavy policy
        assert results[0].model_key == "local"

    def test_contains_fallback_options(self, no_health_check):
        """Ranked list provides fallback options after the top pick."""
        results = select_models(PRESETS["background-batch"])
        # Should have at least 2 models (local + haiku at minimum)
        assert len(results) >= 2
        # Second model is a valid fallback
        assert results[1].model_id != results[0].model_id
