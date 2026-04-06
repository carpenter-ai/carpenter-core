"""Tests for chat tool registry validation layer (invariant I10)."""

import pytest

from carpenter.chat_tool_registry import (
    PLATFORM_TOOLS,
    VALID_CAPABILITIES,
    READ_CAPABILITIES,
    WRITE_CAPABILITIES,
    validate_tool_defs,
)
from carpenter.chat_tool_loader import (
    LoadedTool,
    get_tool_defs_for_api,
    get_always_available_names,
    get_total_count,
    get_loaded_tools,
)


def _make_tool(name="test_tool", description="A test tool.",
               input_schema=None, trust_boundary="chat",
               capabilities=None, always_available=False,
               handler=None):
    """Helper to create a LoadedTool for testing."""
    return LoadedTool(
        name=name,
        description=description,
        input_schema=input_schema or {"type": "object", "properties": {}, "required": []},
        trust_boundary=trust_boundary,
        capabilities=capabilities or ["pure"],
        always_available=always_available,
        handler=handler or (lambda tool_input, **kw: "ok"),
    )


class TestTrustBoundaryInvariants:
    """Invariant I10: all chat tools have valid, enforced trust boundaries."""

    def test_loaded_tools_have_valid_boundary(self):
        """Every loaded tool must declare boundary as 'chat' or 'platform'."""
        tools = get_loaded_tools()
        for name, tool in tools.items():
            assert tool.trust_boundary in {"chat", "platform"}, (
                f"Tool {name!r} has invalid trust_boundary "
                f"{tool.trust_boundary!r}"
            )

    def test_no_duplicate_names(self):
        """No two loaded tools should share a name."""
        tools = get_loaded_tools()
        # If we got this far, names are unique (dict keys are unique)
        assert len(tools) > 0

    def test_all_tools_have_description_and_schema(self):
        """Every loaded tool must have a non-empty description and input_schema."""
        tools = get_loaded_tools()
        for name, tool in tools.items():
            assert tool.description, f"Tool {name!r} has empty description"
            assert tool.input_schema, f"Tool {name!r} has empty input_schema"

    def test_validate_returns_no_errors(self):
        """validate_tool_defs() should return empty list on loaded tools."""
        tools = get_loaded_tools()
        errors = validate_tool_defs(list(tools.values()))
        assert errors == [], f"Validation errors: {errors}"


class TestValidationCatchesBadDefinitions:
    """validate_tool_defs() must catch malformed definitions."""

    def test_validate_catches_fake_platform(self):
        """A tool claiming platform boundary but not in allowlist -> error."""
        fake = _make_tool(name="fake_admin", trust_boundary="platform")
        errors = validate_tool_defs([fake])
        assert any("fake_admin" in e for e in errors)

    def test_validate_catches_invalid_boundary(self):
        """A tool with an unrecognised boundary -> error."""
        bad = _make_tool(name="bad_boundary", trust_boundary="action")
        errors = validate_tool_defs([bad])
        assert any("bad_boundary" in e for e in errors)

    def test_validate_catches_unknown_capability(self):
        """A tool with an unknown capability string -> error."""
        bad = _make_tool(name="bad_cap", capabilities=["teleportation"])
        errors = validate_tool_defs([bad])
        assert any("bad_cap" in e for e in errors)
        assert any("teleportation" in e for e in errors)

    def test_validate_catches_pure_mixed_with_others(self):
        """'pure' mixed with other capabilities -> error."""
        bad = _make_tool(name="mixed", capabilities=["pure", "database_read"])
        errors = validate_tool_defs([bad])
        assert any("mixed" in e for e in errors)
        assert any("pure" in e for e in errors)

    def test_validate_catches_write_on_chat_boundary(self):
        """A chat-boundary tool with write capabilities -> error."""
        bad = _make_tool(name="write_chat", trust_boundary="chat",
                         capabilities=["filesystem_write"])
        errors = validate_tool_defs([bad])
        assert any("write_chat" in e for e in errors)

    def test_validate_catches_duplicate_names(self):
        """Duplicate tool names -> error."""
        t1 = _make_tool(name="dupe")
        t2 = _make_tool(name="dupe")
        errors = validate_tool_defs([t1, t2])
        assert any("dupe" in e for e in errors)


class TestRegistryHelpers:
    """Test get_always_available_names, get_tool_defs_for_api, get_total_count."""

    def test_always_available_includes_core(self):
        """Always-available tools include read_file, kb_search."""
        always = get_always_available_names()
        assert "read_file" in always
        assert "kb_search" in always
        # Non-always-available should not be in set
        assert "reverse_string" not in always
        assert "double_string" not in always

    def test_api_format(self):
        """get_tool_defs_for_api() returns proper dicts with required keys."""
        defs = get_tool_defs_for_api()
        assert len(defs) == get_total_count()
        for d in defs:
            assert isinstance(d, dict)
            assert "name" in d
            assert "description" in d
            assert "input_schema" in d
            # Should NOT expose internal fields to the API
            assert "trust_boundary" not in d
            assert "capabilities" not in d
            assert "always_available" not in d


class TestCapabilityVocabulary:
    """Test that capability sets are consistent."""

    def test_read_and_write_disjoint(self):
        """Read and write capability sets should not overlap."""
        overlap = READ_CAPABILITIES & WRITE_CAPABILITIES
        assert not overlap, f"Capabilities in both read and write: {overlap}"

    def test_valid_is_union(self):
        """VALID_CAPABILITIES is exactly READ | WRITE."""
        assert VALID_CAPABILITIES == READ_CAPABILITIES | WRITE_CAPABILITIES

    def test_platform_tools_frozenset(self):
        """PLATFORM_TOOLS is a frozenset (immutable)."""
        assert isinstance(PLATFORM_TOOLS, frozenset)
        assert "submit_code" in PLATFORM_TOOLS
        assert "escalate" in PLATFORM_TOOLS
        assert "escalate_current_arc" in PLATFORM_TOOLS
