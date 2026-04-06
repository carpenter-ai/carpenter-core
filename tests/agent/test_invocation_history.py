"""Tests for history conversion and Ollama multi-turn tool use in carpenter.agent.invocation.

Moved from test_invocation.py — covers _convert_history_to_standard and
Ollama multi-turn tool use regression tests.
"""

from unittest.mock import patch, MagicMock

import pytest

from carpenter.agent import invocation, conversation


# ---------------------------------------------------------------------------
# _convert_history_to_standard — pure unit tests (no DB, no mocking)
# ---------------------------------------------------------------------------

class TestConvertHistoryToStandard:
    """Unit tests for _convert_history_to_standard."""

    def test_anthropic_is_passthrough(self):
        """Anthropic standard returns the exact same list objects."""
        msgs = [{"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "n", "input": {}}]}]
        ids = [42]
        out_msgs, out_ids = invocation._convert_history_to_standard(msgs, "anthropic", ids)
        assert out_msgs is msgs
        assert out_ids is ids

    def test_converts_assistant_tool_use_to_openai(self):
        """Assistant message with tool_use blocks becomes tool_calls format."""
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check."},
                    {"type": "tool_use", "id": "tu_1", "name": "get_state", "input": {"key": "x"}},
                ],
            }
        ]
        ids = [10]
        out_msgs, out_ids = invocation._convert_history_to_standard(msgs, "openai", ids)
        assert len(out_msgs) == 1
        msg = out_msgs[0]
        assert msg["role"] == "assistant"
        assert msg["content"] == "Let me check."
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["id"] == "tu_1"
        assert msg["tool_calls"][0]["function"]["name"] == "get_state"
        assert out_ids == [10]

    def test_tool_use_only_content_becomes_none(self):
        """Tool-only assistant message produces content=None (OpenAI spec)."""
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "get_state", "input": {}},
                ],
            }
        ]
        out_msgs, _ = invocation._convert_history_to_standard(msgs, "openai", [1])
        assert out_msgs[0]["content"] is None

    def test_converts_user_tool_result_to_role_tool(self):
        """User message whose content is tool_result blocks becomes role:tool messages."""
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "result text"},
                ],
            }
        ]
        ids = [7]
        out_msgs, out_ids = invocation._convert_history_to_standard(msgs, "openai", ids)
        assert len(out_msgs) == 1
        assert out_msgs[0]["role"] == "tool"
        assert out_msgs[0]["tool_call_id"] == "tu_1"
        assert out_msgs[0]["content"] == "result text"
        assert out_ids == [7]

    def test_multiple_tool_results_expand_to_separate_messages(self):
        """One user message with N tool_results becomes N role:tool messages."""
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "r1"},
                    {"type": "tool_result", "tool_use_id": "tu_2", "content": "r2"},
                    {"type": "tool_result", "tool_use_id": "tu_3", "content": "r3"},
                ],
            }
        ]
        out_msgs, out_ids = invocation._convert_history_to_standard(msgs, "openai", [99])
        assert len(out_msgs) == 3
        assert all(m["role"] == "tool" for m in out_msgs)
        assert [m["tool_call_id"] for m in out_msgs] == ["tu_1", "tu_2", "tu_3"]

    def test_expanded_ids_first_preserved_rest_none(self):
        """After expansion, only the first slot keeps the original DB id."""
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "r1"},
                    {"type": "tool_result", "tool_use_id": "tu_2", "content": "r2"},
                ],
            }
        ]
        _, out_ids = invocation._convert_history_to_standard(msgs, "openai", [42])
        assert out_ids == [42, None]

    def test_plain_text_messages_pass_through_unchanged(self):
        """Plain user and assistant text messages are not modified."""
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        ids = [1, 2]
        out_msgs, out_ids = invocation._convert_history_to_standard(msgs, "openai", ids)
        assert out_msgs == msgs
        assert out_ids == ids

    def test_mixed_history_full_turn(self):
        """Full tool-use round-trip in history converts correctly end-to-end."""
        msgs = [
            {"role": "user", "content": "Search for X"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "kb_search", "input": {"query": "X"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "Found: X entry"},
                ],
            },
            {"role": "assistant", "content": "Here is what I found about X."},
        ]
        ids = [1, 2, 3, 4]
        out_msgs, out_ids = invocation._convert_history_to_standard(msgs, "openai", ids)

        # Length preserved (no expansion here — single tool call, single result)
        assert len(out_msgs) == 4
        assert len(out_ids) == 4

        # User message unchanged
        assert out_msgs[0] == {"role": "user", "content": "Search for X"}
        assert out_ids[0] == 1

        # Assistant tool_use → tool_calls
        assert out_msgs[1]["role"] == "assistant"
        assert "tool_calls" in out_msgs[1]
        assert out_msgs[1]["tool_calls"][0]["id"] == "tu_1"
        assert out_ids[1] == 2

        # tool_result → role:tool
        assert out_msgs[2]["role"] == "tool"
        assert out_msgs[2]["tool_call_id"] == "tu_1"
        assert out_msgs[2]["content"] == "Found: X entry"
        assert out_ids[2] == 3

        # Final assistant text unchanged
        assert out_msgs[3] == {"role": "assistant", "content": "Here is what I found about X."}
        assert out_ids[3] == 4

    def test_ids_length_always_matches_messages(self):
        """Invariant: len(out_msgs) == len(out_ids) for any conversion."""
        # Single tool result (1→1, no expansion)
        msgs_single = [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r"}]},
        ]
        out_m, out_i = invocation._convert_history_to_standard(msgs_single, "openai", [5])
        assert len(out_m) == len(out_i)

        # Three tool results (1→3 expansion)
        msgs_multi = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": f"t{i}", "content": f"r{i}"}
                    for i in range(3)
                ],
            }
        ]
        out_m, out_i = invocation._convert_history_to_standard(msgs_multi, "openai", [10])
        assert len(out_m) == len(out_i) == 3


