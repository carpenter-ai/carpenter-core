"""Tests for the D24 stage 3a copy-on-install machinery."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter.packages import installer
from carpenter.packages.installer import (
    InstallError,
    compute_package_hash,
    ensure_installer_tables,
    install_package,
    list_install_records,
    list_blocking_arcs,
    uninstall_package,
    verify_install,
)


# ── DB fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def db_conn():
    """In-memory SQLite with installer tables ensured."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_installer_tables(conn)
    yield conn
    conn.close()


@pytest.fixture
def db_with_arcs(db_conn):
    """In-memory SQLite that also has a minimal ``arcs`` table.

    Mirrors the schema columns the installer's blocking-arc query
    inspects (``id``, ``status``, ``template_name``).  The real
    schema is much wider; we test only what list_blocking_arcs uses.
    """
    db_conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS arcs (
            id INTEGER PRIMARY KEY,
            template_name TEXT,
            status TEXT
        );
        """,
    )
    return db_conn


# ── source-package builders ─────────────────────────────────────────


def make_source_pkg(
    root: Path, name: str, *, manifest_yaml: str | None = None,
    extra_files: dict[str, str] | None = None,
) -> Path:
    """Materialize a minimal valid package directory under ``root``.

    Returns the package directory path.
    """
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    if manifest_yaml is None:
        manifest_yaml = dedent(f"""\
            name: {name}
            version: "0.1.0"
            description: Test package {name}.
        """)
    (pkg / "manifest.yaml").write_text(manifest_yaml)
    for rel, content in (extra_files or {}).items():
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return pkg


# ── compute_package_hash ────────────────────────────────────────────


class TestComputePackageHash:
    def test_hash_is_deterministic(self, tmp_path):
        a = make_source_pkg(tmp_path / "a-tree", "p", extra_files={"f.py": "x"})
        b = make_source_pkg(tmp_path / "b-tree", "p", extra_files={"f.py": "x"})
        assert compute_package_hash(a) == compute_package_hash(b)

    def test_hash_changes_when_content_changes(self, tmp_path):
        a = make_source_pkg(tmp_path / "a-tree", "p", extra_files={"f.py": "x"})
        b = make_source_pkg(tmp_path / "b-tree", "p", extra_files={"f.py": "y"})
        assert compute_package_hash(a) != compute_package_hash(b)

    def test_hash_changes_when_filename_changes(self, tmp_path):
        a = make_source_pkg(tmp_path / "a-tree", "p", extra_files={"f.py": "x"})
        b = make_source_pkg(tmp_path / "b-tree", "p", extra_files={"g.py": "x"})
        assert compute_package_hash(a) != compute_package_hash(b)

    def test_hash_ignores_pycache(self, tmp_path):
        a = make_source_pkg(tmp_path / "a-tree", "p", extra_files={"f.py": "x"})
        b = make_source_pkg(tmp_path / "b-tree", "p", extra_files={
            "f.py": "x",
            "__pycache__/f.cpython-312.pyc": "junk",
        })
        assert compute_package_hash(a) == compute_package_hash(b)

    def test_hash_ignores_pyc(self, tmp_path):
        a = make_source_pkg(tmp_path / "a-tree", "p", extra_files={"f.py": "x"})
        b = make_source_pkg(tmp_path / "b-tree", "p", extra_files={
            "f.py": "x",
            "stale.pyc": "junk",
        })
        assert compute_package_hash(a) == compute_package_hash(b)

    def test_symlink_hashes_target_text(self, tmp_path):
        a = make_source_pkg(tmp_path / "a-tree", "p", extra_files={"f.py": "x"})
        b = make_source_pkg(tmp_path / "b-tree", "p", extra_files={"f.py": "x"})
        # Replace b/f.py with a symlink pointing elsewhere.
        (b / "f.py").unlink()
        (b / "f.py").symlink_to("/etc/passwd")
        assert compute_package_hash(a) != compute_package_hash(b)


# ── install_package ─────────────────────────────────────────────────


class TestInstallPackage:
    def test_fresh_install(self, tmp_path, db_conn):
        src = make_source_pkg(tmp_path / "src", "p")
        dest = tmp_path / "installed" / "p"
        result = install_package(src, dest, conn=db_conn)
        assert result.was_update is False
        assert dest.is_dir()
        assert (dest / "manifest.yaml").is_file()
        record = installer.get_install_record(db_conn, "p")
        assert record is not None
        assert record["hash"] == result.hash
        assert record["version"] == "0.1.0"

    def test_update_install_replaces_atomically(self, tmp_path, db_conn):
        src = make_source_pkg(tmp_path / "src1", "p", extra_files={"f.py": "v1"})
        dest = tmp_path / "installed" / "p"
        r1 = install_package(src, dest, conn=db_conn)
        # Now update with new content.
        src2 = make_source_pkg(
            tmp_path / "src2", "p", extra_files={"f.py": "v2"},
        )
        r2 = install_package(src2, dest, conn=db_conn)
        assert r2.was_update is True
        assert r1.hash != r2.hash
        # The new content is on disk, the old is gone.
        assert (dest / "f.py").read_text() == "v2"
        # No staging or rotated dirs left behind.
        leftovers = [
            p for p in dest.parent.iterdir()
            if p.name.startswith("p.staging")
            or p.name.startswith("p.old-")
        ]
        assert leftovers == []

    def test_install_refuses_missing_manifest(self, tmp_path, db_conn):
        src = tmp_path / "no-manifest"
        src.mkdir()
        dest = tmp_path / "installed" / "x"
        with pytest.raises(InstallError, match="no manifest"):
            install_package(src, dest, conn=db_conn)

    def test_install_refuses_dest_name_mismatch(self, tmp_path, db_conn):
        src = make_source_pkg(tmp_path / "src", "p")
        dest = tmp_path / "installed" / "wrong-name"
        with pytest.raises(InstallError, match="dest_path basename"):
            install_package(src, dest, conn=db_conn)

    def test_install_records_arc_templates(self, tmp_path, db_conn):
        src = make_source_pkg(
            tmp_path / "src", "p",
            manifest_yaml=dedent("""\
                name: p
                version: "0.1.0"
                description: With templates.
                arc_templates:
                  - name: tri
                    path: templates/tri.yaml
            """),
            extra_files={"templates/tri.yaml": "x\n"},
        )
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_conn)
        rows = db_conn.execute(
            "SELECT package_name, template_name, kind FROM "
            "installed_packages_templates",
        ).fetchall()
        assert rows == [("p", "tri", "arc_template")]


# ── verify_install ──────────────────────────────────────────────────


class TestVerifyInstall:
    def test_verify_clean_install(self, tmp_path, db_conn):
        src = make_source_pkg(tmp_path / "src", "p", extra_files={"f.py": "x"})
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_conn)
        result = verify_install("p", conn=db_conn)
        assert result.ok is True

    def test_verify_detects_tamper(self, tmp_path, db_conn):
        src = make_source_pkg(tmp_path / "src", "p", extra_files={"f.py": "x"})
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_conn)
        # Tamper with the installed copy.
        (dest / "f.py").write_text("modified")
        result = verify_install("p", conn=db_conn)
        assert result.ok is False
        assert result.expected_hash is not None
        assert result.actual_hash is not None
        assert result.expected_hash != result.actual_hash

    def test_verify_no_record(self, db_conn):
        result = verify_install("nope", conn=db_conn)
        assert result.ok is False
        assert result.expected_hash is None

    def test_verify_missing_install_dir(self, tmp_path, db_conn):
        src = make_source_pkg(tmp_path / "src", "p")
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_conn)
        # Remove the install dir under the platform's nose.
        import shutil as _s
        _s.rmtree(dest)
        result = verify_install("p", conn=db_conn)
        assert result.ok is False
        assert "missing" in result.message


# ── uninstall_package ───────────────────────────────────────────────


class TestUninstall:
    def test_uninstall_removes_dir_and_record(self, tmp_path, db_with_arcs):
        src = make_source_pkg(tmp_path / "src", "p")
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_with_arcs)
        assert dest.is_dir()
        assert installer.get_install_record(db_with_arcs, "p") is not None
        result = uninstall_package("p", conn=db_with_arcs)
        assert result.name == "p"
        assert not dest.exists()
        assert installer.get_install_record(db_with_arcs, "p") is None

    def test_uninstall_missing_package(self, db_with_arcs):
        with pytest.raises(InstallError, match="not installed"):
            uninstall_package("nope", conn=db_with_arcs)

    def test_uninstall_blocked_by_live_arc(self, tmp_path, db_with_arcs):
        src = make_source_pkg(
            tmp_path / "src", "p",
            manifest_yaml=dedent("""\
                name: p
                version: "0.1"
                description: With templates.
                arc_templates:
                  - name: tri
                    path: templates/tri.yaml
            """),
            extra_files={"templates/tri.yaml": "x\n"},
        )
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_with_arcs)
        # Insert a live arc referencing the template.
        db_with_arcs.execute(
            "INSERT INTO arcs(id, template_name, status) VALUES (1, 'tri', 'running')",
        )
        db_with_arcs.commit()
        with pytest.raises(InstallError, match="non-terminal arc"):
            uninstall_package("p", conn=db_with_arcs)

    def test_uninstall_terminal_arcs_dont_block(
        self, tmp_path, db_with_arcs,
    ):
        src = make_source_pkg(
            tmp_path / "src", "p",
            manifest_yaml=dedent("""\
                name: p
                version: "0.1"
                description: With templates.
                arc_templates:
                  - name: tri
                    path: templates/tri.yaml
            """),
            extra_files={"templates/tri.yaml": "x\n"},
        )
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_with_arcs)
        db_with_arcs.execute(
            "INSERT INTO arcs(id, template_name, status) VALUES (1, 'tri', 'completed')",
        )
        db_with_arcs.commit()
        # Should succeed.
        uninstall_package("p", conn=db_with_arcs)
        assert installer.get_install_record(db_with_arcs, "p") is None

    def test_force_bypasses_block(self, tmp_path, db_with_arcs):
        src = make_source_pkg(
            tmp_path / "src", "p",
            manifest_yaml=dedent("""\
                name: p
                version: "0.1"
                description: With templates.
                arc_templates:
                  - name: tri
                    path: templates/tri.yaml
            """),
            extra_files={"templates/tri.yaml": "x\n"},
        )
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_with_arcs)
        db_with_arcs.execute(
            "INSERT INTO arcs(id, template_name, status) VALUES (1, 'tri', 'running')",
        )
        db_with_arcs.commit()
        uninstall_package("p", conn=db_with_arcs, force=True)
        assert installer.get_install_record(db_with_arcs, "p") is None

    def test_uninstall_archive_state_preserves_rows(self, tmp_path, db_with_arcs):
        """Phase 3a (D24): ``archive_state=True`` copies live package_state
        rows into ``package_state_archive`` before the FK cascade fires.
        """
        # Add the package_state tables to the minimal test DB.
        db_with_arcs.executescript("""
            CREATE TABLE IF NOT EXISTS package_state (
                package_name TEXT NOT NULL,
                key          TEXT NOT NULL,
                value_json   TEXT NOT NULL,
                version      INTEGER NOT NULL DEFAULT 1,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (package_name, key),
                FOREIGN KEY (package_name) REFERENCES installed_packages(name)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS package_state_archive (
                package_name TEXT NOT NULL,
                key          TEXT NOT NULL,
                value_json   TEXT NOT NULL,
                version      INTEGER NOT NULL,
                archived_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (package_name, key)
            );
        """)
        src = make_source_pkg(tmp_path / "src", "p")
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_with_arcs)
        # Seed state.
        from carpenter.packages import state as pkg_state
        pkg_state.set("p", "watermark", 999, conn=db_with_arcs)
        # Uninstall with archive_state=True.
        uninstall_package("p", conn=db_with_arcs, archive_state=True)
        # Live state is gone (archive_for_uninstall deleted it explicitly).
        assert pkg_state.get("p", "watermark", conn=db_with_arcs) is None
        # Archive survives.
        rows = db_with_arcs.execute(
            "SELECT key, value_json FROM package_state_archive "
            "WHERE package_name = ?", ("p",),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "watermark"
        assert "999" in rows[0][1]

    def test_uninstall_default_wipes_state_via_cascade(self, tmp_path, db_with_arcs):
        """Default ``archive_state=False`` wipes state via the FK cascade."""
        db_with_arcs.executescript("""
            CREATE TABLE IF NOT EXISTS package_state (
                package_name TEXT NOT NULL,
                key          TEXT NOT NULL,
                value_json   TEXT NOT NULL,
                version      INTEGER NOT NULL DEFAULT 1,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (package_name, key),
                FOREIGN KEY (package_name) REFERENCES installed_packages(name)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS package_state_archive (
                package_name TEXT NOT NULL,
                key          TEXT NOT NULL,
                value_json   TEXT NOT NULL,
                version      INTEGER NOT NULL,
                archived_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (package_name, key)
            );
        """)
        src = make_source_pkg(tmp_path / "src", "p")
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_with_arcs)
        from carpenter.packages import state as pkg_state
        pkg_state.set("p", "k", "v", conn=db_with_arcs)
        uninstall_package("p", conn=db_with_arcs)
        # FK cascade wiped the live row.
        assert pkg_state.get("p", "k", conn=db_with_arcs) is None
        # Nothing in the archive.
        rows = db_with_arcs.execute(
            "SELECT * FROM package_state_archive WHERE package_name = ?", ("p",),
        ).fetchall()
        assert rows == []
