"""Tests for tool argument type checker (verify/tool_arg_types.py)."""

import pytest

from carpenter.verify.tool_arg_types import check_tool_arg_types


class TestCorrectTypes:
    """Correctly typed tool arguments pass."""

    def test_label_for_name_passes(self):
        code = """
from carpenter_tools.act import arc
from carpenter_tools.declarations import Label, UnstructuredText
arc.create(name=Label("test"), goal=UnstructuredText("do-thing"))
"""
        result = check_tool_arg_types(code)
        assert result.passed
        assert result.violations == []

    def test_url_for_web_get_passes(self):
        code = """
from carpenter_tools.act import web
from carpenter_tools.declarations import URL
web.get(url=URL("https://example.com"))
"""
        result = check_tool_arg_types(code)
        assert result.passed

    def test_workspace_path_for_files_passes(self):
        code = """
from carpenter_tools.act import files
from carpenter_tools.declarations import WorkspacePath, UnstructuredText
files.write(path=WorkspacePath("file.txt"), content=UnstructuredText("hello"))
"""
        result = check_tool_arg_types(code)
        assert result.passed

    def test_unstructured_text_for_message_passes(self):
        code = """
from carpenter_tools.act import messaging
from carpenter_tools.declarations import UnstructuredText
messaging.send(message=UnstructuredText("hello"))
"""
        result = check_tool_arg_types(code)
        assert result.passed


class TestWrongTypes:
    """Wrongly typed tool arguments are rejected."""

    def test_url_for_name_rejected(self):
        code = """
from carpenter_tools.act import arc
from carpenter_tools.declarations import URL
arc.create(name=URL("https://example.com"))
"""
        result = check_tool_arg_types(code)
        assert not result.passed
        assert len(result.violations) == 1
        assert "expected Label" in result.violations[0]
        assert "got URL" in result.violations[0]

    def test_label_for_url_rejected(self):
        code = """
from carpenter_tools.act import web
from carpenter_tools.declarations import Label
web.get(url=Label("not-a-url"))
"""
        result = check_tool_arg_types(code)
        assert not result.passed
        assert "expected URL" in result.violations[0]
        assert "got Label" in result.violations[0]

    def test_url_for_workspace_path_rejected(self):
        code = """
from carpenter_tools.act import files
from carpenter_tools.declarations import URL, UnstructuredText
files.write(path=URL("https://example.com"), content=UnstructuredText("x"))
"""
        result = check_tool_arg_types(code)
        assert not result.passed
        assert "expected WorkspacePath" in result.violations[0]


class TestVariableTracking:
    """Variable tracking through assignments."""

    def test_variable_with_correct_type_passes(self):
        code = """
from carpenter_tools.act import arc
from carpenter_tools.declarations import Label
n = Label("test")
arc.create(name=n)
"""
        result = check_tool_arg_types(code)
        assert result.passed

    def test_variable_with_wrong_type_rejected(self):
        code = """
from carpenter_tools.act import arc
from carpenter_tools.declarations import URL
n = URL("https://example.com")
arc.create(name=n)
"""
        result = check_tool_arg_types(code)
        assert not result.passed
        assert "expected Label" in result.violations[0]
        assert "got URL" in result.violations[0]

    def test_unknown_variable_produces_warning(self):
        code = """
from carpenter_tools.act import arc
n = some_function()
arc.create(name=n)
"""
        result = check_tool_arg_types(code)
        assert result.passed  # warnings don't fail
        assert len(result.warnings) == 1
        assert "cannot determine SecurityType" in result.warnings[0]


class TestPositionalArgs:
    """Positional argument checking."""

    def test_positional_correct_type_passes(self):
        code = """
from carpenter_tools.act import arc
from carpenter_tools.declarations import Label
arc.create(Label("test"))
"""
        result = check_tool_arg_types(code)
        assert result.passed

    def test_positional_wrong_type_rejected(self):
        code = """
from carpenter_tools.act import arc
from carpenter_tools.declarations import URL
arc.create(URL("https://example.com"))
"""
        result = check_tool_arg_types(code)
        assert not result.passed
        assert "arg[0]" in result.violations[0]


class TestNonToolCallsIgnored:
    """Non-tool calls are not checked."""

    def test_non_tool_module_ignored(self):
        code = """
from carpenter_tools.declarations import Label
x = Label("hello")
some_other.create(name=x)
"""
        result = check_tool_arg_types(code)
        assert result.passed
        assert result.violations == []
        assert result.warnings == []

    def test_plain_function_call_ignored(self):
        code = """
from carpenter_tools.declarations import URL
x = URL("https://example.com")
print(x)
"""
        result = check_tool_arg_types(code)
        assert result.passed

    def test_syntax_error_passes(self):
        result = check_tool_arg_types("def ")
        assert result.passed


class TestMultipleViolations:
    """Multiple violations in one call."""

    def test_two_wrong_types_in_one_call(self):
        code = """
from carpenter_tools.act import arc
from carpenter_tools.declarations import URL
arc.create(name=URL("a"), goal=URL("b"))
"""
        result = check_tool_arg_types(code)
        assert not result.passed
        assert len(result.violations) == 2
