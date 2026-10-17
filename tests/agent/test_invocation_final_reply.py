"""Tests for tool-supplied final replies (``FinalReply``).

A chat tool can return ``FinalReply(text)`` to make ``text`` the
user-facing reply for the turn. The invocation loop persists it verbatim
and ends the turn without another model call. Plain results (including
errors) keep the normal flow.
"""

import json
from unittest.mock import patch

import pytest

from carpenter import chat_tool_loader
from carpenter.agent import conversation, invocation
from carpenter.chat_tool_loader import FinalReply, register_extension_tool


_SCHEMA = {
    "type": "object",
    "properties": {"item": {"type": "string"}},
    "required": ["item"],
}

ACK = "Order #42 submitted. You will get a message when it is confirmed."


@pytest.fixture
def tool_handlers():
    """Register extension tools whose behaviour each test controls."""
    behaviour = {}

    def make_handler(name):
        def handler(tool_input, **kwargs):
            result = behaviour[name]
            if isinstance(result, Exception):
                raise result
            return result
        return handler

    names = ("submit_order", "lookup_item")
    for name in names:
        register_extension_tool(
            name=name,
            description=f"Test tool {name}.",
            input_schema=_SCHEMA,
            handler=make_handler(name),
        )
    yield behaviour
    for name in names:
        chat_tool_loader._loaded_tools.pop(name, None)


def _tool_use(*calls, text=None):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for tool_id, name in calls:
        content.append({
            "type": "tool_use", "id": tool_id, "name": name,
            "input": {"item": "widget"},
        })
    return {
        "content": content,
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def _text(text):
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 200, "output_tokens": 30},
    }


def _assistant_texts(conv_id):
    return [
        m["content"] for m in conversation.get_messages(conv_id)
        if m["role"] == "assistant"
    ]


class TestFinalReplyType:
    def test_is_a_str(self):
        r = FinalReply("hello")
        assert isinstance(r, str)
        assert r == "hello"
        assert json.dumps({"c": r}) == '{"c": "hello"}'

    def test_is_final_reply_helper(self):
        assert invocation._is_final_reply(FinalReply("done"))
        assert not invocation._is_final_reply("done")
        assert not invocation._is_final_reply(FinalReply("   "))
        assert not invocation._is_final_reply(None)


