"""Tests for the setup-credential CLI command and _update_dot_env helper."""

import sqlite3
import sys
from pathlib import Path

import pytest


# ── _update_dot_env helper ─────────────────────────────────────────


def test_update_dot_env_creates_new_file(tmp_path):
    """_update_dot_env creates the .env file if it does not exist."""
    from carpenter.__main__ import _update_dot_env

    dot_env = tmp_path / ".env"
    _update_dot_env(dot_env, "FORGEJO_TOKEN", "ghp_abc123")

    assert dot_env.is_file()
    assert "FORGEJO_TOKEN=ghp_abc123" in dot_env.read_text()


def test_update_dot_env_updates_existing_key(tmp_path):
    """Existing key in .env is replaced in place."""
    from carpenter.__main__ import _update_dot_env

    dot_env = tmp_path / ".env"
    dot_env.write_text("FORGEJO_TOKEN=old-value\n")

    result = _update_dot_env(dot_env, "FORGEJO_TOKEN", "new-value")
    assert result is True  # updated in place
    content = dot_env.read_text()
    assert "FORGEJO_TOKEN=new-value" in content
    assert "FORGEJO_TOKEN=old-value" not in content
    assert content.count("FORGEJO_TOKEN=") == 1


def test_update_dot_env_adds_new_key_to_existing_file(tmp_path):
    """New key is appended to an existing .env."""
    from carpenter.__main__ import _update_dot_env

    dot_env = tmp_path / ".env"
    dot_env.write_text("ANTHROPIC_API_KEY=sk-existing\n")

    result = _update_dot_env(dot_env, "FORGEJO_TOKEN", "tok-new")
    assert result is False  # newly added
    content = dot_env.read_text()
    assert "ANTHROPIC_API_KEY=sk-existing" in content
    assert "FORGEJO_TOKEN=tok-new" in content


def test_update_dot_env_returns_false_for_new_key(tmp_path):
    """Returns False when the key did not previously exist."""
    from carpenter.__main__ import _update_dot_env

    dot_env = tmp_path / ".env"
    result = _update_dot_env(dot_env, "NEW_KEY", "value")
    assert result is False


# ── setup-credential command ──────────────────────────────────────


def _run_setup_credential(argv, monkeypatch, tmp_path):
    """Invoke _cmd_setup_credential with patched CONFIG."""
    from carpenter import config as cfg_module
    from carpenter.__main__ import _cmd_setup_credential

    fake_config = {
        "base_dir": str(tmp_path),
        "database_path": str(tmp_path / "data" / "platform.db"),
    }
    monkeypatch.setattr(cfg_module, "CONFIG", fake_config)
    monkeypatch.setattr(cfg_module, "_cache", fake_config)
    _cmd_setup_credential(argv)


def test_setup_credential_writes_to_dot_env(tmp_path, monkeypatch):
    """setup-credential writes KEY=VALUE to {base_dir}/.env."""
    _run_setup_credential(
        ["--key", "FORGEJO_TOKEN", "--value", "ghp_xyz"],
        monkeypatch,
        tmp_path,
    )

    dot_env = tmp_path / ".env"
    assert dot_env.is_file()
    assert "FORGEJO_TOKEN=ghp_xyz" in dot_env.read_text()


def test_setup_credential_dot_env_chmod_600(tmp_path, monkeypatch):
    """setup-credential sets .env permissions to 0o600."""
    _run_setup_credential(
        ["--key", "FORGEJO_TOKEN", "--value", "ghp_xyz"],
        monkeypatch,
        tmp_path,
    )

    dot_env = tmp_path / ".env"
    assert oct(dot_env.stat().st_mode)[-3:] == "600"


def test_setup_credential_updates_existing_key(tmp_path, monkeypatch):
    """Existing key in .env is updated, not duplicated."""
    dot_env = tmp_path / ".env"
    dot_env.write_text("FORGEJO_TOKEN=old-value\n")

    _run_setup_credential(
        ["--key", "FORGEJO_TOKEN", "--value", "new-value"],
        monkeypatch,
        tmp_path,
    )

    content = dot_env.read_text()
    assert "FORGEJO_TOKEN=new-value" in content
    assert "FORGEJO_TOKEN=old-value" not in content
    assert content.count("FORGEJO_TOKEN=") == 1


def test_setup_credential_unknown_key_exits(tmp_path, monkeypatch):
    """Unknown credential key causes sys.exit(1)."""
    with pytest.raises(SystemExit) as exc_info:
        _run_setup_credential(
            ["--key", "MADE_UP_KEY", "--value", "x"],
            monkeypatch,
            tmp_path,
        )

    assert exc_info.value.code == 1


def test_setup_credential_enqueues_restart(tmp_path, monkeypatch):
    """setup-credential inserts a platform.restart work item into the DB."""
    # Set up a minimal database
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "platform.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE work_queue (
            id INTEGER PRIMARY KEY,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            max_retries INTEGER NOT NULL DEFAULT 3,
            retry_count INTEGER NOT NULL DEFAULT 0,
            claimed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

    _run_setup_credential(
        ["--key", "FORGEJO_TOKEN", "--value", "tok-123"],
        monkeypatch,
        tmp_path,
    )

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT event_type, payload_json FROM work_queue WHERE event_type='platform.restart'"
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    import json
    payload = json.loads(rows[0][1])
    assert payload["mode"] == "opportunistic"


def test_setup_credential_no_crash_if_db_missing(tmp_path, monkeypatch, capsys):
    """setup-credential doesn't crash if the DB doesn't exist yet."""
    _run_setup_credential(
        ["--key", "FORGEJO_TOKEN", "--value", "tok-abc"],
        monkeypatch,
        tmp_path,
    )
    # Should not raise; .env is still written
    dot_env = tmp_path / ".env"
    assert dot_env.is_file()
