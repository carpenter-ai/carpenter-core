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


def test_non_credential_env_vars_not_loaded(tmp_path, monkeypatch):
    """Arbitrary prefixed env vars are not auto-loaded into config."""
    monkeypatch.setenv("EXTERNAL_HEARTBEAT_SECONDS", "999")

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


# ---------------------------------------------------------------------------
# Path derivation from base_dir (layout-agnostic install.sh support)
# ---------------------------------------------------------------------------


def test_minimal_yaml_derives_all_paths_from_base_dir(tmp_path):
    """A config.yaml with only base_dir produces a fully-populated path config.

    install.sh should be able to emit just ``base_dir`` and let the server
    derive every layout-dependent path.  This verifies that contract.
    """
    base = tmp_path / "foo"
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(f"base_dir: {base}\n")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))

    base_s = str(base)
    assert config["base_dir"] == base_s
    assert config["database_path"] == os.path.join(base_s, "data", "platform.db")
    assert config["log_dir"] == os.path.join(base_s, "data", "logs")
    assert config["code_dir"] == os.path.join(base_s, "data", "code")
    assert config["workspaces_dir"] == os.path.join(base_s, "data", "workspaces")
    assert config["templates_dir"] == os.path.join(base_s, "config", "templates")
    assert config["tools_dir"] == os.path.join(base_s, "config", "tools")
    assert config["data_models_dir"] == os.path.join(base_s, "data_models")
    assert config["prompts_dir"] == os.path.join(base_s, "config", "prompts")
    assert config["coding_prompts_dir"] == os.path.join(base_s, "config", "coding-prompts")
    assert config["coding_tools_dir"] == os.path.join(base_s, "config", "coding-tools")
    assert config["chat_tools_dir"] == os.path.join(base_s, "config", "chat_tools")
    assert config["prompt_templates_dir"] == os.path.join(base_s, "config", "prompt-templates")
    assert config["kb"]["dir"] == os.path.join(base_s, "config", "kb")


def test_explicit_path_overrides_preserved(tmp_path):
    """An explicit YAML override for a layout-dependent key wins over derivation.

    Back-compat: existing deployments that write fully-populated path values
    in config.yaml continue to use those values unchanged.
    """
    base = tmp_path / "foo"
    custom_kb = tmp_path / "my-kb"
    custom_prompts = tmp_path / "my-prompts"
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(
        f"base_dir: {base}\n"
        f"prompts_dir: {custom_prompts}\n"
        "kb:\n"
        f"  dir: {custom_kb}\n"
    )

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))

    # Explicit values preserved
    assert config["prompts_dir"] == str(custom_prompts)
    assert config["kb"]["dir"] == str(custom_kb)
    # Unset keys still derived from base_dir
    assert config["data_models_dir"] == os.path.join(str(base), "data_models")
    assert config["coding_prompts_dir"] == os.path.join(str(base), "config", "coding-prompts")


def test_derived_paths_respect_tilde_expansion(tmp_path, monkeypatch):
    """Tilde-expanded base_dir flows into derived path values."""
    monkeypatch.setenv("HOME", str(tmp_path))
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("base_dir: ~/mycarp\n")

    from carpenter.config import load_config

    config = load_config(yaml_path=str(yaml_file))

    expected_base = str(tmp_path / "mycarp")
    assert config["base_dir"] == expected_base
    assert config["log_dir"] == os.path.join(expected_base, "data", "logs")
    assert config["kb"]["dir"] == os.path.join(expected_base, "config", "kb")


def test_derived_paths_match_seed_manifest(tmp_path):
    """Derived defaults for seeded targets match SEED_MANIFEST.

    The seed installer and the config loader must agree on where seeded
    content lives; any drift would break first-install seeding.
    """
    base = tmp_path / "foo"
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(f"base_dir: {base}\n")

    from carpenter.config import load_config
    from carpenter.seed import SEED_MANIFEST

    config = load_config(yaml_path=str(yaml_file))

    name_to_config_key = {
        "prompts": "prompts_dir",
        "coding-prompts": "coding_prompts_dir",
        "data_models": "data_models_dir",
    }
    for entry in SEED_MANIFEST:
        cfg_key = name_to_config_key.get(entry.name)
        if not cfg_key:
            continue
        assert config[cfg_key] == os.path.join(str(base), entry.default_rel_target), (
            f"Derived {cfg_key} doesn't match SEED_MANIFEST target for {entry.name!r}"
        )
    # kb is stored under a nested dict
    kb_entry = next(e for e in SEED_MANIFEST if e.name == "kb")
    assert config["kb"]["dir"] == os.path.join(str(base), kb_entry.default_rel_target)
