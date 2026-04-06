"""Tests for carpenter.sandbox public API (config, command wrapping, failure handling)."""

import os
import pytest
from unittest.mock import patch, MagicMock

import carpenter.sandbox as sandbox_mod
from carpenter.sandbox import (
    SandboxConfig,
    SandboxError,
    get_sandbox_config,
    sandbox_command,
    sandbox_shell_command,
    _default_write_dirs,
)


@pytest.fixture(autouse=True)
def clear_sandbox_cache():
    """Clear the sandbox config cache before each test."""
    sandbox_mod._cached_config = None
    yield
    sandbox_mod._cached_config = None


class TestSandboxConfig:
    def test_defaults(self):
        """SandboxConfig has sensible defaults."""
        cfg = SandboxConfig()
        assert cfg.method == "none"
        assert cfg.allowed_write_dirs == []
        assert cfg.on_failure == "closed"


class TestGetSandboxConfig:
    def test_explicit_none_skips_detection(self):
        """method=none doesn't trigger auto-detection."""
        with patch.dict("carpenter.config.CONFIG",
                        {"sandbox": {"method": "none"}}):
            cfg = get_sandbox_config()
            assert cfg.method == "none"

    def test_explicit_namespace(self):
        """method=namespace is used directly without detection."""
        with patch.dict("carpenter.config.CONFIG",
                        {"sandbox": {"method": "namespace"}}):
            cfg = get_sandbox_config()
            assert cfg.method == "namespace"

    def test_auto_detection_dispatches(self):
        """method=auto uses injected sandbox provider and picks recommended."""
        mock_caps = {
            "namespace": True, "bubblewrap": False,
            "docker": False, "landlock": False,
            "recommended": "namespace",
        }
        old_provider = sandbox_mod._sandbox_provider
        try:
            sandbox_mod.set_sandbox_provider(lambda: mock_caps)
            with patch.dict("carpenter.config.CONFIG",
                            {"sandbox": {"method": "auto"}}):
                cfg = get_sandbox_config()
                assert cfg.method == "namespace"
        finally:
            sandbox_mod._sandbox_provider = old_provider

    def test_auto_detection_none_fallback(self):
        """method=auto falls back to none when no provider is registered."""
        old_provider = sandbox_mod._sandbox_provider
        try:
            sandbox_mod._sandbox_provider = None
            with patch.dict("carpenter.config.CONFIG",
                            {"sandbox": {"method": "auto"}}):
                cfg = get_sandbox_config()
                assert cfg.method == "none"
        finally:
            sandbox_mod._sandbox_provider = old_provider

    def test_caching(self):
        """Result is cached after first call."""
        with patch.dict("carpenter.config.CONFIG",
                        {"sandbox": {"method": "none"}}):
            cfg1 = get_sandbox_config()
            cfg2 = get_sandbox_config()
            assert cfg1 is cfg2

    def test_default_write_dirs_from_config(self):
        """Default write dirs computed from config paths."""
        with patch.dict("carpenter.config.CONFIG", {
            "workspaces_dir": "/data/workspaces",
            "code_dir": "/data/code",
            "log_dir": "/data/logs",
        }):
            dirs = _default_write_dirs()
            assert "/data/workspaces" in dirs
            assert "/data/code" in dirs
            assert "/data/logs" in dirs
            import tempfile
            assert tempfile.gettempdir() in dirs

    def test_configured_write_dirs_override(self):
        """Explicit allowed_write_dirs overrides defaults."""
        with patch.dict("carpenter.config.CONFIG", {
            "sandbox": {
                "method": "none",
                "allowed_write_dirs": ["/custom/dir"],
            },
        }):
            cfg = get_sandbox_config()
            assert cfg.allowed_write_dirs == ["/custom/dir"]

    def test_on_failure_from_config(self):
        """on_failure policy is read from config."""
        with patch.dict("carpenter.config.CONFIG",
                        {"sandbox": {"method": "none", "on_failure": "closed"}}):
            cfg = get_sandbox_config()
            assert cfg.on_failure == "closed"


