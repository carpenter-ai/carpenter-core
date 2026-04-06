"""Tests for Phase 4: dead code removal and skills system deprecation."""

import os

import pytest

from carpenter.agent.invocation import (
    _build_chat_system_prompt,
    _execute_chat_tool,
)


class TestSkillsNotInSystemPrompt:
    """Skills are discovered via KB, not injected into the system prompt."""

    @pytest.mark.parametrize("kwargs,desc", [
        ({}, "default budget"),
        ({"context_budget": 4000}, "compact/small context budget"),
    ])
    def test_no_available_skills_section(self, kwargs, desc):
        """System prompt should NOT contain an 'Available Skills' table ({desc})."""
        prompt = _build_chat_system_prompt(**kwargs)
        assert "Available Skills" not in prompt


class TestRemovedToolsReturnUnknown:
    """Skill and write chat tool handlers have been removed."""

    @pytest.mark.parametrize("tool_name,args", [
        ("load_skill", {"name": "debug"}),
        ("submit_skill", {"name": "test", "content": "# Test"}),
        ("delete_skill", {"name": "test"}),
        ("list_skills", {}),
    ])
    def test_skill_tool_removed(self, tool_name, args):
        """Removed skill tools return an error/unknown response."""
        result = _execute_chat_tool(tool_name, args)
        assert "unknown" in result.lower() or "error" in result.lower() or "not" in result.lower()

    @pytest.mark.parametrize("tool_name", [
        "rename_conversation",
        "archive_conversation",
        "grant_arc_read_access",
        "request_restart",
        "change_config",
        "request_credential",
        "verify_credential",
        "import_credential_file",
        "subscribe_webhook",
        "list_webhooks",
        "delete_webhook",
        "create_schedule",
        "cancel_schedule",
    ])
    def test_write_chat_tool_removed(self, tool_name):
        """Removed write chat tools return 'Unknown tool'."""
        result = _execute_chat_tool(tool_name, {})
        assert "unknown tool" in result.lower()


class TestDeadCodeArtifactsRemoved:
    """Verify that removed code artifacts and config files are truly gone."""

    @pytest.mark.parametrize("attr_name", [
        "_scan_read_tools",
        "_PROMPT_SECTIONS",
        "_PROMPT_SECTION_ORDER",
        "CHAT_TOOL_DEFINITIONS",
    ])
    def test_module_attribute_removed(self, attr_name):
        """Dead module-level attributes no longer exist in invocation."""
        from carpenter.agent import invocation
        assert not hasattr(invocation, attr_name)

    @pytest.mark.parametrize("filename", [
        "11-webhooks.yaml",
        "12-skills.yaml",
    ])
    def test_config_seed_yaml_removed(self, filename):
        """Removed tool definition YAML files no longer exist."""
        tool_defaults = os.path.join(os.path.dirname(__file__), "..", "..", "config_seed", "tools")
        assert not os.path.exists(os.path.join(tool_defaults, filename))
