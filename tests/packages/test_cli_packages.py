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
* ``verify`` re-hashes the installed tree, one package or all of them,
  and exits nonzero when one does not match its install record — plus
  the guard that every subcommand the platform's messages tell an
  operator to run actually exists.
"""

from __future__ import annotations

import re
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


# ── source resolution ───────────────────────────────────────────────


class TestSourceResolution:
    def test_bare_name_uses_the_legacy_local_layout(
        self, test_db, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert cli_packages._source_dir_for("demo") == (
            tmp_path / "repos" / "carpenter-packages" / "packages" / "demo"
        )

    def test_install_by_bare_name_lands_under_base_dir(
        self, test_db, monkeypatch, tmp_path, capsys,
    ):
        from carpenter import config

        home = tmp_path / "home"
        src_tree = home / "repos" / "carpenter-packages" / "packages"
        src_tree.mkdir(parents=True)
        _write_plain_pkg(src_tree)
        monkeypatch.setenv("HOME", str(home))

        rc = cli_packages.cmd_packages(["install", "plainpkg"])
        assert rc == 0
        installed = (
            Path(config.CONFIG["base_dir"]) / "packages" / "plainpkg"
        )
        assert (installed / "manifest.yaml").is_file()

    def test_a_bare_name_may_now_resolve_to_a_remote_source(
        self, test_db, monkeypatch, capsys,
    ):
        """A configured remote source is FETCHED, not refused for being one.

        The stopping point here is the egress gate rather than a
        CLI-level "remote is not supported": the host resolves to a
        private address through the guard's own resolver seam, so the
        refusal arrives without any network contact, and it names the
        host and the config key that would allow it.
        """
        from carpenter import config
        from carpenter.security import egress

        monkeypatch.setattr(
            egress, "_resolve_host", lambda host, port: ["10.0.0.7"],
        )
        monkeypatch.setitem(config.CONFIG, "web_egress_allowlist", [])
        monkeypatch.setitem(config.CONFIG, "package_sources", [{
            "name": "house",
            "url": "git+https://forge.example/org/pkgs.git",
            "subdir_template": "packages/{name}",
        }])
        rc = cli_packages.cmd_packages(["install", "plainpkg"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "forge.example" in err
        assert "web_egress_allowlist" in err

    def test_source_dir_for_still_refuses_a_remote_spec(
        self, test_db, monkeypatch,
    ):
        """The local-only helper has no directory to give for a remote."""
        from carpenter import config
        from carpenter.packages.sources import SourceSpecError

        monkeypatch.setitem(config.CONFIG, "package_sources", [{
            "name": "house",
            "url": "git+https://forge.example/org/pkgs.git",
        }])
        with pytest.raises(SourceSpecError) as exc:
            cli_packages._source_dir_for("plainpkg")
        assert "remote source" in str(exc.value)

    def test_malformed_source_spec_is_refused(self, test_db, capsys):
        rc = cli_packages.cmd_packages(["install", "Not A Name"])
        assert rc == 1
        assert "accepted source forms" in capsys.readouterr().err

    def test_install_records_local_provenance(
        self, test_db, src_root, capsys,
    ):
        pkg = _write_plain_pkg(src_root)
        assert cli_packages.cmd_packages(["install", "--path", str(pkg)]) == 0
        with db_connection() as db:
            rec = installer.get_install_record(db, "plainpkg")
        assert rec["source_kind"] == "local"
        assert rec["source_url"] == ""
        assert rec["source_resolved"] == ""
        assert rec["source_id"]
        assert rec["source_path"] == str(pkg.resolve())


# ── verify ──────────────────────────────────────────────────────────


class TestVerify:
    """``packages verify`` — the check two recovery messages name."""

    def test_verify_all_reports_each_installed_package(
        self, test_db, src_root, monkeypatch, capsys,
    ):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
        assert cli_packages.cmd_packages(
            ["install", "--path", str(_write_plain_pkg(src_root))],
        ) == 0
        assert cli_packages.cmd_packages([
            "install", "--path", str(_write_cap_pkg(src_root)),
            "--accept-capabilities",
        ]) == 0
        capsys.readouterr()

        rc = cli_packages.cmd_packages(["verify"])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert "NAME" in out and "STATUS" in out
        assert re.search(r"plainpkg\s+OK", out)
        assert re.search(r"cappkg\s+OK", out)
        assert "2 package(s) verified." in out

    def test_a_tampered_tree_fails_and_exits_nonzero(
        self, test_db, src_root, monkeypatch, capsys,
    ):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
        assert cli_packages.cmd_packages(
            ["install", "--path", str(_write_plain_pkg(src_root))],
        ) == 0
        with db_connection() as db:
            installed = Path(
                installer.get_install_record(db, "plainpkg")["install_path"],
            )
        (installed / "sneaked_in.py").write_text("print('not mine')\n")
        capsys.readouterr()

        rc = cli_packages.cmd_packages(["verify"])

        out = capsys.readouterr().out
        assert rc == 1
        assert re.search(r"plainpkg\s+FAILED", out)
        assert "hash mismatch" in out
        assert "will NOT load them" in out
        # Read-only: the row is left exactly as it was for the operator.
        with db_connection() as db:
            assert installer.get_install_record(db, "plainpkg") is not None

    def test_one_package_by_name(
        self, test_db, src_root, monkeypatch, capsys,
    ):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
        assert cli_packages.cmd_packages(
            ["install", "--path", str(_write_plain_pkg(src_root))],
        ) == 0
        assert cli_packages.cmd_packages(
            ["install", "--path", str(_write_plain_pkg(src_root, "second"))],
        ) == 0
        capsys.readouterr()

        rc = cli_packages.cmd_packages(["verify", "second"])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert "second" in out
        assert "plainpkg" not in out
        assert "1 package(s) verified." in out

    def test_an_uninstalled_name_is_a_failure(
        self, test_db, capsys,
    ):
        rc = cli_packages.cmd_packages(["verify", "absent"])

        out = capsys.readouterr().out
        assert rc == 1
        assert "no install record" in out

    def test_nothing_installed_is_not_a_failure(self, test_db, capsys):
        rc = cli_packages.cmd_packages(["verify"])

        assert rc == 0
        assert "No capability packages installed." in capsys.readouterr().out

    def test_every_subcommand_the_platform_names_exists(self):
        """The bug this subcommand closed, as a standing guard.

        Two recovery messages told an operator to run `packages verify`
        while the dispatcher knew only install/update/uninstall/list, so
        the advice given at the worst possible moment produced "unknown
        packages subcommand".  Any message that names a subcommand must
        name one that exists.
        """
        pattern = re.compile(r"carpenter packages ([a-z][a-z-]*)")
        carpenter_root = Path(cli_packages.__file__).resolve().parent
        roots = [carpenter_root, carpenter_root.parent / "config_seed"]
        found: dict[str, set[str]] = {}
        for root in roots:
            for path in sorted(root.rglob("*.py")):
                # Flatten the source so a message split across adjacent
                # string literals still reads as one sentence.
                text = path.read_text()
                for prefix in ('f"', "f'"):
                    text = text.replace(prefix, prefix[1])
                flat = re.sub(
                    r"\s+", " ", text.replace('"', " ").replace("'", " "),
                )
                for name in pattern.findall(flat):
                    found.setdefault(name, set()).add(path.name)

        assert "verify" in found, "the messages under test moved or changed"
        unknown = {
            name: sorted(where) for name, where in found.items()
            if name not in cli_packages._SUBCOMMANDS
        }
        assert unknown == {}, (
            f"these messages name a packages subcommand that does not "
            f"exist: {unknown}"
        )
