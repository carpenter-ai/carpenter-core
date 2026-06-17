"""Tests for carpenter.agent.providers.anthropic."""

import json
import pytest
from unittest.mock import patch, MagicMock

from carpenter.agent.providers import anthropic as claude_client


def test_build_messages_basic():
    """build_messages returns proper structure."""
    messages, system = claude_client.build_messages(
        "You are helpful.",
        [{"role": "user", "content": "Hello"}],
    )
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello"
    assert system[0]["text"] == "You are helpful."
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_build_messages_no_cache():
    """build_messages without cache_control."""
    _, system = claude_client.build_messages(
        "Test", [], cache_control=False,
    )
    assert "cache_control" not in system[0]


def test_extract_text():
    """extract_text pulls text from response content blocks."""
    response = {
        "content": [
            {"type": "text", "text": "Hello world"},
            {"type": "text", "text": "Second block"},
        ]
    }
    assert claude_client.extract_text(response) == "Hello world\nSecond block"


def test_extract_text_empty():
    """extract_text handles empty content."""
    assert claude_client.extract_text({"content": []}) == ""
    assert claude_client.extract_text({}) == ""


def test_extract_code_from_text():
    """extract_code_from_text finds Python code blocks."""
    text = 'Here is the code:\n\n```python\nprint("hello")\n```\n\nDone.'
    code = claude_client.extract_code_from_text(text)
    assert code == 'print("hello")\n'


def test_extract_code_from_text_multiple():
    """extract_code_from_text returns the last code block."""
    text = '```python\nfirst()\n```\n\nBetter version:\n\n```python\nsecond()\n```'
    code = claude_client.extract_code_from_text(text)
    assert code == "second()\n"


def test_extract_code_from_text_none():
    """extract_code_from_text returns None when no code block."""
    assert claude_client.extract_code_from_text("No code here") is None


def test_extract_code():
    """extract_code works on full API response."""
    response = {
        "content": [
            {"type": "text", "text": '```python\nx = 1\n```'},
        ]
    }
    code = claude_client.extract_code(response)
    assert code == "x = 1\n"


def test_call_constructs_request(monkeypatch):
    """call sends correct headers and body structure."""
    captured = {}

    def mock_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        # Body is sent as pre-serialized bytes via content= kwarg
        import json as _json
        captured["body"] = _json.loads(kwargs["content"])
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"type": "text", "text": "response"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        mock_response.headers = {}
        return mock_response

    monkeypatch.setattr("httpx.post", mock_post)

    result = claude_client.call(
        "System prompt",
        [{"role": "user", "content": "Hello"}],
        api_key="test-key",
        model="claude-test",
        max_tokens=100,
    )

    assert captured["url"] == claude_client.API_URL
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["body"]["model"] == "claude-test"
    assert captured["body"]["max_tokens"] == 100
    assert len(captured["body"]["messages"]) == 1
    assert result["content"][0]["text"] == "response"


def test_call_omits_temperature_for_unsupported_model(monkeypatch):
    """call must NOT send `temperature` for models that reject it (opus-4-7)."""
    captured = {}

    def mock_post(url, **kwargs):
        import json as _json
        captured["body"] = _json.loads(kwargs["content"])
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        mock_response.headers = {}
        return mock_response

    monkeypatch.setattr("httpx.post", mock_post)

    claude_client.call(
        "system", [{"role": "user", "content": "hi"}],
        api_key="key", model="claude-opus-4-7", temperature=0.7,
    )

    assert "temperature" not in captured["body"]


def test_call_sends_temperature_for_supported_model(monkeypatch):
    """call must still send `temperature` for models that accept it."""
    captured = {}

    def mock_post(url, **kwargs):
        import json as _json
        captured["body"] = _json.loads(kwargs["content"])
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        mock_response.headers = {}
        return mock_response

    monkeypatch.setattr("httpx.post", mock_post)

    claude_client.call(
        "system", [{"role": "user", "content": "hi"}],
        api_key="key", model="claude-sonnet-4-6", temperature=0.42,
    )

    assert captured["body"]["temperature"] == 0.42


def test_supports_temperature_helper():
    """supports_temperature flags opus-4-7 unsupported, others supported."""
    assert claude_client.supports_temperature("claude-opus-4-7") is False
    assert claude_client.supports_temperature("claude-sonnet-4-6") is True
    assert claude_client.supports_temperature("claude-haiku-4-5") is True
    assert claude_client.supports_temperature(None) is True


