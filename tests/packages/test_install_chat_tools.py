"""Tests for the install_package / uninstall_package chat tools.

These tests exercise the seed chat-tool definitions directly, both
to confirm their metadata declares the right trust posture and to
smoke-test the happy/sad paths via mocked DB + filesystem.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter.packages.installer import (
    ensure_installer_tables,
    get_install_record,
)


def _import_packages_chat_tools():
    seed = (
        Path(__file__).resolve().parents[2]
        / "config_seed" / "chat_tools" / "packages.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_packages_chat_tools", str(seed),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_source(root: Path, name: str) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.yaml").write_text(dedent(f"""\
        name: {name}
        version: "0.1.0"
        description: Test package.
    """))
    return pkg


@pytest.fixture
def fake_carpenter_dirs(tmp_path, monkeypatch):
    """Redirect ~/repos/carpenter-packages and ~/carpenter to tmp_path.

    The chat tools resolve sources / dests via ``os.path.expanduser``,
    so we monkeypatch HOME for the duration of the test.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "repos" / "carpenter-packages" / "packages").mkdir(parents=True)
    (home / "carpenter" / "packages").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def fake_db(monkeypatch):
    """Provide an in-memory DB and patch ``carpenter.db.db_transaction``."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_installer_tables(conn)
    # Provide a minimal arcs table for blocking-arc lookups.
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS arcs("
        " id INTEGER PRIMARY KEY, template_name TEXT, status TEXT);"
    )

    class _Ctx:
        def __init__(self, c):
            self._c = c

        def __enter__(self):
            return self._c

        def __exit__(self, *a):
            return False

    import carpenter.db as carpenter_db
    monkeypatch.setattr(carpenter_db, "db_transaction", lambda: _Ctx(conn))
    yield conn
    conn.close()


# ── metadata ────────────────────────────────────────────────────────


class TestMetadata:
    def test_install_package_metadata(self):
        mod = _import_packages_chat_tools()
        meta = mod.install_package._chat_tool_meta
        assert meta["trust_boundary"] == "platform"
        assert meta["requires_user_confirm"] is True
        assert "filesystem_write" in meta["capabilities"]
        assert "database_write" in meta["capabilities"]
        assert meta["always_available"] is False

    def test_uninstall_package_metadata(self):
        mod = _import_packages_chat_tools()
        meta = mod.uninstall_package._chat_tool_meta
        assert meta["trust_boundary"] == "platform"
        assert meta["requires_user_confirm"] is True
        assert "filesystem_write" in meta["capabilities"]
        assert "database_write" in meta["capabilities"]


# ── install_package ─────────────────────────────────────────────────


class TestInstallChatTool:
    def test_install_happy_path(self, fake_carpenter_dirs, fake_db):
        mod = _import_packages_chat_tools()
        src = _make_source(
            fake_carpenter_dirs / "repos" / "carpenter-packages"
            / "packages",
            "p",
        )
        out = mod.install_package({"source_name": "p"})
        assert "Installed" in out
        assert "p v0.1.0" in out
        # Hash row was recorded.
        record = get_install_record(fake_db, "p")
        assert record is not None
        # Install dir was materialized.
        installed = (
            fake_carpenter_dirs / "carpenter" / "packages" / "p"
        )
        assert (installed / "manifest.yaml").is_file()

    def test_install_missing_source(self, fake_carpenter_dirs, fake_db):
        mod = _import_packages_chat_tools()
        out = mod.install_package({"source_name": "nope"})
        assert "Error" in out
        assert "not found" in out

    def test_install_invalid_source_name(
        self, fake_carpenter_dirs, fake_db,
    ):
        mod = _import_packages_chat_tools()
        out = mod.install_package({})
        assert "Error" in out
        assert "source_name" in out


# ── uninstall_package ───────────────────────────────────────────────


class TestUninstallChatTool:
    def test_uninstall_not_installed(self, fake_carpenter_dirs, fake_db):
        mod = _import_packages_chat_tools()
        out = mod.uninstall_package({"name": "nope"})
        assert "not installed" in out

    def test_uninstall_happy_path(self, fake_carpenter_dirs, fake_db):
        mod = _import_packages_chat_tools()
        src = _make_source(
            fake_carpenter_dirs / "repos" / "carpenter-packages"
            / "packages",
            "p",
        )
        # Install first.
        out_in = mod.install_package({"source_name": "p"})
        assert "Installed" in out_in
        # Then uninstall.
        out_un = mod.uninstall_package({"name": "p"})
        assert "Uninstalled p" in out_un
        assert get_install_record(fake_db, "p") is None

    def test_uninstall_blocked_by_live_arc(
        self, fake_carpenter_dirs, fake_db,
    ):
        mod = _import_packages_chat_tools()
        # Build a source with a template.
        pkg_root = (
            fake_carpenter_dirs / "repos" / "carpenter-packages"
            / "packages" / "p"
        )
        pkg_root.mkdir(parents=True)
        (pkg_root / "manifest.yaml").write_text(dedent("""\
            name: p
            version: "0.1.0"
            description: With templates.
            arc_templates:
              - name: tri
                path: templates/tri.yaml
        """))
        (pkg_root / "templates").mkdir()
        (pkg_root / "templates" / "tri.yaml").write_text("x\n")
        out_in = mod.install_package({"source_name": "p"})
        assert "Installed" in out_in
        # Add a live arc referencing the template.
        fake_db.execute(
            "INSERT INTO arcs(id, template_name, status) VALUES (1, 'tri', 'running')",
        )
        fake_db.commit()
        out = mod.uninstall_package({"name": "p"})
        assert "Cannot uninstall" in out
        assert "tri" in out
