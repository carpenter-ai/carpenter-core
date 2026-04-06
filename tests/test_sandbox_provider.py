"""Tests for sandbox provider injection and method registration."""

import carpenter.sandbox as sandbox_mod


def test_set_sandbox_provider_overrides_auto_detection():
    """set_sandbox_provider() overrides the auto-detection in get_sandbox_config()."""
    old_provider = sandbox_mod._sandbox_provider
    old_cached = sandbox_mod._cached_config
    try:
        sandbox_mod._cached_config = None  # Clear cache
        sandbox_mod.set_sandbox_provider(
            lambda: {"recommended": "none", "test": True}
        )
        cfg = sandbox_mod.get_sandbox_config()
        assert cfg.method == "none"
    finally:
        sandbox_mod._sandbox_provider = old_provider
        sandbox_mod._cached_config = old_cached


def test_register_sandbox_method_used_by_sandbox_command():
    """register_sandbox_method() methods are used by sandbox_command()."""
    old_methods = sandbox_mod._sandbox_methods.copy()
    try:
        def fake_build_cmd(command, write_dirs):
            return ["fake-sandbox", "--"] + command

        def fake_build_shell_cmd(shell_cmd, cwd, write_dirs):
            return ["fake-sandbox", "--", "bash", "-c", shell_cmd]

        sandbox_mod.register_sandbox_method("fake", fake_build_cmd, fake_build_shell_cmd)

        cfg = sandbox_mod.SandboxConfig(method="fake", allowed_write_dirs=["/tmp"])
        result = sandbox_mod.sandbox_command(["python3", "test.py"], cfg)
        assert result == ["fake-sandbox", "--", "python3", "test.py"]

        result2 = sandbox_mod.sandbox_shell_command("echo hi", "/tmp", cfg)
        assert result2 == ["fake-sandbox", "--", "bash", "-c", "echo hi"]
    finally:
        sandbox_mod._sandbox_methods = old_methods