# ---------------------------------------------------------------------------
# Ollama multi-turn tool use — regression tests for the format-mismatch bug
# ---------------------------------------------------------------------------

def _ollama_tool_response(tool_id, tool_name, tool_args_json, text=None):
    """Return a raw Ollama (OpenAI-format) tool-use response."""
    message = {
        "role": "assistant",
        "content": text,
        "tool_calls": [{
            "id": tool_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": tool_args_json},
        }],
    }
    return {
        "choices": [{"message": message, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 80, "completion_tokens": 20},
    }


def _ollama_text_response(text):
    """Return a raw Ollama (OpenAI-format) plain-text response."""
    return {
        "choices": [
            {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 30},
    }


class TestOllamaMultiTurnToolUse:
    """Regression tests for Ollama multi-turn tool use (400 bug fix).

    Before the fix, history messages loaded from the DB were sent to Ollama
    in canonical (Anthropic) format, causing a 400 on the second tool-calling
    turn. _convert_history_to_standard() now converts them to OpenAI format.
    """

    @patch("carpenter.agent.invocation.ollama_client")
    def test_second_turn_history_sent_in_openai_format(self, mock_ollama, monkeypatch):
        """On a second invoke_for_chat call, the history tool messages are in
        OpenAI format (tool_calls / role:tool) rather than Anthropic format
        (content:[{type:tool_use}] / content:[{type:tool_result}])."""
        monkeypatch.setitem(invocation.config.CONFIG, "ai_provider", "ollama")

        # Turn 1: tool call then final answer
        mock_ollama.call.side_effect = [
            _ollama_tool_response("call_1", "get_state", '{"key": "foo"}'),
            _ollama_text_response("The value is bar."),
            # Turn 2: a simple final answer (no more tool calls)
            _ollama_text_response("Anything else you need?"),
        ]

        # Turn 1
        result1 = invocation.invoke_for_chat("What is foo?")
        conv_id = result1["conversation_id"]
        assert result1["response_text"] == "The value is bar."

        # Turn 2 on the same conversation
        result2 = invocation.invoke_for_chat("Thanks, what else can you tell me?",
                                              conversation_id=conv_id)
        assert result2["response_text"] == "Anything else you need?"

        # The 3rd call to ollama (first call of turn 2) receives the history.
        # Positional args: call(system, messages, ...)
        assert mock_ollama.call.call_count == 3
        turn2_messages = mock_ollama.call.call_args_list[2][0][1]

        # Find the assistant message that used a tool in the history
        tool_assistant_msgs = [
            m for m in turn2_messages
            if m.get("role") == "assistant" and "tool_calls" in m
        ]
        assert tool_assistant_msgs, (
            "Expected an assistant message with tool_calls in turn-2 history; "
            f"got roles: {[m.get('role') for m in turn2_messages]}"
        )
        tc = tool_assistant_msgs[0]["tool_calls"][0]
        assert tc["id"] == "call_1"
        assert tc["function"]["name"] == "get_state"

        # There must be no Anthropic-format tool_use blocks in any message
        for msg in turn2_messages:
            content = msg.get("content")
            if isinstance(content, list):
                types = {b.get("type") for b in content}
                assert "tool_use" not in types, (
                    f"Found Anthropic tool_use block in turn-2 message: {msg}"
                )
                assert "tool_result" not in types, (
                    f"Found Anthropic tool_result block in turn-2 message: {msg}"
                )

    @patch("carpenter.agent.invocation.ollama_client")
    def test_second_turn_tool_result_history_as_role_tool(self, mock_ollama, monkeypatch):
        """Tool result history appears as role:'tool' messages, not user messages
        with tool_result content blocks."""
        monkeypatch.setitem(invocation.config.CONFIG, "ai_provider", "ollama")

        mock_ollama.call.side_effect = [
            _ollama_tool_response("call_2", "get_state", '{"key": "bar"}'),
            _ollama_text_response("Done."),
            _ollama_text_response("Follow-up done."),
        ]

        result1 = invocation.invoke_for_chat("Check bar?")
        result2 = invocation.invoke_for_chat("Follow up.", conversation_id=result1["conversation_id"])

        turn2_messages = mock_ollama.call.call_args_list[2][0][1]

        tool_messages = [m for m in turn2_messages if m.get("role") == "tool"]
        assert tool_messages, (
            "Expected role:'tool' messages in turn-2 history; "
            f"got roles: {[m.get('role') for m in turn2_messages]}"
        )
        assert tool_messages[0]["tool_call_id"] == "call_2"

        # No user message should have a list content containing tool_result blocks
        for msg in turn2_messages:
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, list):
                    assert not any(b.get("type") == "tool_result" for b in content), (
                        f"Found tool_result in user message content: {msg}"
                    )
