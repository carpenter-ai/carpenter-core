"""Tests for the platform abstraction layer.

Covers detect_platform(), Platform protocol conformance, registration
error handling, and the executor/sandbox/tool-handler injection points
that are not already exercised by sibling test modules.

Existing tests NOT duplicated here:
- test_platform_injection.py: set_platform/get_platform round-trip, RuntimeError
- test_sandbox_provider.py: set_sandbox_provider, register_sandbox_method
- test_sandbox_config.py: SandboxConfig, sandbox_command, sandbox_shell_command
- test_execution_metadata.py: get_executor, register_executor (no-op)
- test_tool_handler_registry.py: register_tool_handler happy path
- test_platform_restart.py: restart handler, enqueue_restart, recovery
"""

from unittest.mock import patch

import pytest

import carpenter.platform as platform_mod
from carpenter.platform.base import Platform


# ── detect_platform() ─────────────────────────────────────────────


class TestDetectPlatform:
    """Tests for detect_platform() which maps sys.platform to identifiers."""

    def test_linux(self):
        with patch.object(platform_mod.sys, "platform", "linux"):
            assert platform_mod.detect_platform() == "linux"

    def test_darwin(self):
        with patch.object(platform_mod.sys, "platform", "darwin"):
            assert platform_mod.detect_platform() == "darwin"

    def test_win32(self):
        with patch.object(platform_mod.sys, "platform", "win32"):
            assert platform_mod.detect_platform() == "windows"

    def test_unknown_falls_through(self):
        """An unrecognised sys.platform is returned as-is."""
        with patch.object(platform_mod.sys, "platform", "freebsd13"):
            assert platform_mod.detect_platform() == "freebsd13"


# ── Platform protocol conformance ─────────────────────────────────


class _CompletePlatform:
    """A complete Platform implementation for protocol checks."""

    name = "test"

    def restart_process(self) -> None:
        pass

    def protect_file(self, path: str) -> None:
        pass

    def generate_service(
        self, name: str, command: list[str], description: str, **kw
    ) -> str | None:
        return None

    def install_service(self, name: str, service_content: str) -> bool:
        return False

    def graceful_kill(self, proc, grace_seconds: int = 5) -> None:
        pass


class _IncompletePlatform:
    """Missing required 'name' attribute and methods."""

    pass


class TestPlatformProtocol:
    """Verify the Platform protocol defines the expected interface."""

    def test_platform_is_a_protocol(self):
        """Platform is a typing.Protocol subclass."""
        from typing import Protocol as TypingProtocol

        assert issubclass(Platform, TypingProtocol)

    def test_protocol_defines_name_attribute(self):
        """Platform protocol requires a 'name' attribute."""
        # Protocol members are listed in __protocol_attrs__ or annotations
        annotations = getattr(Platform, "__annotations__", {})
        assert "name" in annotations
        assert annotations["name"] is str

    def test_protocol_defines_required_methods(self):
        """Platform protocol defines all expected methods."""
        expected_methods = {
            "restart_process",
            "protect_file",
            "generate_service",
            "install_service",
            "graceful_kill",
        }
        actual_methods = {
            name
            for name in dir(Platform)
            if not name.startswith("_") and callable(getattr(Platform, name))
        }
        assert expected_methods.issubset(actual_methods)

    def test_complete_implementation_has_all_members(self):
        """_CompletePlatform has every member the protocol requires."""
        impl = _CompletePlatform()
        assert hasattr(impl, "name")
        assert callable(impl.restart_process)
        assert callable(impl.protect_file)
        assert callable(impl.generate_service)
        assert callable(impl.install_service)
        assert callable(impl.graceful_kill)

    def test_complete_implementation_accepted_by_set_platform(self):
        """set_platform() accepts a complete implementation."""
        old = platform_mod._instance
        try:
            impl = _CompletePlatform()
            platform_mod.set_platform(impl)
            assert platform_mod.get_platform() is impl
        finally:
            platform_mod._instance = old

    def test_generate_service_signature(self):
        """generate_service has working_dir and env_file keyword args."""
        impl = _CompletePlatform()
        # Should accept keyword-only args without error
        result = impl.generate_service(
            "svc", ["cmd"], "desc", working_dir="/opt", env_file="/etc/env"
        )
        assert result is None


# ── set_platform replacement behaviour ────────────────────────────


class TestSetPlatformReplacement:
    """set_platform() can replace a previously injected platform."""

    def test_second_set_replaces_first(self):
        old = platform_mod._instance
        try:
            first = _CompletePlatform()
            first.name = "first"
            second = _CompletePlatform()
            second.name = "second"

            platform_mod.set_platform(first)
            assert platform_mod.get_platform().name == "first"

            platform_mod.set_platform(second)
            assert platform_mod.get_platform().name == "second"
        finally:
            platform_mod._instance = old

    def test_set_platform_accepts_none_implicitly(self):
        """Setting _instance to None resets the platform (internal use)."""
        old = platform_mod._instance
        try:
            platform_mod.set_platform(_CompletePlatform())
            assert platform_mod.get_platform() is not None

            # Manually reset (internal, used in test fixtures)
            platform_mod._instance = None
            with pytest.raises(RuntimeError, match="No platform registered"):
                platform_mod.get_platform()
        finally:
            platform_mod._instance = old


