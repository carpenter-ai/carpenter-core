"""Tests for carpenter.agent.api_standard."""

import pytest

from carpenter.agent import api_standard


# -- get_api_standard --

class TestGetApiStandard:
    """Tests for get_api_standard."""

    def test_anthropic_default(self):
        assert api_standard.get_api_standard("anthropic") == "anthropic"

    def test_ollama_default(self):
        assert api_standard.get_api_standard("ollama") == "openai"

    def test_local_default(self):
        assert api_standard.get_api_standard("local") == "openai"

    def test_tinfoil_default(self):
        assert api_standard.get_api_standard("tinfoil") == "openai"

    def test_unknown_provider_defaults_to_openai(self):
        assert api_standard.get_api_standard("unknown_provider") == "openai"

    def test_config_override(self, monkeypatch):
        """Config overrides built-in defaults."""
        monkeypatch.setitem(
            api_standard.config.CONFIG, "api_standards",
            {"ollama": "anthropic"},
        )
        assert api_standard.get_api_standard("ollama") == "anthropic"

    def test_config_partial_override(self, monkeypatch):
        """Config only overrides listed providers; others use defaults."""
        monkeypatch.setitem(
            api_standard.config.CONFIG, "api_standards",
            {"ollama": "anthropic"},
        )
        # local not in config override, should fall back to built-in default
        assert api_standard.get_api_standard("local") == "openai"


# -- convert_tools_for_provider --

class TestConvertToolsForProvider:
    """Tests for convert_tools_for_provider."""

    SAMPLE_TOOLS = [
        {
            "name": "read_file",
            "description": "Read a file.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    ]

    def test_none_returns_none(self):
        assert api_standard.convert_tools_for_provider(None, "anthropic") is None
        assert api_standard.convert_tools_for_provider(None, "openai") is None

    def test_anthropic_passthrough(self):
        result = api_standard.convert_tools_for_provider(self.SAMPLE_TOOLS, "anthropic")
        assert result is self.SAMPLE_TOOLS

    def test_openai_conversion(self):
        result = api_standard.convert_tools_for_provider(self.SAMPLE_TOOLS, "openai")
        assert len(result) == 1
        tool = result[0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "read_file"
        assert tool["function"]["description"] == "Read a file."
        assert tool["function"]["parameters"]["type"] == "object"
        assert "path" in tool["function"]["parameters"]["properties"]

    def test_openai_multiple_tools(self):
        tools = self.SAMPLE_TOOLS + [
            {
                "name": "get_state",
                "description": "Get state.",
                "input_schema": {"type": "object", "properties": {}},
            },
        ]
        result = api_standard.convert_tools_for_provider(tools, "openai")
        assert len(result) == 2
        assert result[0]["function"]["name"] == "read_file"
        assert result[1]["function"]["name"] == "get_state"


# -- normalize_response --

class TestNormalizeResponse:
    """Tests for normalize_response."""

    def test_anthropic_passthrough(self):
        resp = {
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = api_standard.normalize_response(resp, "anthropic")
        assert result is resp

    def test_openai_text_only(self):
        raw = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "model": "llama3.1",
        }
        result = api_standard.normalize_response(raw, "openai")
        assert result["content"] == [{"type": "text", "text": "Hello!"}]
        assert result["stop_reason"] == "end_turn"
        assert result["usage"] == {"input_tokens": 10, "output_tokens": 5}
        assert result["model"] == "llama3.1"

    def test_openai_with_tool_calls(self):
        raw = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Let me check.",
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "/tmp/x"}',
                                },
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 15},
            "model": "qwen2.5",
        }
        result = api_standard.normalize_response(raw, "openai")

        assert len(result["content"]) == 2
        assert result["content"][0] == {"type": "text", "text": "Let me check."}
        assert result["content"][1]["type"] == "tool_use"
        assert result["content"][1]["id"] == "call_123"
        assert result["content"][1]["name"] == "read_file"
        assert result["content"][1]["input"] == {"path": "/tmp/x"}
        assert result["stop_reason"] == "tool_use"

    def test_openai_empty_choices(self):
        raw = {"choices": [], "usage": {}, "model": "test"}
        result = api_standard.normalize_response(raw, "openai")
        assert result["content"] == []
        assert result["stop_reason"] == "end_turn"

    def test_openai_length_stop(self):
        raw = {
            "choices": [
                {
                    "message": {"content": "Truncated..."},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 4096},
        }
        result = api_standard.normalize_response(raw, "openai")
        assert result["stop_reason"] == "max_tokens"

    def test_openai_null_content(self):
        """When content is None (tool-only response), no text block added."""
        raw = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "get_state", "arguments": "{}"},
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        }
        result = api_standard.normalize_response(raw, "openai")
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "tool_use"

    def test_openai_tool_call_with_dict_arguments(self):
        """Arguments may already be a dict (some providers)."""
        raw = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": {"path": "/x"},
                                },
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {},
        }
        result = api_standard.normalize_response(raw, "openai")
        assert result["content"][0]["input"] == {"path": "/x"}


