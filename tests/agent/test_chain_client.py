"""Tests for carpenter.agent.providers.chain."""

import pytest
from unittest.mock import MagicMock, patch

import httpx

from carpenter import config
from carpenter.agent.providers import chain as chain_client
from carpenter.agent import circuit_breaker
from carpenter.agent.providers.chain import ChainEntry, load_chain


# -- Fixtures --

_TWO_ENTRY_CHAIN = [
    {
        "name": "test-ollama",
        "provider": "ollama",
        "url": "http://fake-ollama:11434",
        "model": "qwen3.5:9b",
        "context_window": 16384,
        "timeout": 30,
    },
    {
        "name": "test-haiku",
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
        "context_window": 200000,
        "timeout": 60,
    },
]


def _openai_response(text="Hello"):
    """Build a minimal OpenAI-format response."""
    return {
        "choices": [
            {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"},
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        "model": "test-model",
    }


def _anthropic_response(text="Hello"):
    """Build a minimal Anthropic-format response."""
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "model": "test-model",
    }


# Circuit breaker reset is handled by _reset_circuit_breakers in
# tests/conftest.py (autouse for all tests).


@pytest.fixture
def chain_config(monkeypatch):
    """Set up a two-entry inference chain in config."""
    monkeypatch.setitem(config.CONFIG, "inference_chain", _TWO_ENTRY_CHAIN)


@pytest.fixture
def empty_chain(monkeypatch):
    """Set up an empty inference chain in config."""
    monkeypatch.setitem(config.CONFIG, "inference_chain", [])


# -- load_chain --


def test_load_chain_from_config(chain_config):
    """Parse config into ChainEntry objects."""
    entries = load_chain()
    assert len(entries) == 2
    assert entries[0].name == "test-ollama"
    assert entries[0].provider == "ollama"
    assert entries[0].model == "qwen3.5:9b"
    assert entries[0].url == "http://fake-ollama:11434"
    assert entries[1].name == "test-haiku"
    assert entries[1].provider == "anthropic"


def test_load_chain_empty_raises(empty_chain):
    """ValueError when inference_chain is empty."""
    with pytest.raises(ValueError, match="inference_chain config is empty"):
        load_chain()


def test_load_chain_missing_provider_raises(monkeypatch):
    """ValueError when provider is missing."""
    monkeypatch.setitem(config.CONFIG, "inference_chain", [{"name": "x"}])
    with pytest.raises(ValueError, match="'provider' is required"):
        load_chain()


def test_load_chain_missing_model_raises(monkeypatch):
    """ValueError when model is missing."""
    monkeypatch.setitem(config.CONFIG, "inference_chain", [{"name": "x", "provider": "ollama"}])
    with pytest.raises(ValueError, match="'model' is required"):
        load_chain()


# -- call --


@patch("carpenter.agent.providers.chain._call_single_backend")
def test_call_first_backend_succeeds(mock_call, chain_config):
    """First backend succeeds, second is not called."""
    resp = _openai_response("from ollama")
    mock_call.return_value = resp

    result = chain_client.call("system", [{"role": "user", "content": "hi"}])

    assert mock_call.call_count == 1
    entry_arg = mock_call.call_args[0][0]
    assert entry_arg.name == "test-ollama"
    assert result["_api_standard"] == "openai"


@patch("carpenter.agent.providers.chain._call_single_backend")
def test_call_failover_on_connect_error(mock_call, chain_config):
    """First backend fails with ConnectError, second succeeds."""
    resp = _openai_response("from haiku")

    def side_effect(entry, *args, **kwargs):
        if entry.name == "test-ollama":
            raise httpx.ConnectError("refused")
        return resp

    mock_call.side_effect = side_effect

    result = chain_client.call("system", [{"role": "user", "content": "hi"}])

    assert mock_call.call_count == 2
    # Response has _api_standard from second backend (anthropic)
    assert result["_api_standard"] == "anthropic"


@patch("carpenter.agent.providers.chain._call_single_backend")
def test_call_failover_on_500(mock_call, chain_config):
    """First backend returns 500, second succeeds."""
    resp = _openai_response("fallback")
    mock_resp = MagicMock()
    mock_resp.status_code = 500

    def side_effect(entry, *args, **kwargs):
        if entry.name == "test-ollama":
            raise httpx.HTTPStatusError("server error", request=MagicMock(), response=mock_resp)
        return resp

    mock_call.side_effect = side_effect

    result = chain_client.call("system", [{"role": "user", "content": "hi"}])
    assert mock_call.call_count == 2


@patch("carpenter.agent.providers.chain._call_single_backend")
def test_call_4xx_does_not_failover(mock_call, chain_config):
    """400 error propagates immediately without trying next backend."""
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_call.side_effect = httpx.HTTPStatusError(
        "bad request", request=MagicMock(), response=mock_resp
    )

    with pytest.raises(httpx.HTTPStatusError):
        chain_client.call("system", [{"role": "user", "content": "hi"}])

    # Only called once — no failover on 4xx
    assert mock_call.call_count == 1


@patch("carpenter.agent.providers.chain._call_single_backend")
def test_call_all_backends_fail(mock_call, chain_config):
    """All backends fail, last error is raised."""
    mock_call.side_effect = httpx.ConnectError("all down")

    with pytest.raises(httpx.ConnectError, match="all down"):
        chain_client.call("system", [{"role": "user", "content": "hi"}])

    assert mock_call.call_count == 2


@patch("carpenter.agent.providers.chain._call_single_backend")
def test_call_skips_open_breaker(mock_call, chain_config):
    """Pre-open first breaker, second gets called."""
    # Force open the first breaker
    breaker = circuit_breaker.get_breaker("test-ollama")
    for _ in range(10):
        breaker.record_failure()

    resp = _openai_response("from second")
    mock_call.return_value = resp

    result = chain_client.call("system", [{"role": "user", "content": "hi"}])

    # Only one call — first was skipped
    assert mock_call.call_count == 1
    entry_arg = mock_call.call_args[0][0]
    assert entry_arg.name == "test-haiku"


# -- extract_text --


def test_extract_text_openai_format():
    """extract_text detects and handles OpenAI format."""
    resp = _openai_response("openai text")
    assert chain_client.extract_text(resp) == "openai text"


def test_extract_text_anthropic_format():
    """extract_text detects and handles Anthropic format."""
    resp = _anthropic_response("anthropic text")
    assert chain_client.extract_text(resp) == "anthropic text"


def test_extract_text_empty():
    """extract_text returns empty string for empty response."""
    assert chain_client.extract_text({}) == ""


# -- _api_standard tag --


@patch("carpenter.agent.providers.chain._call_single_backend")
def test_api_standard_tag_injected(mock_call, chain_config):
    """Verify _api_standard key is injected in response."""
    mock_call.return_value = _openai_response("tagged")

    result = chain_client.call("system", [{"role": "user", "content": "hi"}])

    assert "_api_standard" in result
    assert result["_api_standard"] == "openai"


# -- get_model / get_api_url --


def test_get_model_returns_first_available(chain_config):
    """get_model returns model from first available backend."""
    assert chain_client.get_model() == "qwen3.5:9b"


def test_get_api_url_returns_first_available(chain_config):
    """get_api_url returns URL from first available backend."""
    assert chain_client.get_api_url() == "http://fake-ollama:11434"


def test_get_model_empty_chain(empty_chain):
    """get_model returns empty string for empty chain."""
    assert chain_client.get_model() == ""


def test_get_api_url_empty_chain(empty_chain):
    """get_api_url returns empty string for empty chain."""
    assert chain_client.get_api_url() == ""
