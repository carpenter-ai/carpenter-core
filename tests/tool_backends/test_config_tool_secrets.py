"""``config.get_value`` must not hand credentials to executor code.

CONFIG holds values loaded from ``.env`` and the environment.
``config.get_value`` is session-exempt, so any executor code (including
an untrusted arc) can call it without a reviewed session.
"""
from __future__ import annotations

import pytest

from carpenter import config as carpenter_config
from carpenter.executor.dispatch_bridge import DispatchError, validate_and_dispatch
from carpenter.tool_backends import config_tool


@pytest.fixture
def secrets_in_config(monkeypatch):
    values = {
        "ui_token": "ui-secret-value",
        "claude_api_key": "api-secret-value",
        "git_token": "forge-secret-value",
        "tls_key_password": "tls-secret-value",
        "db_encryption_key": "db-secret-value",
    }
    for k, v in values.items():
        monkeypatch.setitem(carpenter_config.CONFIG, k, v)
    return values


@pytest.mark.parametrize("key", [
    "ui_token", "claude_api_key", "git_token", "tls_key_password",
    "db_encryption_key", "UI_TOKEN", "tinfoil_api_key",
])
def test_credential_keys_refused(secrets_in_config, key):
    with pytest.raises(ValueError, match="credential"):
        config_tool.handle_get_value({"key": key})


def test_registry_credential_key_refused(monkeypatch):
    monkeypatch.setattr(
        carpenter_config, "CREDENTIAL_REGISTRY",
        {"WEATHER_KEY": {"config_key": "weather_service_auth"}},
    )
    monkeypatch.setitem(carpenter_config.CONFIG, "weather_service_auth", "s3cret")
    with pytest.raises(ValueError):
        config_tool.handle_get_value({"key": "weather_service_auth"})


def test_nested_secret_redacted(monkeypatch):
    monkeypatch.setitem(carpenter_config.CONFIG, "some_service", {
        "url": "https://service.example/",
        "api_key": "nested-secret",
        "inner": {"password": "deeper-secret", "timeout": 5},
    })
    result = config_tool.handle_get_value({"key": "some_service"})
    value = result["value"]
    assert value["url"] == "https://service.example/"
    assert value["api_key"] == "<redacted>"
    assert value["inner"] == {"password": "<redacted>", "timeout": 5}
    assert "secret" not in repr(result)


def test_dotted_secret_refused(monkeypatch):
    monkeypatch.setitem(carpenter_config.CONFIG, "some_service", {"api_key": "x"})
    with pytest.raises(ValueError):
        config_tool.handle_get_value({"key": "some_service.api_key"})


@pytest.mark.parametrize("key", [
    "git_url", "memory_recent_hints", "compaction_threshold_tokens",
    "web_response_max_chars", "port",
])
def test_ordinary_keys_still_readable(key):
    result = config_tool.handle_get_value({"key": key})
    assert result["key"] == key
    assert result["value"] == carpenter_config.CONFIG.get(key)


def test_executor_dispatch_cannot_read_ui_token(secrets_in_config):
    """No reviewed session is needed to call config.get_value, so the
    refusal must hold on the executor dispatch path itself."""
    with pytest.raises(DispatchError) as exc_info:
        validate_and_dispatch("config.get_value", {"key": "ui_token"})
    assert "ui-secret-value" not in str(exc_info.value)
