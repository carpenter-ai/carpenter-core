"""Tests for carpenter.config."""

import os
from pathlib import Path

import pytest


def test_defaults_have_all_expected_keys():
    """DEFAULTS dict contains all required config keys."""
    from carpenter.config import DEFAULTS

    expected = {
        "base_dir", "database_path", "log_dir", "code_dir",
        "workspaces_dir", "templates_dir", "tools_dir",
        "executor_type", "context_compaction_hours", "workspace_retention_days",
        "workspace_retention_count", "arc_archive_days", "mechanical_retry_max",
        "agentic_iteration_budget", "agentic_iteration_cap", "heartbeat_seconds",
        "host", "port", "ui_token", "allow_insecure_bind",
    }
    assert expected.issubset(set(DEFAULTS.keys()))


def test_credential_files_not_in_defaults():
    """credential_files is no longer in DEFAULTS (removed in clean-cut migration)."""
    from carpenter.config import DEFAULTS

    assert "credential_files" not in DEFAULTS


def test_load_config_returns_defaults_without_yaml_or_env(tmp_path, monkeypatch):
    """load_config with no YAML file and no env vars returns defaults."""
    from carpenter.config import load_config, DEFAULTS

    # Point to nonexistent YAML
    config = load_config(yaml_path=str(tmp_path / "nonexistent.yaml"))
    for key in DEFAULTS:
        assert key in config


def test_yaml_overrides_defaults(tmp_path):
    """YAML values override defaults."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("heartbeat_seconds: 10\nexecutor_type: restricted\n")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config["heartbeat_seconds"] == 10
    assert config["executor_type"] == "restricted"


def test_path_expansion(tmp_path):
    """Tilde in path values is expanded."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("base_dir: ~/my_carpenter\n")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert "~" not in config["base_dir"]
    assert config["base_dir"].endswith("/my_carpenter")


# ── .env loading tests ─────────────────────────────────────────────


def test_dot_env_loads_credential(tmp_path):
    """Credential keys in {base_dir}/.env are loaded into config."""
    dot_env = tmp_path / ".env"
    dot_env.write_text("ANTHROPIC_API_KEY=sk-test-key-123\nGIT_TOKEN=tok-456\n")
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(f"base_dir: {tmp_path}\n")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config["claude_api_key"] == "sk-test-key-123"
    assert config["git_token"] == "tok-456"


def test_dot_env_overrides_yaml(tmp_path):
    """.env values beat YAML values for credential keys."""
    dot_env = tmp_path / ".env"
    dot_env.write_text("GIT_TOKEN=from-env-file\n")
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(f"base_dir: {tmp_path}\ngit_token: from-yaml\n")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config["git_token"] == "from-env-file"


def test_dot_env_unknown_key_ignored(tmp_path):
    """Unknown keys in .env are silently ignored."""
    dot_env = tmp_path / ".env"
    dot_env.write_text("UNKNOWN_KEY=ignored\n")
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(f"base_dir: {tmp_path}\n")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert "UNKNOWN_KEY" not in config


def test_dot_env_missing_no_crash(tmp_path):
    """Absent .env file is silently skipped."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(f"base_dir: {tmp_path}\n")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config.get("claude_api_key", "") == ""


def test_dot_env_ignores_comments(tmp_path):
    """Comment lines in .env are ignored."""
    dot_env = tmp_path / ".env"
    dot_env.write_text("# this is a comment\nANTHROPIC_API_KEY=real-key\n")
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(f"base_dir: {tmp_path}\n")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config["claude_api_key"] == "real-key"


# ── Credential env var tests ───────────────────────────────────────


def test_credential_env_var_overrides_dot_env(tmp_path, monkeypatch):
    """Standard credential env vars beat .env values."""
    dot_env = tmp_path / ".env"
    dot_env.write_text("ANTHROPIC_API_KEY=from-dot-env\n")
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(f"base_dir: {tmp_path}\n")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-actual-env")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config["claude_api_key"] == "from-actual-env"


def test_credential_env_var_overrides_yaml(tmp_path, monkeypatch):
    """Standard credential env var beats YAML."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(f"base_dir: {tmp_path}\ngit_token: from-yaml\n")

    monkeypatch.setenv("GIT_TOKEN", "from-env")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config["git_token"] == "from-env"


def test_forgejo_token_env_var_backward_compat(tmp_path, monkeypatch):
    """FORGEJO_TOKEN env var still loads into git_token via backward compat."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(f"base_dir: {tmp_path}\n")

    monkeypatch.setenv("FORGEJO_TOKEN", "compat-tok")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config["git_token"] == "compat-tok"


def test_forgejo_url_yaml_backward_compat(tmp_path):
    """Old forgejo_url in YAML is migrated to git_server_url."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(f"base_dir: {tmp_path}\nforgejo_url: https://forge.example.com\n")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config["git_server_url"] == "https://forge.example.com"


