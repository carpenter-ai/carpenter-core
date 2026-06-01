"""Tests for carpenter.core.models.registry."""

import os
import tempfile
import textwrap

import pytest

from carpenter.core.models.registry import (
    ModelEntry,
    _load_from_yaml,
    _load_from_config,
    get_entry,
    get_entry_by_model_id,
    get_registry,
    load_registry,
    reload_registry,
    update_measured_speed,
    _registry,
    _registry_loaded,
)


@pytest.fixture(autouse=True)
def reset_registry(monkeypatch):
    """Reset module-level registry state between tests."""
    import carpenter.core.models.registry as mod
    mod._registry = {}
    mod._registry_loaded = False
    yield


@pytest.fixture
def sample_yaml(tmp_path):
    """Create a sample model_registry.yaml file."""
    content = textwrap.dedent("""\
        models:
          opus:
            provider: anthropic
            model_id: claude-opus-4-7
            quality_tier: 5
            cost_per_mtok_in: 15.0
            cost_per_mtok_out: 75.0
            cached_cost_per_mtok_in: 1.5
            context_window: 200000
            capabilities: [planning, review, code]
          haiku:
            provider: anthropic
            model_id: claude-haiku-4-5
            quality_tier: 2
            cost_per_mtok_in: 0.8
            cost_per_mtok_out: 4.0
            cached_cost_per_mtok_in: 0.08
            context_window: 200000
            capabilities: [summarization]
          local-llm:
            provider: ollama
            model_id: "qwen3.5:9b"
            quality_tier: 1
            cost_per_mtok_in: 0.0
            cost_per_mtok_out: 0.0
            cached_cost_per_mtok_in: 0.0
            context_window: 16384
            capabilities: [chat]
    """)
    path = tmp_path / "model_registry.yaml"
    path.write_text(content)
    return str(path)


