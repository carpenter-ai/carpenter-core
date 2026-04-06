"""Tests for the RestrictedPython executor.

Covers: basic execution, restricted builtins, guard functions, dispatch,
timeout, and security (escape attempts).
"""

import time

import pytest

from carpenter.executor.restricted import (
    ExecutionResult,
    RestrictedExecutor,
    _build_namespace,
    _make_dispatch_fn,
    _terminate_thread,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _no_tools_handler(tool_name, params):
    """Tool handler that rejects all calls."""
    raise RuntimeError(f"No tool handler configured for {tool_name}")


def _echo_handler(tool_name, params):
    """Tool handler that returns the request as the result."""
    return {"tool": tool_name, "echo": params}


def _stateful_handler():
    """Return a handler + call log to verify dispatch interactions."""
    calls = []

    def handler(tool_name, params):
        calls.append((tool_name, params))
        if tool_name == "state.get":
            return {"value": params.get("key", "unknown")}
        if tool_name == "state.set":
            return {"success": True}
        if tool_name == "error.raise":
            raise ValueError(f"Intentional error: {params.get('msg', '')}")
        return {"ok": True}

    return handler, calls


# ── Basic execution ──────────────────────────────────────────────────


class TestBasicExecution:
    """Test that simple Python code executes correctly."""

    def test_simple_arithmetic(self):
        executor = RestrictedExecutor()
        result = executor.execute("x = 1 + 2")
        assert result.exit_code == 0
        assert result.error == ""
        assert result.timed_out is False

    def test_print_captured(self):
        executor = RestrictedExecutor()
        result = executor.execute("print('hello world')")
        assert result.exit_code == 0
        assert "hello world" in result.output

    def test_multiple_prints(self):
        executor = RestrictedExecutor()
        result = executor.execute("print('a')\nprint('b')\nprint('c')")
        assert "a" in result.output
        assert "b" in result.output
        assert "c" in result.output

    def test_loop_execution(self):
        executor = RestrictedExecutor()
        result = executor.execute(
            "total = 0\nfor i in range(10):\n    total += i\nprint(total)"
        )
        assert result.exit_code == 0
        assert "45" in result.output

    def test_dict_operations(self):
        executor = RestrictedExecutor()
        result = executor.execute(
            "d = {'a': 1, 'b': 2}\nprint(d['a'] + d['b'])"
        )
        assert "3" in result.output

    def test_list_operations(self):
        executor = RestrictedExecutor()
        result = executor.execute(
            "lst = [1, 2, 3]\nlst.append(4)\nprint(len(lst))"
        )
        assert "4" in result.output

    def test_string_operations(self):
        executor = RestrictedExecutor()
        result = executor.execute(
            "s = 'hello'\nprint(s.upper())"
        )
        assert "HELLO" in result.output

    def test_builtin_functions(self):
        """Verify that added builtins (sum, max, min, etc.) work."""
        executor = RestrictedExecutor()
        result = executor.execute(
            "print(sum([1,2,3]))\n"
            "print(max(1,2,3))\n"
            "print(min(1,2,3))\n"
            "print(list(range(3)))\n"
            "print(dict(a=1))\n"
            "print(set([1,2,2]))\n"
            "print(any([False, True]))\n"
            "print(all([True, True]))\n"
        )
        assert result.exit_code == 0
        assert "6" in result.output
        assert "3" in result.output
        assert "1" in result.output

    def test_enumerate(self):
        executor = RestrictedExecutor()
        result = executor.execute(
            "for i, v in enumerate(['a', 'b']):\n    print(i, v)"
        )
        assert "0 a" in result.output
        assert "1 b" in result.output

    def test_syntax_error_returns_error(self):
        executor = RestrictedExecutor()
        result = executor.execute("def f(:\n    pass")
        assert result.exit_code == 1
        assert "SyntaxError" in result.error

    def test_runtime_error_returns_error(self):
        executor = RestrictedExecutor()
        result = executor.execute("x = 1 / 0")
        assert result.exit_code == 1
        assert "ZeroDivisionError" in result.error

    def test_name_error_returns_error(self):
        executor = RestrictedExecutor()
        result = executor.execute("print(undefined_variable)")
        assert result.exit_code == 1
        assert "NameError" in result.error

    def test_empty_code(self):
        executor = RestrictedExecutor()
        result = executor.execute("")
        assert result.exit_code == 0
        assert result.error == ""


# ── Security: restricted builtins ────────────────────────────────────


class TestRestrictedBuiltins:
    """Test that dangerous builtins are blocked."""

    def test_import_blocked(self):
        executor = RestrictedExecutor()
        result = executor.execute("import os")
        # RestrictedPython blocks import at compile time or exec time
        assert result.exit_code == 1

    def test_exec_blocked(self):
        executor = RestrictedExecutor()
        result = executor.execute("exec('print(1)')")
        assert result.exit_code == 1

    def test_eval_blocked(self):
        executor = RestrictedExecutor()
        result = executor.execute("eval('1+1')")
        assert result.exit_code == 1

    def test_open_blocked(self):
        executor = RestrictedExecutor()
        result = executor.execute("f = open('/etc/passwd')")
        assert result.exit_code == 1

    def test_dunder_import_blocked(self):
        executor = RestrictedExecutor()
        result = executor.execute("__import__('os')")
        assert result.exit_code == 1

    def test_getattr_on_builtins_blocked(self):
        """Cannot use getattr to get around restrictions."""
        executor = RestrictedExecutor()
        result = executor.execute("getattr(str, '__bases__')")
        # getattr is not in safe_builtins; also __bases__ is blocked at compile time
        assert result.exit_code == 1


# ── Security: escape attempts ────────────────────────────────────────


class TestEscapeAttempts:
    """Test that known sandbox escape patterns are blocked."""

    def test_class_bases_blocked(self):
        """Cannot access __class__.__bases__ to walk the MRO."""
        executor = RestrictedExecutor()
        result = executor.execute("x = ().__class__.__bases__")
        assert result.exit_code == 1

    def test_subclasses_blocked(self):
        """Cannot access __subclasses__ to find dangerous classes."""
        executor = RestrictedExecutor()
        result = executor.execute("x = object.__subclasses__()")
        assert result.exit_code == 1

    def test_globals_blocked(self):
        """Cannot access __globals__ from a function."""
        executor = RestrictedExecutor()
        result = executor.execute(
            "def f(): pass\nx = f.__globals__"
        )
        assert result.exit_code == 1

    def test_code_blocked(self):
        """Cannot access __code__ from a function."""
        executor = RestrictedExecutor()
        result = executor.execute(
            "def f(): pass\nx = f.__code__"
        )
        assert result.exit_code == 1

    def test_builtins_access_blocked(self):
        """Cannot access __builtins__ dict directly."""
        executor = RestrictedExecutor()
        result = executor.execute("x = __builtins__.__dict__")
        assert result.exit_code == 1

    def test_module_attribute_blocked(self):
        """Cannot access __module__ or other dunder attributes."""
        executor = RestrictedExecutor()
        result = executor.execute("x = type.__module__")
        assert result.exit_code == 1

    def test_compile_builtin_blocked(self):
        """compile() is not available."""
        executor = RestrictedExecutor()
        result = executor.execute("c = compile('1+1', '<x>', 'eval')")
        assert result.exit_code == 1


# ── Dispatch ─────────────────────────────────────────────────────────


class TestDispatch:
    """Test the dispatch mechanism."""

    def test_dispatch_calls_handler(self):
        handler, calls = _stateful_handler()
        executor = RestrictedExecutor(tool_handler=handler)
        result = executor.execute(
            "r = dispatch('state.get', {'key': 'mykey'})\n"
            "print(r['value'])"
        )
        assert result.exit_code == 0
        assert "mykey" in result.output
        assert len(calls) == 1
        assert calls[0] == ("state.get", {"key": "mykey"})

    def test_dispatch_multiple_calls(self):
        handler, calls = _stateful_handler()
        executor = RestrictedExecutor(tool_handler=handler)
        result = executor.execute(
            "dispatch('state.set', {'key': 'a', 'value': 1})\n"
            "dispatch('state.set', {'key': 'b', 'value': 2})\n"
            "r = dispatch('state.get', {'key': 'c'})\n"
            "print(r['value'])"
        )
        assert result.exit_code == 0
        assert len(calls) == 3

    def test_dispatch_log_recorded(self):
        handler, _ = _stateful_handler()
        executor = RestrictedExecutor(tool_handler=handler)
        result = executor.execute(
            "dispatch('state.get', {'key': 'test'})"
        )
        assert len(result.dispatch_log) == 1
        assert result.dispatch_log[0]["tool_name"] == "state.get"
        assert "result" in result.dispatch_log[0]

    def test_dispatch_handler_error_propagates(self):
        handler, _ = _stateful_handler()
        executor = RestrictedExecutor(tool_handler=handler)
        result = executor.execute(
            "dispatch('error.raise', {'msg': 'boom'})"
        )
        assert result.exit_code == 1
        assert "boom" in result.error

    def test_dispatch_no_handler_configured(self):
        executor = RestrictedExecutor(tool_handler=None)
        result = executor.execute("dispatch('state.get', {'key': 'x'})")
        assert result.exit_code == 1
        assert "No tool handler" in result.error

    def test_dispatch_allowed_tools_whitelist(self):
        handler, calls = _stateful_handler()
        executor = RestrictedExecutor(tool_handler=handler)
        result = executor.execute(
            "dispatch('state.get', {'key': 'x'})",
            allowed_tools=["state.get"],
        )
        assert result.exit_code == 0
        assert len(calls) == 1

    def test_dispatch_blocked_by_whitelist(self):
        handler, calls = _stateful_handler()
        executor = RestrictedExecutor(tool_handler=handler)
        result = executor.execute(
            "dispatch('state.set', {'key': 'x', 'value': 1})",
            allowed_tools=["state.get"],
        )
        assert result.exit_code == 1
        assert "not in the allowed tools" in result.error
        assert len(calls) == 0

    def test_dispatch_invalid_tool_name_type(self):
        executor = RestrictedExecutor(tool_handler=_echo_handler)
        result = executor.execute("dispatch(123, {})")
        assert result.exit_code == 1
        assert "must be a string" in result.error

    def test_dispatch_invalid_params_type(self):
        executor = RestrictedExecutor(tool_handler=_echo_handler)
        result = executor.execute("dispatch('test', 'not_a_dict')")
        assert result.exit_code == 1
        assert "must be a dict" in result.error

    def test_dispatch_json_serialization_enforced(self):
        """Non-JSON-serializable params should fail."""
        executor = RestrictedExecutor(tool_handler=_echo_handler)
        result = executor.execute(
            "dispatch('test', {'key': set([1,2,3])})"
        )
        assert result.exit_code == 1
        assert "JSON-serializable" in result.error

    def test_dispatch_result_is_deserialized(self):
        """Dispatch results should be plain Python objects, not raw JSON."""
        executor = RestrictedExecutor(tool_handler=_echo_handler)
        result = executor.execute(
            "r = dispatch('test', {'key': 'val'})\n"
            "print(isinstance(r, dict))\n"
            "print(r['tool'])"
        )
        assert result.exit_code == 0
        assert "True" in result.output
        assert "test" in result.output


# ── Timeout ──────────────────────────────────────────────────────────


class TestTimeout:
    """Test the timeout mechanism."""

    def test_infinite_loop_times_out(self):
        executor = RestrictedExecutor()
        result = executor.execute(
            "while True:\n    pass",
            timeout=1.0,
        )
        assert result.timed_out is True
        assert result.exit_code == -1

    def test_normal_code_completes_within_timeout(self):
        executor = RestrictedExecutor()
        result = executor.execute(
            "x = sum(range(100))\nprint(x)",
            timeout=5.0,
        )
        assert result.timed_out is False
        assert result.exit_code == 0
        assert "4950" in result.output


# ── Extra namespace ──────────────────────────────────────────────────


class TestExtraNamespace:
    """Test injecting extra names into the execution namespace."""

    def test_extra_namespace_available(self):
        executor = RestrictedExecutor()
        result = executor.execute(
            "print(my_value)",
            extra_namespace={"my_value": 42},
        )
        assert result.exit_code == 0
        assert "42" in result.output

    def test_extra_namespace_callable(self):
        executor = RestrictedExecutor()
        result = executor.execute(
            "print(my_func(3, 4))",
            extra_namespace={"my_func": lambda a, b: a + b},
        )
        assert result.exit_code == 0
        assert "7" in result.output


# ── RestrictedExecutor as executor type ──────────────────────────────


class TestExecutorRegistration:
    """Test that RestrictedExecutor can be obtained via get_executor."""

    def test_get_executor_restricted(self):
        from carpenter.executor import get_executor
        executor = get_executor("restricted")
        assert executor.name == "restricted"
        assert isinstance(executor, RestrictedExecutor)


# ── Security fixes from PR #124 follow-up ────────────────────────────


class TestShutdownSentinelProtection:
    """Test that __shutdown__ sentinel is blocked from user code (Fix #3)."""

    def test_shutdown_sentinel_blocked(self):
        """User code cannot call dispatch('__shutdown__', {})."""
        executor = RestrictedExecutor(tool_handler=_echo_handler)
        result = executor.execute("dispatch('__shutdown__', {})")
        assert result.exit_code == 1
        assert "reserved for internal use" in result.error

    def test_shutdown_sentinel_does_not_hang(self):
        """Calling __shutdown__ raises error instead of hanging."""
        import time
        executor = RestrictedExecutor(tool_handler=_echo_handler)
        start = time.monotonic()
        result = executor.execute("dispatch('__shutdown__', {})", timeout=5.0)
        elapsed = time.monotonic() - start
        # Should fail immediately, not wait for 2-second shutdown grace period
        assert elapsed < 1.0
        assert result.exit_code == 1


class TestExtraNamespaceGuardProtection:
    """Test that extra_namespace cannot override guard functions (Fix #4)."""

    def test_cannot_override_getattr_guard(self):
        """extra_namespace cannot override _getattr_."""
        executor = RestrictedExecutor()
        with pytest.raises(ValueError, match="cannot override guard function"):
            executor.execute(
                "print('test')",
                extra_namespace={"_getattr_": lambda obj, name: None},
            )

    def test_cannot_override_getitem_guard(self):
        """extra_namespace cannot override _getitem_."""
        executor = RestrictedExecutor()
        with pytest.raises(ValueError, match="cannot override guard function"):
            executor.execute(
                "print('test')",
                extra_namespace={"_getitem_": lambda obj, key: None},
            )

    def test_cannot_override_write_guard(self):
        """extra_namespace cannot override _write_."""
        executor = RestrictedExecutor()
        with pytest.raises(ValueError, match="cannot override guard function"):
            executor.execute(
                "print('test')",
                extra_namespace={"_write_": lambda obj: None},
            )

    def test_cannot_override_builtins(self):
        """extra_namespace cannot override __builtins__."""
        executor = RestrictedExecutor()
        with pytest.raises(ValueError, match="cannot override guard function"):
            executor.execute(
                "print('test')",
                extra_namespace={"__builtins__": {}},
            )

    def test_cannot_override_inplacevar(self):
        """extra_namespace cannot override _inplacevar_."""
        executor = RestrictedExecutor()
        with pytest.raises(ValueError, match="cannot override guard function"):
            executor.execute(
                "print('test')",
                extra_namespace={"_inplacevar_": lambda op, x, y: None},
            )

    def test_cannot_override_iter_unpack_sequence(self):
        """extra_namespace cannot override _iter_unpack_sequence_."""
        executor = RestrictedExecutor()
        with pytest.raises(ValueError, match="cannot override guard function"):
            executor.execute(
                "print('test')",
                extra_namespace={"_iter_unpack_sequence_": lambda seq: None},
            )

    def test_safe_extra_namespace_still_works(self):
        """extra_namespace works for non-guard keys."""
        executor = RestrictedExecutor()
        result = executor.execute(
            "print(my_safe_value)",
            extra_namespace={"my_safe_value": "allowed"},
        )
        assert result.exit_code == 0
        assert "allowed" in result.output
