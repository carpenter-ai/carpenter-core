"""Unit tests for model_resolver module."""

import pytest
from carpenter.agent import model_resolver
from carpenter import config


@pytest.fixture(autouse=True)
def mock_escalation_config(monkeypatch):
    """Inject test escalation config for all tests."""
    test_config = {
        **config.DEFAULTS,
        "escalation": {
            "require_confirmation": True,
            "stacks": {
                "coding": [
                    "ollama:qwen2.5-coder:32b",
                    "anthropic:claude-haiku-4.5-20241022",
                    "anthropic:claude-sonnet-4-20250514",
                ],
                "writing": [
                    "ollama:llama3.1:70b",
                    "anthropic:claude-haiku-4.5-20241022",
                    "anthropic:claude-opus-4-6",
                ],
                "general": [
                    "anthropic:claude-haiku-4.5-20241022",
                    "anthropic:claude-sonnet-4-20250514",
                ],
            },
            "pricing": {
                "anthropic:claude-haiku-4.5-20241022": [0.80, 4.00],
                "anthropic:claude-sonnet-4-20250514": [3.00, 15.00],
                "anthropic:claude-opus-4-6": [15.00, 75.00],
                "ollama:qwen2.5-coder:32b": [0.00, 0.00],
                "ollama:llama3.1:70b": [0.00, 0.00],
            },
        },
    }
    monkeypatch.setattr(config, "CONFIG", test_config)


def test_parse_model_string_valid():
    """Parse valid model string."""
    provider, model = model_resolver.parse_model_string("anthropic:claude-haiku-4.5")
    assert provider == "anthropic"
    assert model == "claude-haiku-4.5"


def test_parse_model_string_ollama_colon():
    """Parse Ollama model with variant (contains extra colon)."""
    provider, model = model_resolver.parse_model_string("ollama:qwen2.5-coder:32b")
    assert provider == "ollama"
    assert model == "qwen2.5-coder:32b"


def test_parse_model_string_bare_name():
    """Bare model name infers provider from ai_provider config."""
    provider, model = model_resolver.parse_model_string("just-a-model-name")
    # Falls back to ai_provider config (default: "anthropic")
    assert provider == "anthropic"
    assert model == "just-a-model-name"


def test_parse_model_string_invalid():
    """Raise on malformed model string."""
    with pytest.raises(ValueError, match="Invalid model string"):
        model_resolver.parse_model_string(":empty-provider")


def test_get_escalation_stack_coding():
    """Get coding escalation stack from config."""
    stack = model_resolver.get_escalation_stack("coding")
    assert stack == [
        "ollama:qwen2.5-coder:32b",
        "anthropic:claude-haiku-4.5-20241022",
        "anthropic:claude-sonnet-4-20250514",
    ]


def test_get_escalation_stack_fallback():
    """Fall back to general stack if task_type not found."""
    stack = model_resolver.get_escalation_stack("nonexistent")
    assert stack == [
        "anthropic:claude-haiku-4.5-20241022",
        "anthropic:claude-sonnet-4-20250514",
    ]


def test_get_next_model_finds_next():
    """Find next model in escalation stack."""
    next_model = model_resolver.get_next_model(
        "anthropic:claude-haiku-4.5-20241022", "coding"
    )
    assert next_model == "anthropic:claude-sonnet-4-20250514"


def test_get_next_model_at_top():
    """Return None when already at highest tier."""
    next_model = model_resolver.get_next_model(
        "anthropic:claude-sonnet-4-20250514", "coding"
    )
    assert next_model is None


def test_get_next_model_not_in_stack():
    """Return first model if current not in stack (fallback)."""
    next_model = model_resolver.get_next_model(
        "unknown:model", "coding"
    )
    # Should return first model in coding stack
    assert next_model == "ollama:qwen2.5-coder:32b"


def test_create_client_for_model_anthropic():
    """Return claude_client for anthropic provider."""
    from carpenter.agent.providers import anthropic as claude_client
    client = model_resolver.create_client_for_model("anthropic:claude-haiku-4.5")
    assert client is claude_client