class TestLoadFromYaml:
    def test_load_basic(self, sample_yaml):
        entries = _load_from_yaml(sample_yaml)
        assert "opus" in entries
        assert "haiku" in entries
        assert "local-llm" in entries

    def test_entry_fields(self, sample_yaml):
        entries = _load_from_yaml(sample_yaml)
        opus = entries["opus"]
        assert isinstance(opus, ModelEntry)
        assert opus.key == "opus"
        assert opus.provider == "anthropic"
        assert opus.model_id == "claude-opus-4-7"
        assert opus.quality_tier == 5
        assert opus.cost_per_mtok_in == 15.0
        assert opus.cost_per_mtok_out == 75.0
        assert opus.cached_cost_per_mtok_in == 1.5
        assert opus.context_window == 200000
        assert "planning" in opus.capabilities
        assert opus.measured_speed is None

    def test_free_model(self, sample_yaml):
        entries = _load_from_yaml(sample_yaml)
        local = entries["local-llm"]
        assert local.cost_per_mtok_in == 0.0
        assert local.cost_per_mtok_out == 0.0

    def test_missing_file(self):
        entries = _load_from_yaml("/nonexistent/path.yaml")
        assert entries == {}

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        entries = _load_from_yaml(str(p))
        assert entries == {}

    def test_malformed_yaml(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("not: a: valid: yaml: [")
        entries = _load_from_yaml(str(p))
        assert entries == {}


class TestLoadFromConfig:
    def test_fallback_to_config(self, monkeypatch):
        monkeypatch.setattr(
            "carpenter.config.CONFIG",
            {
                "models": {
                    "opus": {
                        "provider": "anthropic",
                        "model_id": "claude-opus-4-7",
                        "cost_tier": "high",
                        "context_window": 200000,
                        "roles": ["planning", "review"],
                    },
                    "haiku": {
                        "provider": "anthropic",
                        "model_id": "claude-haiku-4-5",
                        "cost_tier": "low",
                        "context_window": 200000,
                        "roles": ["summarization"],
                    },
                },
            },
        )
        entries = _load_from_config()
        assert "opus" in entries
        assert entries["opus"].quality_tier == 5  # high → 5
        assert entries["haiku"].quality_tier == 2  # low → 2


class TestRegistryLookup:
    def test_load_and_get(self, sample_yaml, monkeypatch):
        import carpenter.core.models.registry as mod
        monkeypatch.setattr(mod, "_yaml_path", lambda: sample_yaml)
        load_registry()
        assert get_entry("opus") is not None
        assert get_entry("haiku") is not None
        assert get_entry("nonexistent") is None

    def test_get_by_model_id(self, sample_yaml, monkeypatch):
        import carpenter.core.models.registry as mod
        monkeypatch.setattr(mod, "_yaml_path", lambda: sample_yaml)
        load_registry()
        entry = get_entry_by_model_id("claude-opus-4-7")
        assert entry is not None
        assert entry.key == "opus"

    def test_get_by_model_id_with_provider_prefix(self, sample_yaml, monkeypatch):
        import carpenter.core.models.registry as mod
        monkeypatch.setattr(mod, "_yaml_path", lambda: sample_yaml)
        load_registry()
        entry = get_entry_by_model_id("anthropic:claude-opus-4-7")
        assert entry is not None
        assert entry.key == "opus"

    def test_get_by_model_id_not_found(self, sample_yaml, monkeypatch):
        import carpenter.core.models.registry as mod
        monkeypatch.setattr(mod, "_yaml_path", lambda: sample_yaml)
        load_registry()
        assert get_entry_by_model_id("nonexistent-model") is None


class TestReload:
    def test_reload_picks_up_changes(self, tmp_path, monkeypatch):
        import carpenter.core.models.registry as mod

        yaml_path = tmp_path / "model_registry.yaml"
        yaml_path.write_text(textwrap.dedent("""\
            models:
              alpha:
                provider: test
                model_id: alpha-1
                quality_tier: 3
                cost_per_mtok_in: 1.0
                cost_per_mtok_out: 5.0
                cached_cost_per_mtok_in: 0.1
                context_window: 8000
                capabilities: [test]
        """))
        monkeypatch.setattr(mod, "_yaml_path", lambda: str(yaml_path))
        load_registry()
        assert get_entry("alpha") is not None
        assert get_entry("beta") is None

        # Modify file
        yaml_path.write_text(textwrap.dedent("""\
            models:
              beta:
                provider: test
                model_id: beta-1
                quality_tier: 2
                cost_per_mtok_in: 0.5
                cost_per_mtok_out: 2.5
                cached_cost_per_mtok_in: 0.05
                context_window: 4000
                capabilities: [test]
        """))
        reload_registry()
        assert get_entry("alpha") is None
        assert get_entry("beta") is not None


class TestUpdateMeasuredSpeed:
    def test_update_in_memory(self, sample_yaml, monkeypatch):
        import carpenter.core.models.registry as mod
        monkeypatch.setattr(mod, "_yaml_path", lambda: sample_yaml)
        load_registry()
        assert get_entry("opus").measured_speed is None
        update_measured_speed("opus", 1.5)
        assert get_entry("opus").measured_speed == 1.5

    def test_update_persists_to_yaml(self, sample_yaml, monkeypatch):
        import yaml as _yaml
        import carpenter.core.models.registry as mod
        monkeypatch.setattr(mod, "_yaml_path", lambda: sample_yaml)
        load_registry()
        update_measured_speed("opus", 2.3)

        # Re-read YAML
        with open(sample_yaml) as f:
            data = _yaml.safe_load(f)
        assert data["models"]["opus"]["measured_speed"] == 2.3

    def test_update_unknown_key(self, sample_yaml, monkeypatch):
        import carpenter.core.models.registry as mod
        monkeypatch.setattr(mod, "_yaml_path", lambda: sample_yaml)
        load_registry()
        # Should not raise, just log warning
        update_measured_speed("nonexistent", 1.0)


class TestGetRegistryAutoLoads:
    def test_auto_loads_on_first_access(self, monkeypatch):
        """get_registry() should auto-load from config fallback."""
        import carpenter.core.models.registry as mod
        monkeypatch.setattr(mod, "_yaml_path", lambda: None)
        monkeypatch.setattr(
            "carpenter.config.CONFIG",
            {
                "models": {
                    "test-model": {
                        "provider": "test",
                        "model_id": "test-1",
                        "cost_tier": "medium",
                        "context_window": 8000,
                        "roles": ["test"],
                    },
                },
                "base_dir": "",
            },
        )
        reg = get_registry()
        assert "test-model" in reg


