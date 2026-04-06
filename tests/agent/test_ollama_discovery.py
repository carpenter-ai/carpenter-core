"""Tests for carpenter.agent.ollama_discovery."""

import pytest
from unittest.mock import MagicMock, patch

from carpenter.agent.ollama_discovery import (
    OllamaModel,
    check_health,
    discover_models,
    find_model,
)


def _make_tags_response(models):
    """Build a mock httpx.Response for /api/tags."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"models": models}
    resp.raise_for_status = MagicMock()
    return resp


# -- discover_models --


@patch("carpenter.agent.ollama_discovery.httpx.get")
def test_discover_models_success(mock_get):
    """Valid /api/tags response parsed correctly."""
    mock_get.return_value = _make_tags_response([
        {"name": "qwen3.5:9b", "size": 5000000000, "extra_field": "ignored"},
        {"name": "llama3.1:8b", "size": 4000000000},
    ])

    models = discover_models(url="http://test:11434")
    assert len(models) == 2
    assert models[0] == OllamaModel(name="qwen3.5:9b", size=5000000000)
    assert models[1] == OllamaModel(name="llama3.1:8b", size=4000000000)


@patch("carpenter.agent.ollama_discovery.httpx.get")
def test_constrained_extraction(mock_get):
    """Unexpected fields are discarded; only name and size are kept."""
    mock_get.return_value = _make_tags_response([
        {
            "name": "model-a",
            "size": 100,
            "modified_at": "2025-01-01",
            "digest": "abc123",
            "details": {"family": "llama"},
        },
    ])

    models = discover_models(url="http://test:11434")
    assert len(models) == 1
    m = models[0]
    assert m.name == "model-a"
    assert m.size == 100
    # Frozen dataclass — only name and size attributes
    assert not hasattr(m, "modified_at")
    assert not hasattr(m, "digest")


@patch("carpenter.agent.ollama_discovery.httpx.get")
def test_truncates_long_names(mock_get):
    """Names longer than 200 chars are truncated."""
    long_name = "x" * 300
    mock_get.return_value = _make_tags_response([
        {"name": long_name, "size": 42},
    ])

    models = discover_models(url="http://test:11434")
    assert len(models) == 1
    assert len(models[0].name) == 200
    assert models[0].name == "x" * 200


@patch("carpenter.agent.ollama_discovery.httpx.get")
def test_non_string_name_skipped(mock_get):
    """Entries with non-string name are filtered out."""
    mock_get.return_value = _make_tags_response([
        {"name": 12345, "size": 100},        # int name — skipped
        {"name": None, "size": 200},          # None name — skipped
        {"name": "valid", "size": 300},       # valid
    ])

    models = discover_models(url="http://test:11434")
    assert len(models) == 1
    assert models[0].name == "valid"


@patch("carpenter.agent.ollama_discovery.httpx.get")
def test_non_int_size_skipped(mock_get):
    """Entries with non-int size are filtered out."""
    mock_get.return_value = _make_tags_response([
        {"name": "bad-size", "size": "big"},
        {"name": "good", "size": 42},
    ])

    models = discover_models(url="http://test:11434")
    assert len(models) == 1
    assert models[0].name == "good"


# -- check_health --


@patch("carpenter.agent.ollama_discovery.httpx.get")
def test_check_health_true(mock_get):
    """check_health returns True when server responds 200."""
    mock_get.return_value = MagicMock(status_code=200)
    assert check_health(url="http://test:11434") is True


@patch("carpenter.agent.ollama_discovery.httpx.get")
def test_check_health_false_on_error(mock_get):
    """check_health returns False when server is unreachable."""
    import httpx
    mock_get.side_effect = httpx.ConnectError("refused")
    assert check_health(url="http://test:11434") is False


@patch("carpenter.agent.ollama_discovery.httpx.get")
def test_check_health_false_on_non_200(mock_get):
    """check_health returns False on non-200 status."""
    mock_get.return_value = MagicMock(status_code=500)
    assert check_health(url="http://test:11434") is False


# -- find_model --


@patch("carpenter.agent.ollama_discovery.httpx.get")
def test_find_model_found(mock_get):
    """find_model returns the matching model."""
    mock_get.return_value = _make_tags_response([
        {"name": "alpha", "size": 100},
        {"name": "beta", "size": 200},
    ])

    result = find_model("beta", url="http://test:11434")
    assert result == OllamaModel(name="beta", size=200)


@patch("carpenter.agent.ollama_discovery.httpx.get")
def test_find_model_not_found(mock_get):
    """find_model returns None when model is not present."""
    mock_get.return_value = _make_tags_response([
        {"name": "alpha", "size": 100},
    ])

    result = find_model("missing", url="http://test:11434")
    assert result is None