# ── register_executor no-op shim ──────────────────────────────────


class TestRegisterExecutorShim:
    """register_executor() is a no-op backward-compat shim."""

    def test_register_does_not_affect_get_executor(self):
        """Registering an executor name does not change get_executor()."""
        import carpenter.executor as executor_mod

        # Register a fake executor class
        executor_mod.register_executor("fake_docker", type("FakeDocker", (), {}))

        # get_executor() still returns the RestrictedExecutor
        result = executor_mod.get_executor()
        assert result.name == "restricted"

    def test_register_executor_does_not_raise(self):
        """register_executor() never raises, even with unusual inputs."""
        import carpenter.executor as executor_mod

        # Various inputs that should all be silently accepted
        executor_mod.register_executor("subprocess", object)
        executor_mod.register_executor("docker", type("D", (), {}))
        executor_mod.register_executor("", type("Empty", (), {}))


# ── register_tool_handler PLATFORM_TOOLS guard ────────────────────


class TestRegisterToolHandlerGuard:
    """register_tool_handler() rejects overrides for platform-boundary tools."""

    def test_cannot_override_platform_tool(self):
        """Registering a handler for a PLATFORM_TOOLS name raises ValueError."""
        from carpenter.agent.invocation import register_tool_handler

        with pytest.raises(ValueError, match="Cannot override platform tool"):
            register_tool_handler("submit_code", lambda ti, **kw: "hacked")

    def test_cannot_override_escalate(self):
        """The 'escalate' tool is in PLATFORM_TOOLS and cannot be overridden."""
        from carpenter.agent.invocation import register_tool_handler

        with pytest.raises(ValueError, match="Cannot override platform tool"):
            register_tool_handler("escalate", lambda ti, **kw: "nope")

    def test_cannot_override_escalate_current_arc(self):
        """escalate_current_arc is in PLATFORM_TOOLS."""
        from carpenter.agent.invocation import register_tool_handler

        with pytest.raises(ValueError, match="Cannot override platform tool"):
            register_tool_handler(
                "escalate_current_arc", lambda ti, **kw: "nope"
            )


# ── Sandbox method registry isolation ─────────────────────────────


class TestSandboxMethodRegistry:
    """Additional sandbox method registration edge cases."""

    def test_overwrite_existing_method(self):
        """Re-registering a sandbox method replaces the previous one."""
        import carpenter.sandbox as sandbox_mod

        old_methods = sandbox_mod._sandbox_methods.copy()
        try:
            def first_cmd(command, write_dirs):
                return ["first"] + command

            def first_shell(shell_cmd, cwd, write_dirs):
                return ["first", "bash", "-c", shell_cmd]

            def second_cmd(command, write_dirs):
                return ["second"] + command

            def second_shell(shell_cmd, cwd, write_dirs):
                return ["second", "bash", "-c", shell_cmd]

            sandbox_mod.register_sandbox_method("test_method", first_cmd, first_shell)
            cfg = sandbox_mod.SandboxConfig(
                method="test_method", allowed_write_dirs=["/tmp"]
            )
            assert sandbox_mod.sandbox_command(["echo"], cfg)[0] == "first"

            sandbox_mod.register_sandbox_method("test_method", second_cmd, second_shell)
            assert sandbox_mod.sandbox_command(["echo"], cfg)[0] == "second"
        finally:
            sandbox_mod._sandbox_methods.clear()
            sandbox_mod._sandbox_methods.update(old_methods)

    def test_unknown_method_fail_closed_raises(self):
        """An unknown sandbox method with on_failure=closed raises SandboxError."""
        import carpenter.sandbox as sandbox_mod

        cfg = sandbox_mod.SandboxConfig(
            method="nonexistent_sandbox",
            on_failure="closed",
        )
        with pytest.raises(sandbox_mod.SandboxError, match="Unknown method"):
            sandbox_mod.sandbox_command(["echo", "hi"], cfg)

    def test_unknown_method_fail_closed_shell_raises(self):
        """An unknown sandbox method with on_failure=closed raises for shell commands too."""
        import carpenter.sandbox as sandbox_mod

        cfg = sandbox_mod.SandboxConfig(
            method="nonexistent_sandbox",
            on_failure="closed",
        )
        with pytest.raises(sandbox_mod.SandboxError, match="Unknown method"):
            sandbox_mod.sandbox_shell_command("echo hi", "/tmp", cfg)


# ── __all__ exports ───────────────────────────────────────────────


class TestModuleExports:
    """Verify that public APIs are exported in __all__."""

    def test_platform_module_all(self):
        assert "Platform" in platform_mod.__all__
        assert "detect_platform" in platform_mod.__all__
        assert "get_platform" in platform_mod.__all__
        assert "set_platform" in platform_mod.__all__

    def test_executor_module_all(self):
        import carpenter.executor as executor_mod

        assert "get_executor" in executor_mod.__all__
        assert "register_executor" in executor_mod.__all__
        assert "RestrictedExecutor" in executor_mod.__all__
