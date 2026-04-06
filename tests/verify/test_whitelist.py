"""Tests for AST whitelist checker (verify/whitelist.py)."""

import pytest

from carpenter.verify.whitelist import check_whitelist


class TestAllowedConstructs:
    """Each allowed construct passes the whitelist."""

    def test_simple_assignment(self):
        result = check_whitelist("x = 1")
        assert result.passed

    def test_if_else(self):
        result = check_whitelist("if True:\n    x = 1\nelse:\n    x = 2")
        assert result.passed

    def test_for_loop(self):
        result = check_whitelist("for i in range(3):\n    x = i")
        assert result.passed

    def test_try_except(self):
        result = check_whitelist("try:\n    x = 1\nexcept Exception as e:\n    x = 0")
        assert result.passed

    def test_comparison(self):
        result = check_whitelist("x = 1 < 2")
        assert result.passed

    def test_boolean_operators(self):
        result = check_whitelist("x = True and False or not True")
        assert result.passed

    def test_function_call(self):
        result = check_whitelist("x = len([1, 2, 3])")
        assert result.passed

    def test_list_literal(self):
        result = check_whitelist("x = [1, 2, 3]")
        assert result.passed

    def test_dict_literal(self):
        result = check_whitelist('x = {"a": 1}')
        assert result.passed

    def test_tuple_literal(self):
        result = check_whitelist("x = (1, 2)")
        assert result.passed

    def test_set_literal(self):
        result = check_whitelist("x = {1, 2, 3}")
        assert result.passed

    def test_list_comprehension(self):
        result = check_whitelist("x = [i for i in range(3)]")
        assert result.passed

    def test_subscript(self):
        result = check_whitelist('x = {"a": 1}["a"]')
        assert result.passed

    def test_attribute(self):
        result = check_whitelist("x = obj.attr")
        assert result.passed

    def test_fstring(self):
        result = check_whitelist('x = 1\ny = f"value is {x}"')
        assert result.passed

    def test_carpenter_tools_import(self):
        result = check_whitelist("from carpenter_tools.act import arc")
        assert result.passed

    def test_carpenter_tools_policy_import(self):
        result = check_whitelist("from carpenter_tools.policy import EmailPolicy")
        assert result.passed

    def test_pass_statement(self):
        result = check_whitelist("if True:\n    pass")
        assert result.passed

    def test_safe_builtins(self):
        result = check_whitelist("x = len([1])\ny = str(42)\nz = int('5')")
        assert result.passed

    def test_safe_stdlib_datetime(self):
        result = check_whitelist("from datetime import datetime, timedelta")
        assert result.passed

    def test_safe_stdlib_json(self):
        result = check_whitelist("from json import dumps, loads")
        assert result.passed

    def test_safe_stdlib_math(self):
        result = check_whitelist("from math import floor, ceil")
        assert result.passed

    def test_safe_stdlib_re(self):
        result = check_whitelist("from re import compile, match")
        assert result.passed

    def test_safe_stdlib_time(self):
        result = check_whitelist("from time import sleep, time")
        assert result.passed

    def test_carpenter_tools_scheduling_import(self):
        result = check_whitelist("from carpenter_tools.act.scheduling import add_cron")
        assert result.passed

    def test_annotated_assignment(self):
        result = check_whitelist("x: int = 42")
        assert result.passed

    def test_augmented_assignment(self):
        result = check_whitelist("x = 0\nx += 1")
        assert result.passed

    def test_empty_code(self):
        result = check_whitelist("")
        assert result.passed


class TestRejectedConstructs:
    """Each rejected construct fails the whitelist."""

    def test_while_loop(self):
        result = check_whitelist("while True:\n    pass")
        assert not result.passed
        assert any("while" in v.lower() for v in result.violations)

    def test_function_def(self):
        result = check_whitelist("def foo():\n    pass")
        assert not result.passed
        assert any("function" in v.lower() for v in result.violations)

    def test_async_function_def(self):
        result = check_whitelist("async def foo():\n    pass")
        assert not result.passed

    def test_lambda(self):
        result = check_whitelist("x = lambda: 1")
        assert not result.passed
        assert any("lambda" in v.lower() for v in result.violations)

    def test_bare_import(self):
        result = check_whitelist("import os")
        assert not result.passed
        assert any("import" in v.lower() for v in result.violations)

    def test_class_def(self):
        result = check_whitelist("class Foo:\n    pass")
        assert not result.passed

    def test_yield(self):
        result = check_whitelist("def gen():\n    yield 1")
        assert not result.passed

    def test_global(self):
        result = check_whitelist("global x")
        assert not result.passed

    def test_nonlocal(self):
        # nonlocal only valid inside a function, but we still reject it
        result = check_whitelist("def f():\n    nonlocal x")
        assert not result.passed  # function def itself is rejected

    def test_assert(self):
        result = check_whitelist("assert True")
        assert not result.passed

    def test_delete(self):
        result = check_whitelist("x = 1\ndel x")
        assert not result.passed

    def test_raise(self):
        result = check_whitelist("raise ValueError('oops')")
        assert not result.passed

    def test_return(self):
        result = check_whitelist("def f():\n    return 1")
        assert not result.passed

    def test_break(self):
        # Break is inside a for-loop syntactically, but rejected
        result = check_whitelist("for i in range(3):\n    break")
        assert not result.passed

    def test_continue(self):
        result = check_whitelist("for i in range(3):\n    continue")
        assert not result.passed


class TestImportValidation:
    """Import validation: only carpenter_tools and safe stdlib allowed."""

    def test_non_carpenter_import(self):
        result = check_whitelist("from os import path")
        assert not result.passed
        assert any("os" in v for v in result.violations)

    def test_unsafe_stdlib_rejected(self):
        result = check_whitelist("from subprocess import run")
        assert not result.passed
        assert any("subprocess" in v for v in result.violations)

    def test_carpenter_tools_deep_import(self):
        result = check_whitelist("from carpenter_tools.act.arc import create_batch")
        assert result.passed

    def test_carpenter_tools_policy_types(self):
        result = check_whitelist("from carpenter_tools.policy.types import EmailPolicy, Domain")
        assert result.passed


class TestSyntaxErrors:
    def test_syntax_error(self):
        result = check_whitelist("def ")
        assert not result.passed
        assert any("syntax" in v.lower() for v in result.violations)


class TestMixedCode:
    def test_mostly_ok_with_one_violation(self):
        code = """
x = 1
y = x + 2
while True:
    pass
"""
        result = check_whitelist(code)
        assert not result.passed
        assert len(result.violations) >= 1
        assert any("while" in v.lower() for v in result.violations)

    def test_complex_valid_code(self):
        code = """
from carpenter_tools.act import arc, messaging
from carpenter_tools.policy import EmailPolicy

sender = EmailPolicy("test@example.com")
recipients = [EmailPolicy("a@b.com"), EmailPolicy("c@d.com")]

for r in recipients:
    if sender == r:
        messaging.send(f"Match found: {sender}")
"""
        result = check_whitelist(code)
        assert result.passed
