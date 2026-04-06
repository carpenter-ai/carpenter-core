"""Tests for carpenter.agent.providers.tinfoil."""

import sys
from unittest.mock import MagicMock

import pytest

from carpenter.agent.providers import tinfoil as tinfoil_client


# -- Helpers --

def _make_openai_response(text="Hello from Tinfoil"):
    """Build a mock response that .model_dump() returns an OpenAI-format dict."""
    response = MagicMock()
    response.model_dump.return_value = {
        "choices": [
            {"message": {"role": "assistant", "content": text}},
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        },
    }
    return response


def _make_tinfoil_module(mock_response):
    """Return a mock tinfoil module whose TinfoilAI client returns mock_response."""
    mock_client_instance = MagicMock()
    mock_client_instance.chat.completions.create.return_value = mock_response

    mock_tinfoil = MagicMock()
    mock_tinfoil.TinfoilAI.return_value = mock_client_instance
    return mock_tinfoil, mock_client_instance


# -- extract_text --

def test_extract_text():
    """extract_text pulls text from OpenAI-format response."""
    response = {"choices": [{"message": {"content": "Hello world"}}]}
    assert tinfoil_client.extract_text(response) == "Hello world"


def test_extract_text_empty_choices():
    """extract_text handles empty choices list."""
    assert tinfoil_client.extract_text({"choices": []}) == ""


def test_extract_text_no_choices():
    """extract_text handles missing choices key."""
    assert tinfoil_client.extract_text({}) == ""


def test_extract_text_none_content():
    """extract_text returns empty string when content is None."""
    response = {"choices": [{"message": {"content": None}}]}
    assert tinfoil_client.extract_text(response) == ""


# -- extract_code --

def test_extract_code():
    """extract_code finds Python code block in response."""
    text = 'Here is the code:\n\n```python\nprint("hello")\n```\n\nDone.'
    response = {"choices": [{"message": {"content": text}}]}
    code = tinfoil_client.extract_code(response)
    assert code == 'print("hello")\n'


def test_extract_code_no_block():
    """extract_code returns None when no code block present."""
    response = {"choices": [{"message": {"content": "No code here."}}]}
    assert tinfoil_client.extract_code(response) is None


# -- extract_code_from_text --

def test_extract_code_from_text():
    """extract_code_from_text finds Python code blocks."""
    text = 'Here is the code:\n\n```python\nprint("hello")\n```\n\nDone.'
    code = tinfoil_client.extract_code_from_text(text)
    assert code == 'print("hello")\n'


def test_extract_code_from_text_multiple():
    """extract_code_from_text returns the last code block."""
    text = '```python\nfirst()\n```\n\nBetter version:\n\n```python\nsecond()\n```'
    code = tinfoil_client.extract_code_from_text(text)
    assert code == "second()\n"


def test_extract_code_from_text_none():
    """extract_code_from_text returns None when no code block."""
    assert tinfoil_client.extract_code_from_text("No code here") is None


# -- call --

def test_call_success(monkeypatch):
    """call creates TinfoilAI client using TINFOIL_API_KEY env var."""
    mock_response = _make_openai_response("test response")
    mock_tinfoil, mock_client_instance = _make_tinfoil_module(mock_response)

    monkeypatch.setitem(sys.modules, "tinfoil", mock_tinfoil)
    monkeypatch.setenv("TINFOIL_API_KEY", "test-env-key")
    monkeypatch.setitem(tinfoil_client.config.CONFIG, "tinfoil_model", "llama3-3-70b")

    result = tinfoil_client.call(
        "System prompt",
        [{"role": "user", "content": "Hello"}],
        model="llama3-3-70b",
        max_tokens=100,
        temperature=0.5,
    )

    # Verify TinfoilAI was constructed with the API key from env
    mock_tinfoil.TinfoilAI.assert_called_once_with(api_key="test-env-key")

    # Verify chat.completions.create was called with correct args
    create_call = mock_client_instance.chat.completions.create.call_args
    assert create_call.kwargs["model"] == "llama3-3-70b"
    assert create_call.kwargs["max_tokens"] == 100
    assert create_call.kwargs["temperature"] == 0.5

    # Messages should include system prompt first
    messages = create_call.kwargs["messages"]
    assert messages[0] == {"role": "system", "content": "System prompt"}
    assert messages[1] == {"role": "user", "content": "Hello"}

    # Return value should be the model_dump() output
    assert result["choices"][0]["message"]["content"] == "test response"