class TestSandboxCommand:
    """Tests for sandbox_command() using registered sandbox methods."""

    @pytest.fixture(autouse=True)
    def _register_mock_methods(self):
        """Register mock sandbox methods for testing."""
        from carpenter.sandbox import register_sandbox_method

        old_methods = sandbox_mod._sandbox_methods.copy()

        def _ns_cmd(command, write_dirs):
            for d in write_dirs:
                if not os.path.isabs(d):
                    raise ValueError(f"write dir must be absolute: {d}")
            return ["unshare", "--mount", "--"] + command

        def _ns_shell(shell_cmd, cwd, write_dirs):
            for d in write_dirs:
                if not os.path.isabs(d):
                    raise ValueError(f"write dir must be absolute: {d}")
            return ["unshare", "--mount", "--", "bash", "-c", shell_cmd]

        def _bwrap_cmd(command, write_dirs):
            return ["bwrap", "--ro-bind", "/", "/"] + command

        def _bwrap_shell(shell_cmd, cwd, write_dirs):
            return ["bwrap", "--ro-bind", "/", "/", "bash", "-c", shell_cmd]

        import sys
        def _landlock_cmd(command, write_dirs):
            return [sys.executable, "-m", "carpenter_linux.sandbox._landlock_helper"] + command

        def _landlock_shell(shell_cmd, cwd, write_dirs):
            return [sys.executable, "-m", "carpenter_linux.sandbox._landlock_helper", "bash", "-c", shell_cmd]

        def _apparmor_cmd(command, write_dirs):
            return ["aa-exec", "-p", "carpenter-sandbox", "--"] + command

        def _apparmor_shell(shell_cmd, cwd, write_dirs):
            return ["aa-exec", "-p", "carpenter-sandbox", "--", "bash", "-c", shell_cmd]

        register_sandbox_method("namespace", _ns_cmd, _ns_shell)
        register_sandbox_method("bubblewrap", _bwrap_cmd, _bwrap_shell)
        register_sandbox_method("landlock", _landlock_cmd, _landlock_shell)
        register_sandbox_method("apparmor", _apparmor_cmd, _apparmor_shell)

        yield

        sandbox_mod._sandbox_methods.clear()
        sandbox_mod._sandbox_methods.update(old_methods)

    def test_none_returns_original(self):
        """method=none returns command unchanged."""
        cfg = SandboxConfig(method="none")
        cmd = ["python3", "script.py"]
        result = sandbox_command(cmd, cfg)
        assert result == cmd

    def test_namespace_wraps_command(self, tmp_path):
        """method=namespace wraps with unshare."""
        cfg = SandboxConfig(method="namespace", allowed_write_dirs=[str(tmp_path)])
        result = sandbox_command(["python3", "script.py"], cfg)
        assert result[0] == "unshare"

    def test_bubblewrap_wraps_command(self, tmp_path):
        """method=bubblewrap wraps with bwrap."""
        cfg = SandboxConfig(method="bubblewrap", allowed_write_dirs=[str(tmp_path)])
        result = sandbox_command(["python3", "script.py"], cfg)
        assert result[0] == "bwrap"

    def test_landlock_wraps_command(self, tmp_path):
        """method=landlock wraps with landlock helper."""
        import sys
        cfg = SandboxConfig(method="landlock", allowed_write_dirs=[str(tmp_path)])
        result = sandbox_command(["python3", "script.py"], cfg)
        assert result[0] == sys.executable

    def test_apparmor_wraps_command(self, tmp_path):
        """method=apparmor wraps with aa-exec."""
        cfg = SandboxConfig(method="apparmor", allowed_write_dirs=[str(tmp_path)])
        result = sandbox_command(["python3", "script.py"], cfg)
        assert result[0] == "aa-exec"

    def test_fail_open_returns_original(self):
        """Failure with on_failure=open returns original command."""
        cfg = SandboxConfig(method="namespace", on_failure="open",
                            allowed_write_dirs=["relative/bad"])
        cmd = ["python3", "script.py"]
        # relative path will cause ValueError in build_command
        result = sandbox_command(cmd, cfg)
        assert result == cmd

    def test_fail_closed_raises(self):
        """Failure with on_failure=closed raises SandboxError."""
        cfg = SandboxConfig(method="namespace", on_failure="closed",
                            allowed_write_dirs=["relative/bad"])
        with pytest.raises(SandboxError):
            sandbox_command(["python3", "script.py"], cfg)

    def test_unknown_method_fail_open(self):
        """Unknown method with on_failure=open returns original."""
        cfg = SandboxConfig(method="unknown_method", on_failure="open")
        cmd = ["echo", "test"]
        result = sandbox_command(cmd, cfg)
        assert result == cmd