def test_create_client_for_model_ollama():
    """Return ollama_client for ollama provider."""
    from carpenter.agent.providers import ollama as ollama_client
    client = model_resolver.create_client_for_model("ollama:qwen2.5-coder:32b")
    assert client is ollama_client


def test_create_client_for_model_unknown():
    """Raise ValueError for unknown provider."""
    with pytest.raises(ValueError, match="Unknown AI provider"):
        model_resolver.create_client_for_model("unknown:model")


def test_estimate_cost_multiplier_3x():
    """Estimate 3x cost increase from haiku to sonnet."""
    cost = model_resolver.estimate_cost_multiplier(
        "anthropic:claude-haiku-4.5-20241022",
        "anthropic:claude-sonnet-4-20250514",
    )
    # haiku avg = 2.40, sonnet avg = 9.00 => 3.75x => "~3x"
    assert cost == "~3x"


def test_estimate_cost_multiplier_10x():
    """Estimate 10x+ cost increase from haiku to opus."""
    cost = model_resolver.estimate_cost_multiplier(
        "anthropic:claude-haiku-4.5-20241022",
        "anthropic:claude-opus-4-6",
    )
    # haiku avg = 2.40, opus avg = 45.00 => ~18.75x
    assert cost == "~18x"


def test_estimate_cost_multiplier_free():
    """Show 'free' for local Ollama models."""
    cost = model_resolver.estimate_cost_multiplier(
        "ollama:qwen2.5-coder:32b",
        "ollama:llama3.1:70b",
    )
    assert cost == "free"


def test_estimate_cost_multiplier_paid():
    """Show 'paid' when going from free to paid model."""
    cost = model_resolver.estimate_cost_multiplier(
        "ollama:qwen2.5-coder:32b",
        "anthropic:claude-haiku-4.5-20241022",
    )
    assert cost == "paid"


def test_estimate_cost_multiplier_similar():
    """Show 'similar cost' for small differences."""
    # Mock config with similar pricing
    test_config = {
        **config.CONFIG,
        "escalation": {
            **config.CONFIG["escalation"],
            "pricing": {
                "model:a": [1.00, 2.00],
                "model:b": [1.10, 2.20],  # ~1.1x = similar
            },
        },
    }
    import unittest.mock
    with unittest.mock.patch.object(config, "CONFIG", test_config):
        cost = model_resolver.estimate_cost_multiplier("model:a", "model:b")
        assert cost == "similar cost"


# -- Local provider --

def test_create_client_for_model_local():
    """Return local_client for local provider."""
    from carpenter.agent.providers import local as local_client
    client = model_resolver.create_client_for_model("local:qwen2.5-1.5b")
    assert client is local_client


def test_create_client_for_model_chain():
    """Return chain_client for chain provider."""
    from carpenter.agent.providers import chain as chain_client
    client = model_resolver.create_client_for_model("chain:qwen3.5:9b")
    assert client is chain_client


def test_get_model_for_role_local(monkeypatch):
    """get_model_for_role auto-detects local provider with model path."""
    test_config = {
        **config.CONFIG,
        "ai_provider": "local",
        "local_model_path": "/home/pi/models/qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "model_roles": {"default": "", "chat": ""},
    }
    monkeypatch.setattr(config, "CONFIG", test_config)
    result = model_resolver.get_model_for_role("chat")
    assert result == "local:qwen2.5-1.5b-instruct-q4_k_m"


def test_get_model_for_role_local_no_path(monkeypatch):
    """get_model_for_role falls back to local:default when no model path."""
    test_config = {
        **config.CONFIG,
        "ai_provider": "local",
        "local_model_path": "",
        "model_roles": {"default": "", "chat": ""},
    }
    monkeypatch.setattr(config, "CONFIG", test_config)
    result = model_resolver.get_model_for_role("chat")
    assert result == "local:default"


def test_estimate_cost_multiplier_local_free():
    """Show 'free' for local models (same as Ollama)."""
    cost = model_resolver.estimate_cost_multiplier(
        "local:qwen2.5-1.5b",
        "local:qwen2.5-3b",
    )
    assert cost == "free"
