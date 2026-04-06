"""Tests for carpenter.agent.providers.local."""

import pytest
from unittest.mock import MagicMock

from carpenter.agent.providers import local as local_client


# -- Fixtures for common response shapes --

def _make_response(text="Hello from local"):
    """Build a minimal OpenAI-format response dict."""
    return {
        "choices": [
            {"message": {"role": "assistant", "content": text}},
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        },
    }


# -- extract_text --

def test_extract_text():
    """extract_text pulls text from OpenAI-format response."""
    response = _make_response("Hello world")
    assert local_client.extract_text(response) == "Hello world"


def test_extract_text_empty_choices():
    """extract_text handles empty choices list."""
    assert local_client.extract_text({"choices": []}) == ""


def test_extract_text_no_choices():
    """extract_text handles missing choices key."""
    assert local_client.extract_text({}) == ""


# -- extract_code --

def test_extract_code():
    """extract_code finds Python code block in response."""
    text = 'Here is the code:\n\n```python\nprint("hello")\n```\n\nDone.'
    response = _make_response(text)
    code = local_client.extract_code(response)
    assert code == 'print("hello")\n'


def test_extract_code_no_block():
    """extract_code returns None when no code block present."""
    response = _make_response("No code here, just text.")
    assert local_client.extract_code(response) is None


# -- extract_code_from_text --

def test_extract_code_from_text():
    """extract_code_from_text finds Python code blocks."""
    text = 'Here is the code:\n\n```python\nprint("hello")\n```\n\nDone.'
    code = local_client.extract_code_from_text(text)
    assert code == 'print("hello")\n'


def test_extract_code_from_text_multiple():
    """extract_code_from_text returns the last code block."""
    text = '```python\nfirst()\n```\n\nBetter version:\n\n```python\nsecond()\n```'
    code = local_client.extract_code_from_text(text)
    assert code == "second()\n"


def test_extract_code_from_text_none():
    """extract_code_from_text returns None when no code block."""
    assert local_client.extract_code_from_text("No code here") is None


# -- call --

def test_call_success(monkeypatch):
    """call sends correct URL and body format, returns response."""
    captured = {}

    def mock_post(url, **kwargs):
        import json as _json
        captured["url"] = url
        captured["body"] = _json.loads(kwargs["content"])
        captured["timeout"] = kwargs.get("timeout")
        mock_response = MagicMock()
        mock_response.json.return_value = _make_response("test response")
        return mock_response

    monkeypatch.setattr("httpx.post", mock_post)
    monkeypatch.setitem(
        local_client.config.CONFIG, "local_server_host", "127.0.0.1",
    )
    monkeypatch.setitem(
        local_client.config.CONFIG, "local_server_port", 8081,
    )
    monkeypatch.setitem(
        local_client.config.CONFIG, "local_model_path", "/tmp/test-model.gguf",
    )

    result = local_client.call(
        "System prompt",
        [{"role": "user", "content": "Hello"}],
        model="test-model",
        max_tokens=100,
        temperature=0.5,
    )

    # Verify URL
    assert captured["url"] == "http://127.0.0.1:8081/v1/chat/completions"

    # Verify body structure
    body = captured["body"]
    assert body["model"] == "test-model"
    assert body["max_tokens"] == 100
    assert body["temperature"] == 0.5

    # Verify messages include system prompt first
    assert body["messages"][0] == {"role": "system", "content": "System prompt"}
    assert body["messages"][1] == {"role": "user", "content": "Hello"}

    # Verify timeout is long (local models are slow)
    assert captured["timeout"] == 600.0

    # Verify response
    assert result["choices"][0]["message"]["content"] == "test response"


def test_call_timeout(monkeypatch):
    """call propagates timeout exceptions."""
    import httpx

    def mock_post(url, **kwargs):
        raise httpx.TimeoutException("Connection timed out")

    monkeypatch.setattr("httpx.post", mock_post)
    monkeypatch.setitem(
        local_client.config.CONFIG, "local_server_host", "127.0.0.1",
    )
    monkeypatch.setitem(
        local_client.config.CONFIG, "local_server_port", 8081,
    )

    with pytest.raises(httpx.TimeoutException):
        local_client.call(
            "System prompt",
            [{"role": "user", "content": "Hello"}],
        )