def test_call_uses_config_defaults(monkeypatch):
    """call uses model and max_tokens from config when not provided."""
    mock_response = _make_openai_response()
    mock_tinfoil, mock_client_instance = _make_tinfoil_module(mock_response)

    monkeypatch.setitem(sys.modules, "tinfoil", mock_tinfoil)
    monkeypatch.setenv("TINFOIL_API_KEY", "key")
    monkeypatch.setitem(tinfoil_client.config.CONFIG, "tinfoil_model", "custom-model")
    monkeypatch.setitem(tinfoil_client.config.CONFIG, "tinfoil_max_tokens", 2048)

    tinfoil_client.call("sys", [{"role": "user", "content": "hi"}])

    create_call = mock_client_instance.chat.completions.create.call_args
    assert create_call.kwargs["model"] == "custom-model"
    assert create_call.kwargs["max_tokens"] == 2048


def test_call_api_key_config_fallback(monkeypatch):
    """call falls back to tinfoil_api_key config key when env var is not set."""
    mock_response = _make_openai_response()
    mock_tinfoil, _ = _make_tinfoil_module(mock_response)

    monkeypatch.setitem(sys.modules, "tinfoil", mock_tinfoil)
    monkeypatch.delenv("TINFOIL_API_KEY", raising=False)
    monkeypatch.setitem(tinfoil_client.config.CONFIG, "tinfoil_api_key", "config-key")

    tinfoil_client.call("sys", [{"role": "user", "content": "hi"}])

    mock_tinfoil.TinfoilAI.assert_called_once_with(api_key="config-key")


def test_call_raises_import_error_when_tinfoil_not_installed(monkeypatch):
    """call raises ImportError with helpful message when tinfoil is not installed."""
    monkeypatch.setitem(sys.modules, "tinfoil", None)
    monkeypatch.setenv("TINFOIL_API_KEY", "key")

    with pytest.raises(ImportError, match="pip install tinfoil"):
        tinfoil_client.call("sys", [{"role": "user", "content": "hi"}])


def test_call_raises_value_error_when_no_api_key(monkeypatch):
    """call raises ValueError when neither TINFOIL_API_KEY env var nor config key is set."""
    mock_tinfoil = MagicMock()
    monkeypatch.setitem(sys.modules, "tinfoil", mock_tinfoil)
    monkeypatch.delenv("TINFOIL_API_KEY", raising=False)
    monkeypatch.setitem(tinfoil_client.config.CONFIG, "tinfoil_api_key", "")

    with pytest.raises(ValueError, match="tinfoil_api_key"):
        tinfoil_client.call("sys", [{"role": "user", "content": "hi"}])


def test_call_retries_on_server_error(monkeypatch):
    """call retries on 5xx-equivalent errors."""
    mock_tinfoil = MagicMock()
    error = Exception("Server error")
    # No status_code attribute = treated as retryable
    mock_tinfoil.TinfoilAI.return_value.chat.completions.create.side_effect = error

    monkeypatch.setitem(sys.modules, "tinfoil", mock_tinfoil)
    monkeypatch.setenv("TINFOIL_API_KEY", "key")
    monkeypatch.setitem(tinfoil_client.config.CONFIG, "retry_max_attempts", 2)
    monkeypatch.setitem(tinfoil_client.config.CONFIG, "retry_base_delay", 0.0)
    monkeypatch.setattr("time.sleep", lambda _: None)

    with pytest.raises(Exception, match="Server error"):
        tinfoil_client.call("sys", [{"role": "user", "content": "hi"}])

    # Should have been called twice (max_attempts=2)
    assert mock_tinfoil.TinfoilAI.return_value.chat.completions.create.call_count == 2


def test_call_does_not_retry_on_4xx(monkeypatch):
    """call does not retry on 4xx errors."""
    mock_tinfoil = MagicMock()
    error = Exception("Unauthorized")
    error.status_code = 401
    mock_tinfoil.TinfoilAI.return_value.chat.completions.create.side_effect = error

    monkeypatch.setitem(sys.modules, "tinfoil", mock_tinfoil)
    monkeypatch.setenv("TINFOIL_API_KEY", "key")
    monkeypatch.setitem(tinfoil_client.config.CONFIG, "retry_max_attempts", 3)
    monkeypatch.setitem(tinfoil_client.config.CONFIG, "retry_base_delay", 0.0)

    with pytest.raises(Exception, match="Unauthorized"):
        tinfoil_client.call("sys", [{"role": "user", "content": "hi"}])

    # Should have been called only once (no retry on 4xx)
    assert mock_tinfoil.TinfoilAI.return_value.chat.completions.create.call_count == 1


# -- get_model --

def test_get_model_from_config(monkeypatch):
    """get_model reads tinfoil_model from config."""
    monkeypatch.setitem(tinfoil_client.config.CONFIG, "tinfoil_model", "kimi-k2-5")
    assert tinfoil_client.get_model() == "kimi-k2-5"


def test_get_model_default(monkeypatch):
    """get_model falls back to DEFAULT_MODEL when config key is missing."""
    tinfoil_client.config.CONFIG.pop("tinfoil_model", None)
    assert tinfoil_client.get_model() == tinfoil_client.DEFAULT_MODEL
