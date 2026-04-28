"""Tests for chat tool loader — decorator, module importing, hot-reload, validation."""

import os
import time
from pathlib import Path

import pytest

from carpenter.chat_tool_loader import (
    LoadedTool,
    chat_tool,
    install_chat_tool_defaults,
    load_chat_tools,
    get_handler,
    get_tool_defs_for_api,
    get_always_available_names,
    get_total_count,
    get_loaded_tools,
    _check_and_reload,
    _loaded_tools,
    READ_CAPABILITIES,
    WRITE_CAPABILITIES,
)
from carpenter.chat_tool_registry import PLATFORM_TOOLS, validate_tool_defs


class TestDecorator:
    """Test the @chat_tool decorator."""

    def test_basic_decoration(self):
        """Decorator attaches metadata to the function."""
        @chat_tool(
            description="Test tool.",
            input_schema={"type": "object", "properties": {}, "required": []},
        )
        def my_tool(tool_input, **kwargs):
            return "result"

        assert hasattr(my_tool, "_chat_tool_meta")
        meta = my_tool._chat_tool_meta
        assert meta["name"] == "my_tool"
        assert meta["description"] == "Test tool."
        assert meta["capabilities"] == ["pure"]
        assert meta["trust_boundary"] == "chat"
        assert meta["always_available"] is False

    def test_custom_capabilities(self):
        """Decorator accepts custom capabilities."""
        @chat_tool(
            description="DB tool.",
            input_schema={"type": "object", "properties": {}, "required": []},
            capabilities=["database_read"],
            always_available=True,
        )
        def db_tool(tool_input, **kwargs):
            return "ok"

        meta = db_tool._chat_tool_meta
        assert meta["capabilities"] == ["database_read"]
        assert meta["always_available"] is True

    def test_unknown_capability_raises(self):
        """Decorator rejects unknown capability strings at decoration time."""
        with pytest.raises(ValueError, match="Unknown capability"):
            @chat_tool(
                description="Bad.",
                input_schema={"type": "object", "properties": {}, "required": []},
                capabilities=["teleportation"],
            )
            def bad_tool(tool_input, **kwargs):
                return "nope"

    def test_pure_mixed_raises(self):
        """Decorator rejects 'pure' mixed with other capabilities."""
        with pytest.raises(ValueError, match="pure.*cannot be mixed"):
            @chat_tool(
                description="Bad.",
                input_schema={"type": "object", "properties": {}, "required": []},
                capabilities=["pure", "database_read"],
            )
            def bad_tool(tool_input, **kwargs):
                return "nope"

    def test_invalid_boundary_raises(self):
        """Decorator rejects invalid trust boundary."""
        with pytest.raises(ValueError, match="Invalid trust_boundary"):
            @chat_tool(
                description="Bad.",
                input_schema={"type": "object", "properties": {}, "required": []},
                trust_boundary="action",
            )
            def bad_tool(tool_input, **kwargs):
                return "nope"

    def test_function_still_callable(self):
        """Decorated function is still callable normally."""
        @chat_tool(
            description="Simple.",
            input_schema={"type": "object", "properties": {}, "required": []},
        )
        def simple(tool_input, **kwargs):
            return "hello"

        assert simple({"x": 1}) == "hello"


class TestInstallDefaults:
    """Test install_chat_tool_defaults."""

    def test_install_creates_directory(self, tmp_path):
        """Installs defaults when target dir doesn't exist."""
        target = str(tmp_path / "fresh_chat_tools")
        result = install_chat_tool_defaults(target)
        assert result["status"] == "installed"
        assert result["copied"] > 0
        assert os.path.isdir(target)

    def test_install_skips_existing(self, tmp_path):
        """Doesn't overwrite existing directory."""
        target = str(tmp_path / "fresh_chat_tools")
        os.makedirs(target)
        result = install_chat_tool_defaults(target)
        assert result["status"] == "exists"


class TestLoadChatTools:
    """Test loading chat tools from directory."""

    def test_load_from_seed(self, tmp_path):
        """Loads tools from installed seed directory."""
        target = str(tmp_path / "ct")
        install_chat_tool_defaults(target)
        tools = load_chat_tools(target)
        assert len(tools) > 0
        # Should have the basic tools
        assert "read_file" in tools
        assert "reverse_string" in tools
        assert "get_state" in tools

    def test_tool_handler_callable(self, tmp_path):
        """Loaded tool handlers are callable."""
        target = str(tmp_path / "ct")
        install_chat_tool_defaults(target)
        tools = load_chat_tools(target)
        handler = get_handler("reverse_string")
        assert handler is not None
        result = handler({"text": "hello"})
        assert result == "olleh"

    def test_unknown_tool_returns_none(self, tmp_path):
        """get_handler returns None for unknown tools."""
        target = str(tmp_path / "ct")
        install_chat_tool_defaults(target)
        load_chat_tools(target)
        assert get_handler("nonexistent_tool") is None

    def test_skips_underscore_files(self, tmp_path):
        """Files starting with _ are skipped."""
        target = str(tmp_path / "ct")
        install_chat_tool_defaults(target)
        # Add an __init__.py
        (Path(target) / "__init__.py").write_text("# init\n")
        (Path(target) / "_private.py").write_text("x = 1\n")
        tools = load_chat_tools(target)
        assert len(tools) > 0  # Should still load normal files

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        """Loading from nonexistent dir returns empty dict."""
        tools = load_chat_tools(str(tmp_path / "does_not_exist"))
        assert tools == {}


