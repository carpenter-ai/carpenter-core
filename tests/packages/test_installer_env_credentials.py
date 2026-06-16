"""Tests for install-time handling of ``kind: env`` package credentials.

PR #32 added the ``kind: env`` manifest schema (``EnvCredentialRef``)
but did no install-time handling or runtime delivery.  This follow-up
wires that in:

* At install, for each declared env-credential ref, a one-time
  credential request is created for every ``{prefix}_{suffix}`` env var
  that is not already set, surfaced in ``InstallResult`` and via a log
  line.  Pre-set keys are NOT re-requested.  Install never hard-fails on
  missing env creds (mirrors the OAuth posture).
* The delivery gap is closed by mirroring ``.env`` writes into
  ``os.environ`` (tested in ``tests/test_setup_credential.py``); here we
  assert the request-creation half end-to-end through ``install_package``.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter.api import credentials as credentials_api
from carpenter.packages import installer
from carpenter.packages.installer import (
    ensure_installer_tables,
    install_package,
)


_ENV_MANIFEST = """\
    name: envpkg
    version: "0.1.0"
    description: A package needing env-var credentials.
    credential_requirements:
      - kind: env
        provider: imap_smtp
        env_key_prefix: IMAP_EMAIL
        required_keys:
          - IMAP_HOST
          - IMAP_PORT
          - USERNAME
          - PASSWORD
"""


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_installer_tables(conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def clean_credential_requests():
    """Isolate the process-wide credential-request registry per test."""
    credentials_api.clear_credential_requests()
    yield
    credentials_api.clear_credential_requests()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure the env-credential keys are unset unless a test sets them."""
    for suffix in ("IMAP_HOST", "IMAP_PORT", "USERNAME", "PASSWORD"):
        monkeypatch.delenv(f"IMAP_EMAIL_{suffix}", raising=False)
    yield


def _make_pkg(root: Path, manifest_yaml: str = _ENV_MANIFEST) -> Path:
    pkg = root / "envpkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.yaml").write_text(dedent(manifest_yaml))
    return pkg


def test_install_creates_requests_for_each_unset_key(tmp_path, db_conn):
    src = _make_pkg(tmp_path / "src")
    dest = tmp_path / "installed" / "envpkg"

    result = install_package(src, dest, conn=db_conn)

    keys = {r["key"] for r in result.env_credential_requests}
    assert keys == {
        "IMAP_EMAIL_IMAP_HOST",
        "IMAP_EMAIL_IMAP_PORT",
        "IMAP_EMAIL_USERNAME",
        "IMAP_EMAIL_PASSWORD",
    }
    # Each surfaced request carries provider + a resolvable one-time link.
    for req in result.env_credential_requests:
        assert req["provider"] == "imap_smtp"
        assert req["url"].startswith("/api/credentials/")
        # The request must actually exist in the intake registry.
        stored = credentials_api.get_credential_request(req["request_id"])
        assert stored is not None
        assert stored["key"] == req["key"]
        assert stored["fulfilled"] is False
        assert "envpkg" in stored["description"]
        assert "imap_smtp" in stored["description"]


def test_preset_env_var_is_not_rerequested(tmp_path, db_conn, monkeypatch):
    # One of the four keys is already provided via the live environment.
    monkeypatch.setenv("IMAP_EMAIL_USERNAME", "alice@example.com")

    src = _make_pkg(tmp_path / "src")
    dest = tmp_path / "installed" / "envpkg"

    result = install_package(src, dest, conn=db_conn)

    keys = {r["key"] for r in result.env_credential_requests}
    # The pre-set USERNAME is not re-requested; the other three are.
    assert "IMAP_EMAIL_USERNAME" not in keys
    assert keys == {
        "IMAP_EMAIL_IMAP_HOST",
        "IMAP_EMAIL_IMAP_PORT",
        "IMAP_EMAIL_PASSWORD",
    }


def test_preset_via_config_is_not_rerequested(
    tmp_path, db_conn, monkeypatch,
):
    """A value present in loaded config (lower-cased alias) counts as set."""
    from carpenter import config

    monkeypatch.setitem(config.CONFIG, "imap_email_password", "hunter2")
    try:
        src = _make_pkg(tmp_path / "src")
        dest = tmp_path / "installed" / "envpkg"
        result = install_package(src, dest, conn=db_conn)
        keys = {r["key"] for r in result.env_credential_requests}
        assert "IMAP_EMAIL_PASSWORD" not in keys
    finally:
        config.CONFIG.pop("imap_email_password", None)


def test_all_preset_creates_no_requests(tmp_path, db_conn, monkeypatch):
    for suffix in ("IMAP_HOST", "IMAP_PORT", "USERNAME", "PASSWORD"):
        monkeypatch.setenv(f"IMAP_EMAIL_{suffix}", "x")

    src = _make_pkg(tmp_path / "src")
    dest = tmp_path / "installed" / "envpkg"
    result = install_package(src, dest, conn=db_conn)
    assert result.env_credential_requests == ()


def test_install_does_not_hard_fail_on_missing_env_creds(tmp_path, db_conn):
    """Missing env creds are reported, not fatal (mirrors OAuth posture)."""
    src = _make_pkg(tmp_path / "src")
    dest = tmp_path / "installed" / "envpkg"
    # Should not raise even though no credential values are available.
    result = install_package(src, dest, conn=db_conn)
    assert dest.is_dir()
    assert installer.get_install_record(db_conn, "envpkg") is not None
    assert len(result.env_credential_requests) == 4


def test_package_without_env_creds_has_no_requests(tmp_path, db_conn):
    src = tmp_path / "src" / "plain"
    src.mkdir(parents=True)
    (src / "manifest.yaml").write_text(dedent("""\
        name: plain
        version: "0.1.0"
        description: No credentials here.
    """))
    dest = tmp_path / "installed" / "plain"
    result = install_package(src, dest, conn=db_conn)
    assert result.env_credential_requests == ()


def test_oauth_cred_is_not_requested_as_env(tmp_path, db_conn):
    """OAuth refs are left to the OAuth flow; install creates no env req."""
    src = tmp_path / "src" / "oauthpkg"
    src.mkdir(parents=True)
    (src / "manifest.yaml").write_text(dedent("""\
        name: oauthpkg
        version: "0.1.0"
        description: OAuth-only package.
        credential_requirements:
          - kind: oauth
            provider: google
            env_key_prefix: GMAIL_OAUTH
            authorize_url: https://accounts.google.com/o/oauth2/v2/auth
            token_url: https://oauth2.googleapis.com/token
            scopes:
              - https://www.googleapis.com/auth/gmail.readonly
    """))
    dest = tmp_path / "installed" / "oauthpkg"
    result = install_package(src, dest, conn=db_conn)
    assert result.env_credential_requests == ()