# -- format_tool_results_for_api --

class TestFormatToolResults:
    """Tests for format_tool_results_for_api."""

    SAMPLE_RESULTS = [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "file contents"},
        {"type": "tool_result", "tool_use_id": "tu_2", "content": "state value"},
    ]

    def test_anthropic_passthrough(self):
        result = api_standard.format_tool_results_for_api(
            self.SAMPLE_RESULTS, "anthropic"
        )
        assert result is self.SAMPLE_RESULTS

    def test_openai_conversion(self):
        result = api_standard.format_tool_results_for_api(
            self.SAMPLE_RESULTS, "openai"
        )
        assert len(result) == 2
        assert result[0] == {
            "role": "tool",
            "tool_call_id": "tu_1",
            "content": "file contents",
        }
        assert result[1] == {
            "role": "tool",
            "tool_call_id": "tu_2",
            "content": "state value",
        }


# -- format_assistant_tool_message --

class TestFormatAssistantToolMessage:
    """Tests for format_assistant_tool_message."""

    SAMPLE_BLOCKS = [
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "/x"}},
    ]

    def test_anthropic_format(self):
        result = api_standard.format_assistant_tool_message(
            self.SAMPLE_BLOCKS, "anthropic"
        )
        assert result == {"role": "assistant", "content": self.SAMPLE_BLOCKS}

    def test_openai_format(self):
        result = api_standard.format_assistant_tool_message(
            self.SAMPLE_BLOCKS, "openai"
        )
        assert result["role"] == "assistant"
        assert result["content"] == "Let me check."
        assert len(result["tool_calls"]) == 1
        tc = result["tool_calls"][0]
        assert tc["id"] == "tu_1"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "read_file"

    def test_openai_no_text(self):
        """Tool-only blocks produce None content."""
        blocks = [
            {"type": "tool_use", "id": "tu_1", "name": "get_state", "input": {}},
        ]
        result = api_standard.format_assistant_tool_message(blocks, "openai")
        assert result["content"] is None
        assert len(result["tool_calls"]) == 1

    def test_openai_no_tools(self):
        """Text-only blocks produce no tool_calls key."""
        blocks = [{"type": "text", "text": "Hello!"}]
        result = api_standard.format_assistant_tool_message(blocks, "openai")
        assert result["content"] == "Hello!"
        assert "tool_calls" not in result


# -- extract_code_from_text --

class TestExtractCodeFromText:
    """Tests for extract_code_from_text."""

    def test_finds_code_block(self):
        text = 'Here:\n\n```python\nprint("hello")\n```\n\nDone.'
        code = api_standard.extract_code_from_text(text)
        assert code == 'print("hello")\n'

    def test_returns_last_block(self):
        text = '```python\nfirst()\n```\n\nBetter:\n\n```python\nsecond()\n```'
        code = api_standard.extract_code_from_text(text)
        assert code == "second()\n"

    def test_returns_none_when_no_block(self):
        assert api_standard.extract_code_from_text("No code here") is None

    def test_strips_whitespace(self):
        text = '```python\n  x = 1\n```'
        code = api_standard.extract_code_from_text(text)
        assert code == "x = 1\n"
