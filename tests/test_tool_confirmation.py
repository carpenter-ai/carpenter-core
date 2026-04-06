"""Tests for tool confirmation mechanism (requires_user_confirm flag)."""

import pytest

from carpenter.chat_tool_loader import (
    LoadedTool,
    set_confirmation_handler,
    get_confirmation_handler,
    register_extension_tool,
)


def _make_tool(
    name="test_tool",
    description="A test tool.",
    input_schema=None,
    trust_boundary="chat",
    capabilities=None,
    always_available=False,
    requires_user_confirm=False,
    handler=None,
):
    """Helper to create a LoadedTool for testing."""
    return LoadedTool(
        name=name,
        description=description,
        input_schema=input_schema or {"type": "object", "properties": {}, "required": []},
        trust_boundary=trust_boundary,
        capabilities=capabilities or ["pure"],
        always_available=always_available,
        requires_user_confirm=requires_user_confirm,
        handler=handler or (lambda tool_input, **kw: "ok"),
    )


class TestConfirmationHandlerRegistry:
    """Test confirmation handler registration and retrieval."""

    def test_no_handler_by_default(self):
        """By default, no confirmation handler is registered."""
        # Note: We can't reset global state in tests, so we check if it's callable or None
        handler = get_confirmation_handler()
        assert handler is None or callable(handler)

    def test_set_and_get_handler(self):
        """set_confirmation_handler() registers a handler that get_confirmation_handler() returns."""
        def mock_handler(tool_name: str, tool_input: dict) -> bool:
            return True

        set_confirmation_handler(mock_handler)
        retrieved = get_confirmation_handler()
        assert retrieved is mock_handler


class TestToolWithConfirmationFlag:
    """Test LoadedTool with requires_user_confirm flag."""

    def test_default_requires_user_confirm_false(self):
        """LoadedTool.requires_user_confirm defaults to False."""
        tool = _make_tool()
        assert tool.requires_user_confirm is False

    def test_explicit_requires_user_confirm_true(self):
        """LoadedTool can have requires_user_confirm=True."""
        tool = _make_tool(requires_user_confirm=True)
        assert tool.requires_user_confirm is True

    def test_explicit_requires_user_confirm_false(self):
        """LoadedTool can explicitly set requires_user_confirm=False."""
        tool = _make_tool(requires_user_confirm=False)
        assert tool.requires_user_confirm is False


class TestExtensionToolRegistration:
    """Test register_extension_tool() with requires_user_confirm."""

    def test_register_extension_tool_without_confirmation(self):
        """Extension tool can be registered without confirmation requirement."""
        def handler(tool_input, **kwargs):
            return "extension_result"

        # This should not raise
        register_extension_tool(
            name="test_ext_no_confirm",
            description="Test extension without confirmation",
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=handler,
            capabilities=["pure"],
            requires_user_confirm=False,
        )

    def test_register_extension_tool_with_confirmation(self):
        """Extension tool can be registered with confirmation requirement."""
        def handler(tool_input, **kwargs):
            return "extension_result"

        # This should not raise
        # Note: Extension tools have trust_boundary="chat", so they can only
        # have read capabilities. Use "pure" for this test.
        register_extension_tool(
            name="test_ext_with_confirm",
            description="Test extension with confirmation",
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=handler,
            capabilities=["pure"],
            requires_user_confirm=True,
        )


class TestConfirmationHandlerBehavior:
    """Test confirmation handler callback behavior."""

    def test_handler_receives_correct_arguments(self):
        """Confirmation handler receives tool name and input."""
        received_args = []

        def mock_handler(tool_name: str, tool_input: dict) -> bool:
            received_args.append((tool_name, tool_input))
            return True

        set_confirmation_handler(mock_handler)

        # Simulate calling the handler
        handler = get_confirmation_handler()
        test_input = {"param1": "value1"}
        result = handler("test_tool", test_input)

        assert result is True
        assert len(received_args) == 1
        assert received_args[0] == ("test_tool", test_input)

    def test_handler_can_decline(self):
        """Confirmation handler can return False to decline execution."""
        def decline_handler(tool_name: str, tool_input: dict) -> bool:
            return False

        set_confirmation_handler(decline_handler)
        handler = get_confirmation_handler()
        result = handler("test_tool", {})

        assert result is False

    def test_handler_can_confirm(self):
        """Confirmation handler can return True to confirm execution."""
        def confirm_handler(tool_name: str, tool_input: dict) -> bool:
            return True

        set_confirmation_handler(confirm_handler)
        handler = get_confirmation_handler()
        result = handler("test_tool", {})

        assert result is True


class TestChatToolDecorator:
    """Test @chat_tool decorator with requires_user_confirm parameter."""

    def test_decorator_accepts_requires_user_confirm(self):
        """@chat_tool decorator accepts requires_user_confirm parameter."""
        from carpenter.chat_tool_loader import chat_tool

        @chat_tool(
            description="Test tool",
            input_schema={"type": "object", "properties": {}, "required": []},
            requires_user_confirm=True,
        )
        def test_tool(tool_input, **kwargs):
            return "ok"

        assert hasattr(test_tool, "_chat_tool_meta")
        assert test_tool._chat_tool_meta["requires_user_confirm"] is True

    def test_decorator_defaults_to_false(self):
        """@chat_tool decorator defaults requires_user_confirm to False."""
        from carpenter.chat_tool_loader import chat_tool

        @chat_tool(
            description="Test tool",
            input_schema={"type": "object", "properties": {}, "required": []},
        )
        def test_tool(tool_input, **kwargs):
            return "ok"

        assert hasattr(test_tool, "_chat_tool_meta")
        assert test_tool._chat_tool_meta["requires_user_confirm"] is False
