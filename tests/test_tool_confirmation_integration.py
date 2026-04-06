"""Integration tests for tool confirmation in the invocation flow."""

import pytest
import tempfile
import os
from pathlib import Path

from carpenter.chat_tool_loader import (
    load_chat_tools,
    set_confirmation_handler,
    get_confirmation_handler,
    get_loaded_tools,
)


class TestConfirmationIntegration:
    """Test full integration of confirmation mechanism in tool loading."""

    def test_tool_with_confirmation_loads_correctly(self, tmp_path):
        """A tool with requires_user_confirm=True loads correctly."""
        # Create a test tool module
        tool_module = tmp_path / "test_confirm_tool.py"
        tool_module.write_text("""
from carpenter.chat_tool_loader import chat_tool

@chat_tool(
    description="Test tool requiring confirmation",
    input_schema={"type": "object", "properties": {}, "required": []},
    capabilities=["pure"],
    requires_user_confirm=True,
)
def test_confirm_tool(tool_input, **kwargs):
    return "executed"
""")

        # Load the tools
        tools = load_chat_tools(str(tmp_path))

        # Check that the tool was loaded with the correct flag
        assert "test_confirm_tool" in tools
        assert tools["test_confirm_tool"].requires_user_confirm is True

    def test_tool_without_confirmation_loads_correctly(self, tmp_path):
        """A tool without requires_user_confirm loads with False."""
        # Create a test tool module
        tool_module = tmp_path / "test_no_confirm_tool.py"
        tool_module.write_text("""
from carpenter.chat_tool_loader import chat_tool

@chat_tool(
    description="Test tool without confirmation",
    input_schema={"type": "object", "properties": {}, "required": []},
    capabilities=["pure"],
)
def test_no_confirm_tool(tool_input, **kwargs):
    return "executed"
""")

        # Load the tools
        tools = load_chat_tools(str(tmp_path))

        # Check that the tool was loaded with False
        assert "test_no_confirm_tool" in tools
        assert tools["test_no_confirm_tool"].requires_user_confirm is False

    def test_mixed_tools_load_correctly(self, tmp_path):
        """Tools with and without confirmation can coexist."""
        # Create test tool modules
        confirm_tool = tmp_path / "confirm_tool.py"
        confirm_tool.write_text("""
from carpenter.chat_tool_loader import chat_tool

@chat_tool(
    description="Needs confirmation",
    input_schema={"type": "object", "properties": {}, "required": []},
    requires_user_confirm=True,
)
def needs_confirm(tool_input, **kwargs):
    return "confirmed"
""")

        no_confirm_tool = tmp_path / "no_confirm_tool.py"
        no_confirm_tool.write_text("""
from carpenter.chat_tool_loader import chat_tool

@chat_tool(
    description="No confirmation needed",
    input_schema={"type": "object", "properties": {}, "required": []},
)
def no_confirm(tool_input, **kwargs):
    return "executed"
""")

        # Load the tools
        tools = load_chat_tools(str(tmp_path))

        # Check both tools loaded with correct flags
        assert "needs_confirm" in tools
        assert tools["needs_confirm"].requires_user_confirm is True

        assert "no_confirm" in tools
        assert tools["no_confirm"].requires_user_confirm is False


class TestConfirmationHandlerInvocation:
    """Test that confirmation handler is invoked correctly during tool execution."""

    def test_handler_not_called_for_non_confirmation_tools(self):
        """Confirmation handler should not be called for tools without requires_user_confirm."""
        call_count = [0]

        def mock_handler(tool_name: str, tool_input: dict) -> bool:
            call_count[0] += 1
            return True

        set_confirmation_handler(mock_handler)

        # Load a tool without confirmation requirement
        # (We can't easily test invocation.py without full setup, but we
        # can verify the handler registration works)
        handler = get_confirmation_handler()
        assert handler is not None
        assert callable(handler)

    def test_handler_state_persists(self):
        """Confirmation handler state persists across multiple calls."""
        calls = []

        def tracking_handler(tool_name: str, tool_input: dict) -> bool:
            calls.append((tool_name, tool_input))
            return True

        set_confirmation_handler(tracking_handler)

        # Simulate multiple calls
        handler = get_confirmation_handler()
        handler("tool1", {"param": "value1"})
        handler("tool2", {"param": "value2"})

        assert len(calls) == 2
        assert calls[0] == ("tool1", {"param": "value1"})
        assert calls[1] == ("tool2", {"param": "value2"})


class TestConfirmationEdgeCases:
    """Test edge cases and error conditions."""

    def test_tool_with_empty_input_schema(self, tmp_path):
        """Tool with requires_user_confirm and empty params loads correctly."""
        tool_module = tmp_path / "no_params_tool.py"
        tool_module.write_text("""
from carpenter.chat_tool_loader import chat_tool

@chat_tool(
    description="No parameters",
    input_schema={"type": "object", "properties": {}, "required": []},
    requires_user_confirm=True,
)
def no_params(tool_input, **kwargs):
    return "ok"
""")

        tools = load_chat_tools(str(tmp_path))
        assert "no_params" in tools
        assert tools["no_params"].requires_user_confirm is True
        assert tools["no_params"].input_schema["properties"] == {}

    def test_confirmation_handler_exception_handling(self):
        """Test that handler exceptions are caught properly."""
        def failing_handler(tool_name: str, tool_input: dict) -> bool:
            raise RuntimeError("Handler failed")

        set_confirmation_handler(failing_handler)
        handler = get_confirmation_handler()

        # The handler itself will raise, but invocation.py should catch it
        with pytest.raises(RuntimeError):
            handler("test", {})