def test_call_retries_without_temperature_on_400_deprecated(monkeypatch):
    """On a 400 'temperature is deprecated' error, call retries once stripping it."""
    calls = []

    def mock_post(url, **kwargs):
        import json as _json
        body = _json.loads(kwargs["content"])
        calls.append(body)
        if len(calls) == 1:
            # First attempt: simulate the 400 deprecation error
            err_response = MagicMock()
            err_response.status_code = 400
            err_response.json.return_value = {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "`temperature` is deprecated for this model.",
                },
            }
            err_response.text = "deprecated"
            import httpx as _httpx
            raise _httpx.HTTPStatusError(
                "400", request=MagicMock(), response=err_response)
        # Retry succeeds
        ok = MagicMock()
        ok.json.return_value = {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        ok.headers = {}
        return ok

    monkeypatch.setattr("httpx.post", mock_post)
    # Use a model NOT in the deny-set so temperature is sent on the first try
    result = claude_client.call(
        "system", [{"role": "user", "content": "hi"}],
        api_key="key", model="claude-future-9", temperature=0.7,
    )

    assert len(calls) == 2
    assert "temperature" in calls[0]  # first attempt sent it
    assert "temperature" not in calls[1]  # retry stripped it
    assert result["content"][0]["text"] == "ok"


def test_is_temperature_deprecated_error():
    """Helper detects the deprecation error message and ignores others."""
    assert claude_client._is_temperature_deprecated_error(
        {"error": {"message": "`temperature` is deprecated for this model."}}
    ) is True
    assert claude_client._is_temperature_deprecated_error(
        {"error": {"message": "some other error"}}
    ) is False
    assert claude_client._is_temperature_deprecated_error(None) is False


def test_build_messages_conversation_cache_breakpoint():
    """build_messages adds cache_control to penultimate user message in long conversations."""
    conversation = [
        {"role": "user", "content": "Message 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "Message 2"},
        {"role": "assistant", "content": "Reply 2"},
        {"role": "user", "content": "Message 3"},
    ]
    messages, _ = claude_client.build_messages("system", conversation)
    # The penultimate user message is "Message 2" at index 2
    assert isinstance(messages[2]["content"], list)
    assert messages[2]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert messages[2]["content"][0]["text"] == "Message 2"
    # The final user message should NOT have cache_control
    assert messages[4]["content"] == "Message 3"


def test_build_messages_no_cache_breakpoint_short_conversation():
    """build_messages doesn't add conversation cache for < 4 messages."""
    conversation = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
        {"role": "user", "content": "Bye"},
    ]
    messages, _ = claude_client.build_messages("system", conversation)
    # All content should remain plain strings
    for m in messages:
        assert isinstance(m["content"], str)


def test_build_messages_structured_content_cache_breakpoint():
    """build_messages adds cache_control to structured content blocks."""
    tool_result_blocks = [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
    ]
    conversation = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": tool_result_blocks},  # structured
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q2"},
    ]
    messages, _ = claude_client.build_messages("system", conversation)
    # Penultimate user msg is at index 2 (structured)
    cached_block = messages[2]["content"][-1]
    assert cached_block.get("cache_control") == {"type": "ephemeral"}


def test_call_adds_cache_control_to_last_tool(monkeypatch):
    """call adds cache_control to the last tool definition."""
    captured = {}

    def mock_post(url, **kwargs):
        import json as _json
        captured["body"] = _json.loads(kwargs["content"])
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        mock_response.headers = {}
        return mock_response

    monkeypatch.setattr("httpx.post", mock_post)

    tools = [
        {"name": "tool_a", "description": "A", "input_schema": {"type": "object", "properties": {}}},
        {"name": "tool_b", "description": "B", "input_schema": {"type": "object", "properties": {}}},
    ]
    claude_client.call(
        "system", [{"role": "user", "content": "hi"}],
        api_key="key", tools=tools,
    )

    sent_tools = captured["body"]["tools"]
    assert "cache_control" not in sent_tools[0]
    assert sent_tools[-1]["cache_control"] == {"type": "ephemeral"}
    # Original tools list should not be mutated
    assert "cache_control" not in tools[-1]
