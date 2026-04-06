"""Tests for carpenter.agent.providers.ollama."""

import pytest
from unittest.mock import MagicMock

from carpenter.agent.providers import ollama as ollama_client


# -- Fixtures for common response shapes --

def _make_response(text="Hello from Ollama"):
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
    assert ollama_client.extract_text(response) == "Hello world"


def test_extract_text_empty_choices():
    """extract_text handles empty choices list."""
    assert ollama_client.extract_text({"choices": []}) == ""


def test_extract_text_no_choices():
    """extract_text handles missing choices key."""
    assert ollama_client.extract_text({}) == ""


# -- extract_code --

def test_extract_code():
    """extract_code finds Python code block in response."""
    text = 'Here is the code:\n\n```python\nprint("hello")\n```\n\nDone.'
    response = _make_response(text)
    code = ollama_client.extract_code(response)
    assert code == 'print("hello")\n'


def test_extract_code_no_block():
    """extract_code returns None when no code block present."""
    response = _make_response("No code here, just text.")
    assert ollama_client.extract_code(response) is None


# -- extract_code_from_text --

def test_extract_code_from_text():
    """extract_code_from_text finds Python code blocks."""
    text = 'Here is the code:\n\n```python\nprint("hello")\n```\n\nDone.'
    code = ollama_client.extract_code_from_text(text)
    assert code == 'print("hello")\n'


def test_extract_code_from_text_multiple():
    """extract_code_from_text returns the last code block."""
    text = '```python\nfirst()\n```\n\nBetter version:\n\n```python\nsecond()\n```'
    code = ollama_client.extract_code_from_text(text)
    assert code == "second()\n"


def test_extract_code_from_text_none():
    """extract_code_from_text returns None when no code block."""
    assert ollama_client.extract_code_from_text("No code here") is None


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
        ollama_client.config.CONFIG, "ollama_url", "http://localhost:11434",
    )
    monkeypatch.setitem(
        ollama_client.config.CONFIG, "ollama_model", "testmodel",
    )

    result = ollama_client.call(
        "System prompt",
        [{"role": "user", "content": "Hello"}],
        model="testmodel",
        max_tokens=100,
        temperature=0.5,
    )

    # Verify URL
    assert captured["url"] == "http://localhost:11434/v1/chat/completions"

    # Verify body structure
    body = captured["body"]
    assert body["model"] == "testmodel"
    assert body["max_tokens"] == 100
    assert body["temperature"] == 0.5

    # Verify messages include system prompt first
    assert body["messages"][0] == {"role": "system", "content": "System prompt"}
    assert body["messages"][1] == {"role": "user", "content": "Hello"}

    # Verify timeout is long
    assert captured["timeout"] == 300.0

    # Verify response
    assert result["choices"][0]["message"]["content"] == "test response"


def test_call_timeout(monkeypatch):
    """call propagates timeout exceptions."""
    import httpx

    def mock_post(url, **kwargs):
        raise httpx.TimeoutException("Connection timed out")

    monkeypatch.setattr("httpx.post", mock_post)
    monkeypatch.setitem(
        ollama_client.config.CONFIG, "ollama_url", "http://localhost:11434",
    )

    with pytest.raises(httpx.TimeoutException):
        ollama_client.call(
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
        ollama_client.config.CONFIG, "ollama_url", "http://localhost:11434",
    )
    monkeypatch.setitem(
        ollama_client.config.CONFIG, "ollama_model", "llama3.1",
    )

    ollama_client.call("sys", [{"role": "user", "content": "hi"}])

    assert captured["body"]["model"] == "llama3.1"
    assert captured["body"]["max_tokens"] == ollama_client.DEFAULT_MAX_TOKENS


# -- get_api_url --

def test_get_api_url_from_config(monkeypatch):
    """get_api_url reads ollama_url from config."""
    monkeypatch.setitem(
        ollama_client.config.CONFIG, "ollama_url", "http://myhost:9999",
    )
    assert ollama_client.get_api_url() == "http://myhost:9999"


def test_get_api_url_default(monkeypatch):
    """get_api_url falls back to default when config key is missing."""
    # Remove the key if present
    ollama_client.config.CONFIG.pop("ollama_url", None)
    assert ollama_client.get_api_url() == ollama_client.DEFAULT_URL


# -- get_model --

def test_get_model_from_config(monkeypatch):
    """get_model reads ollama_model from config."""
    monkeypatch.setitem(
        ollama_client.config.CONFIG, "ollama_model", "codellama",
    )
    assert ollama_client.get_model() == "codellama"


def test_get_model_default(monkeypatch):
    """get_model falls back to default when config key is missing."""
    ollama_client.config.CONFIG.pop("ollama_model", None)
    assert ollama_client.get_model() == ollama_client.DEFAULT_MODEL