class TestValidation:
    """Test validation enforcement."""

    def test_chat_tool_with_write_cap_rejected(self, tmp_path):
        """A chat-boundary module with write capabilities is rejected."""
        target = str(tmp_path / "ct")
        os.makedirs(target)
        (Path(target) / "bad.py").write_text(
            "from carpenter.chat_tool_loader import chat_tool\n\n"
            "@chat_tool(\n"
            '    description="Bad write tool.",\n'
            '    input_schema={"type": "object", "properties": {}, "required": []},\n'
            '    capabilities=["filesystem_write"],\n'
            ")\n"
            "def bad_write(tool_input, **kwargs):\n"
            '    return "bad"\n'
        )
        # This should raise ValueError at decoration time because
        # the decorator itself doesn't check chat vs write,
        # but validation does
        # Actually the decorator allows any valid capability,
        # validation catches boundary violations
        # The decorator won't raise since filesystem_write IS a valid capability
        # But load_chat_tools validates and should raise RuntimeError
        with pytest.raises(RuntimeError, match="validation failed"):
            load_chat_tools(target)

    def test_platform_boundary_from_config_rejected(self, tmp_path):
        """User config cannot create platform-boundary tools."""
        target = str(tmp_path / "ct")
        os.makedirs(target)
        (Path(target) / "bad.py").write_text(
            "from carpenter.chat_tool_loader import chat_tool\n\n"
            "@chat_tool(\n"
            '    description="Fake platform tool.",\n'
            '    input_schema={"type": "object", "properties": {}, "required": []},\n'
            '    trust_boundary="platform",\n'
            '    capabilities=["filesystem_write"],\n'
            ")\n"
            "def fake_admin(tool_input, **kwargs):\n"
            '    return "pwned"\n'
        )
        with pytest.raises(RuntimeError, match="validation failed"):
            load_chat_tools(target)


class TestHotReload:
    """Test mtime-based hot-reload."""

    def test_reload_detects_new_file(self, tmp_path):
        """Adding a new module triggers reload."""
        import carpenter.chat_tool_loader as loader

        target = str(tmp_path / "ct")
        install_chat_tool_defaults(target)
        load_chat_tools(target)
        original_count = get_total_count()

        # Add a new tool module
        (Path(target) / "custom.py").write_text(
            "from carpenter.chat_tool_loader import chat_tool\n\n"
            "@chat_tool(\n"
            '    description="Custom tool.",\n'
            '    input_schema={"type": "object", "properties": {}, "required": []},\n'
            ")\n"
            "def my_custom_tool(tool_input, **kwargs):\n"
            '    return "custom"\n'
        )

        # Trigger reload check
        loader._chat_tools_dir = target
        _check_and_reload()

        assert get_total_count() == original_count + 1
        assert get_handler("my_custom_tool") is not None
        assert get_handler("my_custom_tool")({"x": 1}) == "custom"

    def test_reload_keeps_previous_on_error(self, tmp_path):
        """If reload validation fails, keep previous valid set."""
        import carpenter.chat_tool_loader as loader

        target = str(tmp_path / "ct")
        install_chat_tool_defaults(target)
        load_chat_tools(target)
        original_count = get_total_count()

        # Add an invalid module (syntax error)
        (Path(target) / "broken.py").write_text("def this is bad syntax")

        loader._chat_tools_dir = target
        _check_and_reload()

        # Should keep previous tools
        assert get_total_count() == original_count


class TestLoadedToolDataclass:
    """Test LoadedTool properties."""

    def test_is_read_only_pure(self):
        """A pure tool is read-only."""
        tool = LoadedTool(
            name="test", description="Test", input_schema={},
            trust_boundary="chat", capabilities=["pure"],
            always_available=False, handler=lambda x, **k: "ok",
        )
        assert tool.is_read_only is True

    def test_is_read_only_with_read_caps(self):
        """A tool with only read capabilities is read-only."""
        tool = LoadedTool(
            name="test", description="Test", input_schema={},
            trust_boundary="chat", capabilities=["database_read", "filesystem_read"],
            always_available=False, handler=lambda x, **k: "ok",
        )
        assert tool.is_read_only is True

    def test_not_read_only_with_write_cap(self):
        """A tool with write capabilities is not read-only."""
        tool = LoadedTool(
            name="test", description="Test", input_schema={},
            trust_boundary="platform", capabilities=["database_write"],
            always_available=False, handler=lambda x, **k: "ok",
        )
        assert tool.is_read_only is False