@patch("carpenter.agent.invocation.claude_client")
class TestFinalReplyLoop:
    def test_final_reply_ends_turn_verbatim(self, mock_client, tool_handlers):
        tool_handlers["submit_order"] = FinalReply(ACK)
        mock_client.call.side_effect = [_tool_use(("tu_1", "submit_order"))]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("Order a widget", api_key="k")

        # No second model call.
        assert mock_client.call.call_count == 1
        assert result["response_text"] == ACK

        messages = conversation.get_messages(result["conversation_id"])
        roles = [m["role"] for m in messages]
        assert roles == ["user", "tool_call", "tool_result", "assistant"]
        assert messages[-1]["content"] == ACK
        assert result["message_id"] == messages[-1]["id"]

        # The model still sees the text as the tool result.
        blocks = json.loads(messages[2]["content_json"])
        assert blocks == [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": ACK},
        ]

    def test_error_result_continues_normal_flow(self, mock_client, tool_handlers):
        tool_handlers["submit_order"] = "Error: the shop is closed."
        mock_client.call.side_effect = [
            _tool_use(("tu_1", "submit_order")),
            _text("Sorry, the shop is closed right now."),
        ]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("Order a widget", api_key="k")

        assert mock_client.call.call_count == 2
        assert _assistant_texts(result["conversation_id"]) == [
            "Sorry, the shop is closed right now.",
        ]

    def test_raising_tool_continues_normal_flow(self, mock_client, tool_handlers):
        tool_handlers["submit_order"] = RuntimeError("backend down")
        mock_client.call.side_effect = [
            _tool_use(("tu_1", "submit_order")),
            _text("The order could not be placed."),
        ]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("Order a widget", api_key="k")

        assert mock_client.call.call_count == 2
        assert _assistant_texts(result["conversation_id"]) == [
            "The order could not be placed.",
        ]

    def test_empty_final_reply_is_a_plain_result(self, mock_client, tool_handlers):
        tool_handlers["submit_order"] = FinalReply("  ")
        mock_client.call.side_effect = [
            _tool_use(("tu_1", "submit_order")),
            _text("Done."),
        ]
        mock_client.extract_code_from_text.return_value = None

        invocation.invoke_for_chat("Order a widget", api_key="k")

        assert mock_client.call.call_count == 2

    def test_preamble_text_is_kept_and_reply_is_last(self, mock_client, tool_handlers):
        tool_handlers["submit_order"] = FinalReply(ACK)
        mock_client.call.side_effect = [
            _tool_use(("tu_1", "submit_order"), text="Placing the order."),
        ]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("Order a widget", api_key="k")

        assert mock_client.call.call_count == 1
        assert _assistant_texts(result["conversation_id"]) == [
            "Placing the order.", ACK,
        ]

    def test_batch_mixed_with_plain_tool(self, mock_client, tool_handlers):
        tool_handlers["lookup_item"] = "widget: in stock"
        tool_handlers["submit_order"] = FinalReply(ACK)
        mock_client.call.side_effect = [
            _tool_use(("tu_a", "lookup_item"), ("tu_b", "submit_order")),
        ]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("Order a widget", api_key="k")

        assert mock_client.call.call_count == 1
        conv_id = result["conversation_id"]
        assert _assistant_texts(conv_id) == [ACK]

        # Every tool_use in the batch has its tool_result.
        tr = [m for m in conversation.get_messages(conv_id)
              if m["role"] == "tool_result"]
        assert len(tr) == 1
        ids = [b["tool_use_id"] for b in json.loads(tr[0]["content_json"])]
        assert ids == ["tu_a", "tu_b"]

    def test_batch_multiple_final_replies_join_in_call_order(
        self, mock_client, tool_handlers,
    ):
        tool_handlers["lookup_item"] = FinalReply("First.")
        tool_handlers["submit_order"] = FinalReply("Second.")
        mock_client.call.side_effect = [
            _tool_use(("tu_a", "submit_order"), ("tu_b", "lookup_item")),
        ]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("Order a widget", api_key="k")

        assert _assistant_texts(result["conversation_id"]) == [
            "Second.\n\nFirst.",
        ]

    def test_long_reply_reaches_user_untruncated(
        self, mock_client, tool_handlers, monkeypatch,
    ):
        from carpenter import config
        monkeypatch.setitem(config.CONFIG, "tool_output_max_bytes", 200)
        long_reply = "\n".join(f"line {i}" for i in range(200))
        tool_handlers["submit_order"] = FinalReply(long_reply)
        mock_client.call.side_effect = [_tool_use(("tu_1", "submit_order"))]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("Order a widget", api_key="k")

        conv_id = result["conversation_id"]
        assert _assistant_texts(conv_id) == [long_reply]
        tr = [m for m in conversation.get_messages(conv_id)
              if m["role"] == "tool_result"][0]
        stored = json.loads(tr["content_json"])[0]["content"]
        assert "truncated" in stored

    def test_arc_step_treats_final_reply_as_plain_result(
        self, mock_client, tool_handlers,
    ):
        """In an arc step the loop keeps going until the model finishes."""
        from carpenter.core.arcs import manager as arc_manager

        arc_id = arc_manager.create_arc(name="step")
        tool_handlers["submit_order"] = FinalReply(ACK)
        mock_client.call.side_effect = [
            _tool_use(("tu_1", "submit_order")),
            _text("Step complete: order submitted."),
        ]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat(
            "Run the step", api_key="k", _executor_arc_id=arc_id,
        )

        assert mock_client.call.call_count == 2
        assert _assistant_texts(result["conversation_id"]) == [
            "Step complete: order submitted.",
        ]

    def test_history_is_well_formed_on_next_turn(self, mock_client, tool_handlers):
        tool_handlers["submit_order"] = FinalReply(ACK)
        mock_client.call.side_effect = [
            _tool_use(("tu_1", "submit_order")),
            _text("It is still pending."),
        ]
        mock_client.extract_code_from_text.return_value = None

        first = invocation.invoke_for_chat("Order a widget", api_key="k")
        conv_id = first["conversation_id"]
        invocation.invoke_for_chat(
            "Any news?", conversation_id=conv_id, api_key="k",
        )

        assert mock_client.call.call_count == 2
        sent = mock_client.call.call_args_list[1]
        messages = sent.kwargs.get("messages")
        if messages is None:
            messages = sent.args[1]
        roles = [m["role"] for m in messages]
        # Strict alternation, and the tool_use is answered by its tool_result.
        assert all(a != b for a, b in zip(roles, roles[1:]))
        for i, m in enumerate(messages):
            if isinstance(m["content"], list) and any(
                b.get("type") == "tool_use" for b in m["content"]
            ):
                nxt = messages[i + 1]["content"]
                assert any(
                    b.get("type") == "tool_result" and b["tool_use_id"] == "tu_1"
                    for b in nxt
                )
        texts = [m["content"] for m in messages if m["role"] == "assistant"
                 and isinstance(m["content"], str)]
        assert ACK in texts


class _CaptureChannel:
    """Factory for a minimal ChannelConnector that records sends."""

    @staticmethod
    def make():
        from carpenter.channels.base import HealthStatus
        from carpenter.channels.channel import ChannelConnector

        class Capture(ChannelConnector):
            channel_type = "test"

            def __init__(self):
                self.name = "capture"
                self.enabled = True
                self.sent = []

            async def start(self, config):
                pass

            async def stop(self):
                pass

            async def health_check(self):
                return HealthStatus(healthy=True, detail="test")

            async def send_message(self, conversation_id, text, metadata=None):
                self.sent.append(text)
                return True

        return Capture()


@patch("carpenter.agent.invocation.claude_client")
async def test_channel_sends_final_reply_once(mock_client, tool_handlers):
    tool_handlers["submit_order"] = FinalReply(ACK)
    mock_client.call.side_effect = [
        _tool_use(("tu_1", "submit_order"), text="Placing the order."),
    ]
    mock_client.extract_code_from_text.return_value = None

    channel = _CaptureChannel.make()
    conv_id = conversation.get_or_create_conversation()
    conversation.add_message(conv_id, "user", "Order a widget")
    await channel._run_and_respond("Order a widget", conv_id)

    assert channel.sent == [ACK]
    assert mock_client.call.call_count == 1
