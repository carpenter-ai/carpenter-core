"""Tests for the operator package-management CLI.

Exercises the ``python3 -m carpenter packages <subcommand>`` command
functions directly (via argv lists), against the real test DB connection
path (the ``test_db`` fixture points ``config.CONFIG['database_path']`` at
an isolated template-derived SQLite file, and ``base_dir`` at a tmp dir,
so installs land under ``<base_dir>/packages/<name>``).

Coverage:

* install a NO-capability package → succeeds without prompting.
* install a capability package non-interactively WITHOUT
  ``--accept-capabilities`` → exits nonzero, nothing granted.
* install a capability package WITH ``--accept-capabilities`` → grant
  recorded + capabilities echoed.
* ``list`` shows installed packages + their granted verbs.
* ``uninstall`` removes the install dir + DB row.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from carpenter import cli_packages
from carpenter.db import db_connection, db_transaction
from carpenter.packages import installer


@pytest.fixture(autouse=True)
def _installer_tables(test_db):
    """Ensure the installer tables exist on the test DB.

    The cached template DB is built with ``skip_migrations=True``, so the
    migration-created ``installed_packages`` / ``installed_packages_templates``
    tables are absent. The installer's ``ensure_installer_tables`` is the
    same idempotent DDL the migration runs, so we apply it here.
    """
    with db_transaction() as db:
        installer.ensure_installer_tables(db)


# ── source-package builders ─────────────────────────────────────────


def _write_plain_pkg(root: Path, name: str = "plainpkg") -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.yaml").write_text(dedent(f"""\
        name: {name}
        version: "0.1.0"
        description: A plain package with no platform capabilities.
    """))
    return pkg


def _write_cap_pkg(root: Path, name: str = "cappkg") -> Path:
    pkg = root / name
    (pkg / "handlers").mkdir(parents=True, exist_ok=True)
    (pkg / "handlers" / "__init__.py").write_text("")
    (pkg / "handlers" / "echo.py").write_text(
        "def handle_echo(params, ctx):\n"
        "    return {'echo': params}\n"
    )
    (pkg / "manifest.yaml").write_text(dedent(f"""\
        name: {name}
        version: "0.2.0"
        description: A package declaring a trusted egress capability.
        credential_requirements:
          - kind: env
            provider: demo
            env_key_prefix: DEMO_MAIL
            required_keys:
              - HOST
              - PORT
              - PASSWORD
        platform_capabilities:
          - verb: demo.echo
            kind: egress
            module: handlers.echo
            handler: handle_echo
            grant:
              protocol: demo
              host_from: HOST
              port: 1234
              credential_ref: DEMO_MAIL
    """))
    return pkg


@pytest.fixture
def src_root(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    root.mkdir()
    return root


# ── install: no-capability package ──────────────────────────────────


def test_install_plain_package_succeeds_without_prompt(
    test_db, src_root, monkeypatch, capsys,
):
    pkg = _write_plain_pkg(src_root)

    # If anything tried to prompt, this would blow up the test.
    monkeypatch.setattr("builtins.input", lambda *a, **k: pytest.fail(
        "install of a no-capability package must not prompt"))

    rc = cli_packages.cmd_packages(["install", "--path", str(pkg)])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Installed plainpkg v0.1.0" in out
    assert "Restart the server" in out

    with db_connection() as db:
        rec = installer.get_install_record(db, "plainpkg")
        assert rec is not None
        assert rec["version"] == "0.1.0"
        assert installer.granted_verbs_for_package(db, "plainpkg") == frozenset()


# ── install: capability package, non-interactive, no flag → fail ────


def test_install_capability_pkg_noninteractive_without_flag_fails(
    test_db, src_root, monkeypatch, capsys,
):
    pkg = _write_cap_pkg(src_root)

    # Force non-tty stdin.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)

    rc = cli_packages.cmd_packages(["install", "--path", str(pkg)])
    assert rc != 0

    err = capsys.readouterr().err
    assert "PLATFORM-LEVEL TRUST" in err
    assert "--accept-capabilities" in err

    # Nothing recorded — the dest must not have been installed.
    with db_connection() as db:
        assert installer.get_install_record(db, "cappkg") is None


# ── install: capability package, --accept-capabilities → granted ────


def test_install_capability_pkg_with_accept_flag_grants(
    test_db, src_root, monkeypatch, capsys,
):
    pkg = _write_cap_pkg(src_root)

    # Even on a non-tty, the explicit flag is sufficient consent.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)

    rc = cli_packages.cmd_packages(
        ["install", "--path", str(pkg), "--accept-capabilities"]
    )
    assert rc == 0

    captured = capsys.readouterr()
    # Echo of the grant on stderr, and the granted-capabilities report on stdout.
    assert "GRANTING PLATFORM-LEVEL TRUST" in captured.err
    assert "demo.echo" in captured.err
    assert "Granted platform capabilities" in captured.out
    assert "demo.echo" in captured.out

    with db_connection() as db:
        rec = installer.get_install_record(db, "cappkg")
        assert rec is not None
        assert installer.granted_verbs_for_package(db, "cappkg") == frozenset(
            {"demo.echo"}
        )


# ── list ────────────────────────────────────────────────────────────


def test_list_shows_installed_packages_and_verbs(
    test_db, src_root, monkeypatch, capsys,
):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    plain = _write_plain_pkg(src_root)
    cap = _write_cap_pkg(src_root)

    assert cli_packages.cmd_packages(["install", "--path", str(plain)]) == 0
    assert cli_packages.cmd_packages(
        ["install", "--path", str(cap), "--accept-capabilities"]
    ) == 0
    capsys.readouterr()  # drain install output

    rc = cli_packages.cmd_packages(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "plainpkg" in out
    assert "cappkg" in out
    assert "demo.echo" in out
    # The plain package has no capability verbs → dash placeholder.
    assert "-" in out


def test_list_empty(test_db, capsys):
    rc = cli_packages.cmd_packages(["list"])
    assert rc == 0
    assert "No capability packages installed." in capsys.readouterr().out


# ── uninstall ───────────────────────────────────────────────────────


def test_uninstall_removes_dir_and_db_row(
    test_db, src_root, monkeypatch, capsys,
):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    cap = _write_cap_pkg(src_root)
    assert cli_packages.cmd_packages(
        ["install", "--path", str(cap), "--accept-capabilities"]
    ) == 0
    capsys.readouterr()

    # Capture the install dir before uninstall.
    with db_connection() as db:
        rec = installer.get_install_record(db, "cappkg")
        assert rec is not None
        install_dir = Path(rec["install_path"])
    assert install_dir.is_dir()

    rc = cli_packages.cmd_packages(["uninstall", "cappkg"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Uninstalled cappkg" in out
    assert "demo.echo" in out  # dropped capability grant echoed

    assert not install_dir.exists()
    with db_connection() as db:
        assert installer.get_install_record(db, "cappkg") is None


def test_uninstall_not_installed_errors(test_db, capsys):
    rc = cli_packages.cmd_packages(["uninstall", "does_not_exist"])
    assert rc != 0
    assert "is not installed" in capsys.readouterr().err


# ── dispatcher edge cases ───────────────────────────────────────────


def test_unknown_subcommand_errors(capsys):
    rc = cli_packages.cmd_packages(["frobnicate"])
    assert rc == 2
    assert "unknown packages subcommand" in capsys.readouterr().err


def test_install_requires_exactly_one_source(test_db, capsys):
    rc = cli_packages.cmd_packages(["install"])
    assert rc == 2
    assert "exactly one" in capsys.readouterr().err