def test_new_key_takes_precedence_over_old(tmp_path):
    """New git_server_url takes precedence over old forgejo_url."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(
        f"base_dir: {tmp_path}\n"
        "forgejo_url: https://old.example.com\n"
        "git_server_url: https://new.example.com\n"
    )

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config["git_server_url"] == "https://new.example.com"


def test_non_credential_env_vars_not_loaded(tmp_path, monkeypatch):
    """Arbitrary env vars (like the old TC_* convention) are not auto-loaded."""
    monkeypatch.setenv("TC_HEARTBEAT_SECONDS", "999")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(tmp_path / "nonexistent.yaml"))
    assert config["heartbeat_seconds"] != 999


# ── Credential registry tests ──────────────────────────────────────


def test_credential_registry_loaded_from_base_dir(tmp_path):
    """credential_registry.yaml in base_dir is loaded into CREDENTIAL_REGISTRY."""
    dot_env = tmp_path / ".env"
    dot_env.write_text("")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    registry_file = config_dir / "credential_registry.yaml"
    registry_file.write_text(
        "MY_CUSTOM_KEY:\n"
        "  config_key: my_custom\n"
        "  description: Test key\n"
    )
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(f"base_dir: {tmp_path}\n")

    from carpenter.config import load_config
    import carpenter.config as cfg_module

    load_config(yaml_path=str(yaml_file))
    assert "MY_CUSTOM_KEY" in cfg_module.CREDENTIAL_REGISTRY


def test_credential_registry_missing_no_crash(tmp_path):
    """Absent credential_registry.yaml is silently handled."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(f"base_dir: {tmp_path}\n")

    from carpenter.config import load_config

    # Should not raise
    config = load_config(yaml_path=str(yaml_file))
    assert config is not None


# ── get_config function tests ──────────────────────────────────────


def test_get_config_returns_value():
    """get_config() reads from the live CONFIG cache."""
    from carpenter.config import get_config, CONFIG

    # Any key that should be in CONFIG
    assert get_config("host") == CONFIG["host"]


def test_get_config_returns_default_for_missing_key():
    """get_config() returns provided default for absent keys."""
    from carpenter.config import get_config

    assert get_config("nonexistent_key_xyz", "fallback") == "fallback"


# ── TLS configuration tests ───────────────────────────────────────


def test_tls_defaults():
    """TLS is disabled by default."""
    from carpenter.config import DEFAULTS

    assert DEFAULTS["tls_enabled"] is False
    assert DEFAULTS["tls_cert_path"] == ""
    assert DEFAULTS["tls_key_path"] == ""
    assert DEFAULTS["tls_domain"] == ""
    assert DEFAULTS["tls_ca_path"] == ""


def test_tls_paths_expanded():
    """TLS path keys are expanded by _expand_paths."""
    from carpenter.config import _expand_paths

    cfg = {
        "tls_cert_path": "~/certs/fullchain.pem",
        "tls_key_path": "~/certs/privkey.pem",
        "tls_ca_path": "~/certs/ca.pem",
    }
    result = _expand_paths(cfg)
    assert "~" not in result["tls_cert_path"]
    assert "~" not in result["tls_key_path"]
    assert "~" not in result["tls_ca_path"]


def test_tls_domain_from_yaml(tmp_path):
    """TLS domain can be set via YAML."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("tls_domain: example.com\n")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config["tls_domain"] == "example.com"


# ── CARPENTER_CONFIG bootstrap env var ─────────────────────────


def test_carpenter_config_env_var(tmp_path, monkeypatch):
    """CARPENTER_CONFIG env var selects the config file path."""
    yaml_file = tmp_path / "custom_config.yaml"
    yaml_file.write_text("heartbeat_seconds: 99\n")

    monkeypatch.setenv("CARPENTER_CONFIG", str(yaml_file))

    from carpenter.config import load_config

    config = load_config()  # no yaml_path given — should use env var
    assert config["heartbeat_seconds"] == 99


# ── _coerce_types ──────────────────────────────────────────────────


def test_ui_token_loaded_from_dot_env(tmp_path):
    """UI_TOKEN in .env sets ui_token (tokens must not live in config.yaml)."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(f"base_dir: {tmp_path}\n")
    dot_env = tmp_path / ".env"
    dot_env.write_text("UI_TOKEN=mysecrettoken\n")
    dot_env.chmod(0o600)

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config["ui_token"] == "mysecrettoken"


def test_coerce_quoted_int(tmp_path):
    """Quoted integer in YAML is coerced to int."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("port: '9999'\n")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config["port"] == 9999
    assert isinstance(config["port"], int)


def test_coerce_quoted_bool_true(tmp_path):
    """Quoted 'true' in YAML is coerced to True for bool fields."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("allow_insecure_bind: 'true'\n")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config["allow_insecure_bind"] is True


def test_coerce_quoted_bool_false(tmp_path):
    """Quoted 'false' in YAML is coerced to False for bool fields."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("allow_insecure_bind: 'false'\n")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config["allow_insecure_bind"] is False


def test_coerce_unquoted_values_unchanged(tmp_path):
    """Unquoted native YAML values are not changed."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("port: 8080\nallow_insecure_bind: true\n")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config["port"] == 8080
    assert config["allow_insecure_bind"] is True


def test_coerce_invalid_int_left_as_string(tmp_path):
    """A string that cannot be coerced is left as-is (no crash)."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("port: 'not-a-number'\n")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))
    assert config["port"] == "not-a-number"  # left as string, no crash
