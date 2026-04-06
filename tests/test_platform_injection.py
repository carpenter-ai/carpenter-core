"""Tests for platform injection (set_platform / get_platform)."""

import carpenter.platform as platform_mod


class FakePlatform:
    """Minimal platform stub for testing."""
    name = "fake"

    def restart_process(self):
        pass

    def protect_file(self, path):
        pass

    def generate_service(self, name, command, description, **kw):
        return None

    def install_service(self, name, service_content):
        return False

    def graceful_kill(self, proc, grace_seconds=5):
        pass


def test_set_platform_overrides_get_platform():
    """set_platform() makes get_platform() return the injected instance."""
    old = platform_mod._instance
    try:
        fake = FakePlatform()
        platform_mod.set_platform(fake)
        result = platform_mod.get_platform()
        assert result is fake
        assert result.name == "fake"
    finally:
        platform_mod._instance = old


def test_get_platform_raises_when_not_injected():
    """get_platform() raises RuntimeError when no platform is registered."""
    import pytest
    old = platform_mod._instance
    try:
        platform_mod._instance = None
        with pytest.raises(RuntimeError, match="No platform registered"):
            platform_mod.get_platform()
    finally:
        platform_mod._instance = old
