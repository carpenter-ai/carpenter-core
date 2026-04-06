"""Tests for tool_meta type map builder functions."""

import pytest

from carpenter_tools.tool_meta import build_tool_type_map, build_tool_return_type_map


class TestBuildToolTypeMap:
    """build_tool_type_map() returns entries for annotated tools."""

    def test_returns_nonempty(self):
        m = build_tool_type_map()
        assert len(m) > 0

    def test_arc_create_name_is_label(self):
        m = build_tool_type_map()
        assert m[("arc", "create", "name")] == "Label"

    def test_arc_create_positional_name(self):
        m = build_tool_type_map()
        assert m[("arc", "create", 0)] == "Label"

    def test_arc_create_goal_is_unstructured(self):
        m = build_tool_type_map()
        assert m[("arc", "create", "goal")] == "UnstructuredText"

    def test_web_get_url_is_url(self):
        m = build_tool_type_map()
        assert m[("web", "get", "url")] == "URL"

    def test_web_get_positional_url(self):
        m = build_tool_type_map()
        assert m[("web", "get", 0)] == "URL"

    def test_files_write_path_is_workspace(self):
        m = build_tool_type_map()
        assert m[("files", "write", "path")] == "WorkspacePath"

    def test_files_write_content_is_unstructured(self):
        m = build_tool_type_map()
        assert m[("files", "write", "content")] == "UnstructuredText"

    def test_files_read_path_is_workspace(self):
        m = build_tool_type_map()
        assert m[("files", "read", "path")] == "WorkspacePath"

    def test_messaging_send_message(self):
        m = build_tool_type_map()
        assert m[("messaging", "send", "message")] == "UnstructuredText"

    def test_state_set_key_is_label(self):
        m = build_tool_type_map()
        assert m[("state", "set", "key")] == "Label"

    def test_lm_call_prompt_is_unstructured(self):
        m = build_tool_type_map()
        assert m[("lm", "call", "prompt")] == "UnstructuredText"

    def test_review_submit_verdict_decision(self):
        m = build_tool_type_map()
        assert m[("review", "submit_verdict", "decision")] == "Label"

    def test_git_create_pr_repo_owner(self):
        m = build_tool_type_map()
        assert m[("git", "create_pr", "repo_owner")] == "Label"


class TestBuildToolReturnTypeMap:
    """build_tool_return_type_map() returns entries for tools with return_types."""

    def test_returns_nonempty(self):
        m = build_tool_return_type_map()
        assert len(m) > 0

    def test_web_get_returns_text_unstructured(self):
        m = build_tool_return_type_map()
        assert m[("web", "get")] == {"text": "UnstructuredText"}

    def test_web_post_returns_text_unstructured(self):
        m = build_tool_return_type_map()
        assert m[("web", "post")] == {"text": "UnstructuredText"}

    def test_lm_call_returns_content_unstructured(self):
        m = build_tool_return_type_map()
        assert m[("lm", "call")] == {"content": "UnstructuredText"}

    def test_files_read_returns_unstructured(self):
        m = build_tool_return_type_map()
        assert m[("files", "read")] == "UnstructuredText"

    def test_no_return_types_absent(self):
        m = build_tool_return_type_map()
        assert ("arc", "create") not in m