def test_call_defaults(monkeypatch):
    """call uses defaults from config when model/max_tokens not specified."""
    captured = {}

    def mock_post(url, **kwargs):
        import json as _json
        captured["body"] = _json.loads(kwargs["content"])
        mock_response = MagicMock()
        mock_response.json.return_value = _make_response()
        return mock_response

    monkeypatch.setattr("httpx.post", mock_post)
    monkeypatch.setitem(
        local_client.config.CONFIG, "local_server_host", "127.0.0.1",
    )
    monkeypatch.setitem(
        local_client.config.CONFIG, "local_server_port", 8081,
    )
    monkeypatch.setitem(
        local_client.config.CONFIG, "local_model_path", "/tmp/qwen2.5-1.5b.gguf",
    )

    local_client.call("sys", [{"role": "user", "content": "hi"}])

    # Model derived from filename
    assert captured["body"]["model"] == "qwen2.5-1.5b"
    assert captured["body"]["max_tokens"] == local_client.DEFAULT_MAX_TOKENS


# -- get_api_url --

def test_get_api_url_from_config(monkeypatch):
    """get_api_url reads host/port from config."""
    monkeypatch.setitem(
        local_client.config.CONFIG, "local_server_host", "192.168.1.10",
    )
    monkeypatch.setitem(
        local_client.config.CONFIG, "local_server_port", 9090,
    )
    assert local_client.get_api_url() == "http://192.168.1.10:9090"


def test_get_api_url_default(monkeypatch):
    """get_api_url falls back to defaults."""
    local_client.config.CONFIG.pop("local_server_host", None)
    local_client.config.CONFIG.pop("local_server_port", None)
    assert local_client.get_api_url() == "http://127.0.0.1:8081"


# -- get_model --

def test_get_model_from_config(monkeypatch):
    """get_model extracts basename from model path."""
    monkeypatch.setitem(
        local_client.config.CONFIG, "local_model_path",
        "/home/pi/models/qwen2.5-1.5b-instruct-q4_k_m.gguf",
    )
    assert local_client.get_model() == "qwen2.5-1.5b-instruct-q4_k_m"


def test_get_model_default(monkeypatch):
    """get_model falls back to 'local' when no path configured."""
    monkeypatch.setitem(local_client.config.CONFIG, "local_model_path", "")
    assert local_client.get_model() == "local"


# -- Local downloadable models (via model registry) --

def test_local_downloadable_models_structure():
    """get_local_downloadable_models returns entries with expected structure."""
    from carpenter.core.models.registry import (
        get_local_downloadable_models,
        load_registry,
    )
    from pathlib import Path

    # Load from the bundled seed YAML so we test the real data
    seed_path = Path(__file__).resolve().parent.parent.parent / "config_seed" / "model-registry.yaml"
    load_registry(str(seed_path))

    catalog = get_local_downloadable_models()
    assert len(catalog) == 4
    for key, entry in catalog.items():
        assert "repo" in entry
        assert "filename" in entry
        assert "size_mb" in entry
        assert "label" in entry
        assert entry["filename"].endswith(".gguf")

    # Verify 0.5B model is gone
    assert "qwen2.5-0.5b-q4" not in catalog


def test_call_with_tools(monkeypatch):
    """call includes tools in request body when provided."""
    captured = {}

    def mock_post(url, **kwargs):
        import json as _json
        captured["body"] = _json.loads(kwargs["content"])
        mock_response = MagicMock()
        mock_response.json.return_value = _make_response()
        return mock_response

    monkeypatch.setattr("httpx.post", mock_post)
    monkeypatch.setitem(
        local_client.config.CONFIG, "local_server_host", "127.0.0.1",
    )
    monkeypatch.setitem(
        local_client.config.CONFIG, "local_server_port", 8081,
    )

    tools = [
        {"type": "function", "function": {"name": "read_file", "parameters": {}}},
    ]

    local_client.call(
        "System prompt",
        [{"role": "user", "content": "Hello"}],
        model="test-model",
        tools=tools,
    )

    assert captured["body"]["tools"] == tools


def test_call_without_tools(monkeypatch):
    """call omits tools from request body when not provided."""
    captured = {}

    def mock_post(url, **kwargs):
        import json as _json
        captured["body"] = _json.loads(kwargs["content"])
        mock_response = MagicMock()
        mock_response.json.return_value = _make_response()
        return mock_response

    monkeypatch.setattr("httpx.post", mock_post)
    monkeypatch.setitem(
        local_client.config.CONFIG, "local_server_host", "127.0.0.1",
    )
    monkeypatch.setitem(
        local_client.config.CONFIG, "local_server_port", 8081,
    )

    local_client.call(
        "System prompt",
        [{"role": "user", "content": "Hello"}],
        model="test-model",
    )

    assert "tools" not in captured["body"]
