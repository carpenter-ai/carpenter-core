"""Tests for tool handler registration in the invocation module."""

from carpenter.agent.invocation import register_tool_handler, _extra_tool_handlers, _execute_chat_tool


def test_register_tool_handler_called_before_builtins():
    """register_tool_handler() handlers are dispatched before built-in tools."""
    old_handlers = _extra_tool_handlers.copy()
    try:
        calls = []

        def fake_handler(tool_input, **kwargs):
            calls.append(("fake_tool", tool_input, kwargs))
            return "fake result"

        register_tool_handler("fake_tool", fake_handler)
        result = _execute_chat_tool("fake_tool", {"key": "value"})
        assert result == "fake result"
        assert len(calls) == 1
        assert calls[0][0] == "fake_tool"
        assert calls[0][1] == {"key": "value"}
    finally:
        _extra_tool_handlers.clear()
        _extra_tool_handlers.update(old_handlers)


def test_registered_handler_receives_kwargs():
    """Registered handlers receive conversation_id and other kwargs."""
    old_handlers = _extra_tool_handlers.copy()
    try:
        received_kwargs = {}

        def capture_handler(tool_input, **kwargs):
            received_kwargs.update(kwargs)
            return "ok"

        register_tool_handler("kwarg_test", capture_handler)
        _execute_chat_tool(
            "kwarg_test", {},
            conversation_id=42,
            executor_arc_id=7,
            executor_conv_id=3,
        )
        assert received_kwargs["conversation_id"] == 42
        assert received_kwargs["executor_arc_id"] == 7
        assert received_kwargs["executor_conv_id"] == 3
    finally:
        _extra_tool_handlers.clear()
        _extra_tool_handlers.update(old_handlers)