class TestSandboxShellCommand:
    """Tests for sandbox_shell_command() using registered sandbox methods."""

    @pytest.fixture(autouse=True)
    def _register_mock_methods(self):
        """Register mock sandbox methods for testing."""
        from carpenter.sandbox import register_sandbox_method

        old_methods = sandbox_mod._sandbox_methods.copy()

        def _ns_cmd(command, write_dirs):
            for d in write_dirs:
                if not os.path.isabs(d):
                    raise ValueError(f"write dir must be absolute: {d}")
            return ["unshare", "--mount", "--"] + command

        def _ns_shell(shell_cmd, cwd, write_dirs):
            for d in write_dirs:
                if not os.path.isabs(d):
                    raise ValueError(f"write dir must be absolute: {d}")
            return ["unshare", "--mount", "--", "bash", "-c", shell_cmd]

        def _bwrap_cmd(command, write_dirs):
            return ["bwrap", "--ro-bind", "/", "/"] + command

        def _bwrap_shell(shell_cmd, cwd, write_dirs):
            return ["bwrap", "--ro-bind", "/", "/", "bash", "-c", shell_cmd]

        import sys
        def _landlock_cmd(command, write_dirs):
            return [sys.executable, "-m", "mock_landlock"] + command

        def _landlock_shell(shell_cmd, cwd, write_dirs):
            return [sys.executable, "-m", "mock_landlock", "bash", "-c", shell_cmd]

        def _apparmor_cmd(command, write_dirs):
            return ["aa-exec", "-p", "carpenter-sandbox", "--"] + command

        def _apparmor_shell(shell_cmd, cwd, write_dirs):
            return ["aa-exec", "-p", "carpenter-sandbox", "--", "bash", "-c", shell_cmd]

        register_sandbox_method("namespace", _ns_cmd, _ns_shell)
        register_sandbox_method("bubblewrap", _bwrap_cmd, _bwrap_shell)
        register_sandbox_method("landlock", _landlock_cmd, _landlock_shell)
        register_sandbox_method("apparmor", _apparmor_cmd, _apparmor_shell)

        yield

        sandbox_mod._sandbox_methods.clear()
        sandbox_mod._sandbox_methods.update(old_methods)

    def test_none_returns_bash(self):
        """method=none returns simple bash -c."""
        cfg = SandboxConfig(method="none")
        result = sandbox_shell_command("echo hello", "/tmp", cfg)
        assert result == ["bash", "-c", "echo hello"]

    def test_namespace_wraps_shell(self, tmp_path):
        """method=namespace wraps shell command."""
        cfg = SandboxConfig(method="namespace", allowed_write_dirs=[str(tmp_path)])
        result = sandbox_shell_command("echo hello", str(tmp_path), cfg)
        assert result[0] == "unshare"

    def test_bubblewrap_wraps_shell(self, tmp_path):
        """method=bubblewrap wraps shell command."""
        cfg = SandboxConfig(method="bubblewrap", allowed_write_dirs=[str(tmp_path)])
        result = sandbox_shell_command("echo hello", str(tmp_path), cfg)
        assert result[0] == "bwrap"

    def test_landlock_wraps_shell(self, tmp_path):
        """method=landlock wraps shell command."""
        import sys
        cfg = SandboxConfig(method="landlock", allowed_write_dirs=[str(tmp_path)])
        result = sandbox_shell_command("echo hello", str(tmp_path), cfg)
        assert result[0] == sys.executable

    def test_apparmor_wraps_shell(self, tmp_path):
        """method=apparmor wraps shell command."""
        cfg = SandboxConfig(method="apparmor", allowed_write_dirs=[str(tmp_path)])
        result = sandbox_shell_command("echo hello", str(tmp_path), cfg)
        assert result[0] == "aa-exec"

    def test_fail_open_returns_bash(self):
        """Failure with on_failure=open returns simple bash -c."""
        cfg = SandboxConfig(method="namespace", on_failure="open",
                            allowed_write_dirs=["relative/bad"])
        result = sandbox_shell_command("echo hello", "/tmp", cfg)
        assert result == ["bash", "-c", "echo hello"]

    def test_fail_closed_raises(self):
        """Failure with on_failure=closed raises SandboxError."""
        cfg = SandboxConfig(method="namespace", on_failure="closed",
                            allowed_write_dirs=["relative/bad"])
        with pytest.raises(SandboxError):
            sandbox_shell_command("echo hello", "/tmp", cfg)


class TestEnvVarBlocking:
    def test_sandbox_env_vars_ignored(self):
        """Arbitrary env vars (like the old TC_SANDBOX_* pattern) do not override sandbox config."""
        env_patch = {
            "TC_SANDBOX_METHOD": "none",
            "TC_SANDBOX_ON_FAILURE": "open",
        }
        with patch.dict(os.environ, env_patch):
            from carpenter.config import _load_env
            result = _load_env()
            # sandbox keys should not appear because _load_env skips them
            assert "sandbox_method" not in result
            assert "sandbox_on_failure" not in result
            # The key "sandbox" should not appear either
            assert "sandbox" not in result
