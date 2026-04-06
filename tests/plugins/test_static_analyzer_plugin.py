"""Tests for the plugin prompt safety check in static analyzer."""

import pytest

from carpenter.review.static_analyzer import check_plugin_prompt_safety


class TestCheckPluginPromptSafety:
    def test_literal_prompt_is_safe(self):
        code = '''
from carpenter_tools.act import plugin
result = plugin.submit_task("my-plugin", prompt="Install nginx")
'''
        result = check_plugin_prompt_safety(code)
        assert result["safe"] is True
        assert result["warnings"] == []

    def test_literal_positional_prompt_is_safe(self):
        code = '''
from carpenter_tools.act import plugin
result = plugin.submit_task("my-plugin", "Install nginx")
'''
        result = check_plugin_prompt_safety(code)
        assert result["safe"] is True

    def test_variable_prompt_is_unsafe(self):
        code = '''
prompt = get_user_input()
result = plugin.submit_task("my-plugin", prompt=prompt)
'''
        result = check_plugin_prompt_safety(code)
        assert result["safe"] is False
        assert len(result["warnings"]) == 1
        assert "non-literal" in result["warnings"][0]

    def test_fstring_prompt_is_unsafe(self):
        code = '''
name = "nginx"
result = plugin.submit_task("my-plugin", prompt=f"Install {name}")
'''
        result = check_plugin_prompt_safety(code)
        assert result["safe"] is False

    def test_concatenated_prompt_is_unsafe(self):
        code = '''
result = plugin.submit_task("my-plugin", prompt="Install " + package_name)
'''
        result = check_plugin_prompt_safety(code)
        assert result["safe"] is False

    def test_function_call_prompt_is_unsafe(self):
        code = '''
result = plugin.submit_task("my-plugin", prompt=build_prompt())
'''
        result = check_plugin_prompt_safety(code)
        assert result["safe"] is False

    def test_no_plugin_calls_is_safe(self):
        code = '''
x = 1 + 2
print("hello")
'''
        result = check_plugin_prompt_safety(code)
        assert result["safe"] is True

    def test_submit_task_without_module(self):
        code = '''
result = submit_task("my-plugin", prompt="Install nginx")
'''
        result = check_plugin_prompt_safety(code)
        assert result["safe"] is True

    def test_submit_task_variable_without_module(self):
        code = '''
result = submit_task("my-plugin", prompt=dynamic_prompt)
'''
        result = check_plugin_prompt_safety(code)
        assert result["safe"] is False

    def test_syntax_error_is_safe(self):
        code = "def foo(:"
        result = check_plugin_prompt_safety(code)
        assert result["safe"] is True

    def test_multiple_calls_mixed(self):
        code = '''
plugin.submit_task("a", prompt="literal is fine")
plugin.submit_task("b", prompt=variable_prompt)
'''
        result = check_plugin_prompt_safety(code)
        assert result["safe"] is False
        assert len(result["warnings"]) == 1

    def test_other_function_calls_ignored(self):
        code = '''
some_other_function(prompt=variable)
plugin.other_method(prompt=variable)
'''
        result = check_plugin_prompt_safety(code)
        assert result["safe"] is True
