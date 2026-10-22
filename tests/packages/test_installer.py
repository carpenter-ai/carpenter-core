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

    def test_hash_ignores_subscriptions_json(self, tmp_path):
        # The subscription pipeline writes _subscriptions.json beside an
        # installed package; it is generated runtime state, not shipped
        # content, so it must not change the SD6 integrity hash (else the
        # package is refused on the load after its first subscription run).
        a = make_source_pkg(tmp_path / "a-tree", "p", extra_files={"f.py": "x"})
        b = make_source_pkg(tmp_path / "b-tree", "p", extra_files={
            "f.py": "x",
            "_subscriptions.json": '[{"event": "email.received"}]',
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


# ── uninstall reclaims the archive cache ────────────────────────────


class TestUninstallReclaimsArchiveCache:
    """``uninstall_package`` used to leave the pristine archives behind.

    Install writes ``cache/package-archives/<name>/<source_id>/
    <version>.tar.gz`` as the reconcile anchor.  Uninstall never removed
    it, so a full copy of the tree of every version of every package that
    was no longer installed accumulated forever.
    """

    def _extra_archive(self, tmp_path, name, version, *, source_id=None,
                       marker="x"):
        """Add one more cached archive for name@version by hand."""
        from carpenter.packages import archive_cache
        src = tmp_path / f"extra-{name}-{version}-{source_id}-{marker}"
        src.mkdir(parents=True)
        (src / "manifest.yaml").write_text(
            f"name: {name}\nversion: {version}\n",
        )
        (src / "body.py").write_text(f"# {marker}\n")
        return archive_cache.store_archive(
            name, version, src, source_id=source_id,
        )

    def _cache_root(self):
        from carpenter.packages import archive_cache
        return archive_cache.cache_dir()

    def test_install_caches_then_uninstall_reclaims(
        self, tmp_path, db_with_arcs,
    ):
        src = make_source_pkg(tmp_path / "src", "p")
        install_package(src, tmp_path / "installed" / "p", conn=db_with_arcs)
        cached = sorted(self._cache_root().rglob("*.tar.gz"))
        assert len(cached) == 1, "install should have cached one archive"

        result = uninstall_package("p", conn=db_with_arcs)

        assert result.reclaimed_archives == tuple(cached)
        assert not cached[0].exists()
        assert not (self._cache_root() / "p").exists()
        assert sorted(self._cache_root().rglob("*.tar.gz")) == []

    def test_reclaims_every_version_source_and_superseded_copy(
        self, tmp_path, db_with_arcs,
    ):
        src = make_source_pkg(tmp_path / "src", "p")
        install_package(src, tmp_path / "installed" / "p", conn=db_with_arcs)
        # Several versions, several source_ids, a superseded copy (same
        # version string re-published with different bytes), and the
        # legacy source-blind layout.
        self._extra_archive(tmp_path, "p", "0.2.0", source_id="aaa")
        self._extra_archive(tmp_path, "p", "0.2.0", source_id="bbb")
        self._extra_archive(
            tmp_path, "p", "0.2.0", source_id="aaa", marker="republished",
        )
        self._extra_archive(tmp_path, "p", "0.3.0")  # legacy flat layout
        before = sorted((self._cache_root() / "p").rglob("*.tar.gz"))
        # install's 0.1.0 + 0.2.0/aaa + its superseded copy + 0.2.0/bbb
        # + the legacy flat 0.3.0.
        assert len(before) == 5, before
        assert any(".superseded-" in f.name for f in before)
        assert (self._cache_root() / "p" / "0.3.0.tar.gz").is_file()

        result = uninstall_package("p", conn=db_with_arcs)

        assert set(result.reclaimed_archives) == set(before)
        assert not (self._cache_root() / "p").exists()

    def test_another_packages_cache_is_untouched(
        self, tmp_path, db_with_arcs,
    ):
        for name in ("p", "q"):
            src = make_source_pkg(tmp_path / f"src-{name}", name)
            install_package(
                src, tmp_path / "installed" / name, conn=db_with_arcs,
            )
        q_cached = sorted((self._cache_root() / "q").rglob("*.tar.gz"))
        assert len(q_cached) == 1

        uninstall_package("p", conn=db_with_arcs)

        assert not (self._cache_root() / "p").exists()
        assert q_cached[0].is_file()
        assert installer.get_install_record(db_with_arcs, "q") is not None

    def test_second_uninstall_of_a_reinstalled_package_is_clean(
        self, tmp_path, db_with_arcs,
    ):
        """Idempotent: nothing cached to reclaim is not an error."""
        src = make_source_pkg(tmp_path / "src", "p")
        install_package(src, tmp_path / "installed" / "p", conn=db_with_arcs)
        uninstall_package("p", conn=db_with_arcs)
        # Re-install, then delete the cache under the platform's nose so
        # the second uninstall finds nothing.
        install_package(src, tmp_path / "installed" / "p", conn=db_with_arcs)
        import shutil as _s
        _s.rmtree(self._cache_root() / "p")

        result = uninstall_package("p", conn=db_with_arcs)

        assert result.reclaimed_archives == ()
        assert installer.get_install_record(db_with_arcs, "p") is None

    def test_preserve_state_still_reclaims_the_archives(
        self, tmp_path, db_with_arcs,
    ):
        """``archive_state=True`` keeps mutable state, not derived archives.

        The flag exists so a re-install can pick up the package's own
        state; the pristine archive is a derived artifact a re-fetch
        rebuilds, and with the install record gone there is no
        ``installed_packages.hash`` left to verify a kept one against.
        """
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
        install_package(src, tmp_path / "installed" / "p", conn=db_with_arcs)
        from carpenter.packages import state as pkg_state
        pkg_state.set("p", "watermark", 999, conn=db_with_arcs)
        cached = sorted((self._cache_root() / "p").rglob("*.tar.gz"))
        assert len(cached) == 1

        result = uninstall_package(
            "p", conn=db_with_arcs, archive_state=True,
        )

        # State preserved...
        rows = db_with_arcs.execute(
            "SELECT key FROM package_state_archive WHERE package_name = ?",
            ("p",),
        ).fetchall()
        assert [r[0] for r in rows] == ["watermark"]
        # ...derived archives reclaimed anyway.
        assert result.reclaimed_archives == tuple(cached)
        assert not (self._cache_root() / "p").exists()

    def test_reclaim_failure_is_logged_and_the_uninstall_succeeds(
        self, tmp_path, db_with_arcs, monkeypatch, caplog,
    ):
        """A stuck cache file must not leave the package installed."""
        from carpenter.packages import archive_cache
        src = make_source_pkg(tmp_path / "src", "p")
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_with_arcs)
        cached = sorted((self._cache_root() / "p").rglob("*.tar.gz"))
        assert len(cached) == 1

        # Fail only inside the cache: ``archive_cache.shutil`` is the one
        # shared module object, so a blanket patch would also break the
        # install-dir removal this test needs to succeed.
        real_rmtree = archive_cache.shutil.rmtree
        cache_root = str(self._cache_root())

        def boom(path, *a, **k):
            if str(path).startswith(cache_root):
                raise OSError(13, "Permission denied")
            return real_rmtree(path, *a, **k)

        monkeypatch.setattr(archive_cache.shutil, "rmtree", boom)
        with caplog.at_level("WARNING"):
            result = uninstall_package("p", conn=db_with_arcs)

        # The uninstall still happened in full.
        assert result.name == "p"
        assert result.reclaimed_archives == ()
        assert not dest.exists()
        assert installer.get_install_record(db_with_arcs, "p") is None
        # ...and the failure was reported, not swallowed.
        assert any(
            "reclaim" in rec.message.lower() for rec in caplog.records
        ), caplog.text
        # The archive is genuinely still there (nothing pretended).
        assert cached[0].is_file()

    def test_crafted_package_name_cannot_delete_outside_the_cache(
        self, tmp_path, db_with_arcs,
    ):
        """The guard refuses a name that is not one component of the root."""
        from carpenter.packages import archive_cache
        outside = self._cache_root().parent / "package-listings"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "src-aaa.json").write_text("{}\n")

        with pytest.raises(archive_cache.ArchiveCacheError):
            archive_cache.reclaim_package_archives("../package-listings")

        assert (outside / "src-aaa.json").is_file()


# ── source provenance ───────────────────────────────────────────────


_PROVENANCE_KEYS = (
    "source_kind", "source_url", "source_ref", "source_resolved",
    "source_subdir", "source_id", "fetched_at",
)


class TestProvenanceColumns:
    """The ``source_*`` / ``fetched_at`` columns on ``installed_packages``.

    These record WHERE the installed bytes came from.  ``source_path``
    keeps its original meaning (the local directory they were copied
    from) — the test below pins that, because changing it would silently
    change what the reconcile path re-records.
    """

    def test_remote_provenance_is_recorded(self, tmp_path, db_conn):
        from carpenter.packages import sources

        src = make_source_pkg(tmp_path / "src", "p")
        spec = sources.parse_source(
            "git+https://forge.example/org/pkgs.git@v1.0#subdir=packages/p",
        )
        install_package(
            src, tmp_path / "installed" / "p", conn=db_conn,
            provenance=spec, resolved="c" * 40,
        )
        rec = installer.get_install_record(db_conn, "p")
        assert rec["source_kind"] == "git"
        assert rec["source_url"] == "https://forge.example/org/pkgs.git"
        assert rec["source_ref"] == "v1.0"
        assert rec["source_resolved"] == "c" * 40
        assert rec["source_subdir"] == "packages/p"
        assert rec["source_id"] == sources.source_id(spec)
        assert rec["fetched_at"] == rec["installed_at"]

    def test_local_provenance_records_an_empty_resolved(
        self, tmp_path, db_conn,
    ):
        from carpenter.packages import sources

        src = make_source_pkg(tmp_path / "src", "p")
        install_package(
            src, tmp_path / "installed" / "p", conn=db_conn,
            provenance=sources.parse_source(str(src)),
        )
        rec = installer.get_install_record(db_conn, "p")
        assert rec["source_kind"] == "local"
        assert rec["source_url"] == ""
        assert rec["source_ref"] == ""
        assert rec["source_resolved"] == ""
        assert rec["source_subdir"] == ""
        assert rec["source_id"]
        assert rec["fetched_at"]

    def test_omitting_provenance_records_nothing(self, tmp_path, db_conn):
        src = make_source_pkg(tmp_path / "src", "p")
        install_package(src, tmp_path / "installed" / "p", conn=db_conn)
        rec = installer.get_install_record(db_conn, "p")
        assert [rec[k] for k in _PROVENANCE_KEYS] == [None] * 7

    def test_credential_is_never_persisted(self, tmp_path, db_conn):
        from carpenter.packages import sources

        src = make_source_pkg(tmp_path / "src", "p")
        spec = sources.parse_source(
            "https://files.example/dl/p-1.0.tar.gz?token=secrettoken",
        )
        install_package(
            src, tmp_path / "installed" / "p", conn=db_conn,
            provenance=spec, resolved="a" * 64,
        )
        row = " ".join(
            str(v) for v in db_conn.execute(
                "SELECT * FROM installed_packages WHERE name = 'p'",
            ).fetchone()
        )
        assert "secrettoken" not in row

    def test_userinfo_credential_is_redacted_not_stored(
        self, tmp_path, db_conn,
    ):
        from carpenter.packages import sources

        src = make_source_pkg(tmp_path / "src", "p")
        spec = sources.parse_source(
            "git+https://bot:secrettoken@forge.example/org/p.git",
        )
        install_package(
            src, tmp_path / "installed" / "p", conn=db_conn,
            provenance=spec, resolved="c" * 40,
        )
        rec = installer.get_install_record(db_conn, "p")
        assert rec["source_url"] == "https://forge.example/org/p.git"
        assert "secrettoken" not in rec["source_url"]

    def test_source_path_still_means_the_local_source_directory(
        self, tmp_path, db_conn,
    ):
        """Pins the chosen answer to "what is source_path now?".

        It stays what it always was: the local directory the bytes were
        copied from — which for a remote source is the quarantine the
        fetcher materialised, not the URL.  The URL lives in
        ``source_url``.  ``reconcile_apply`` reads ``source_path`` only
        to carry it forward on a re-record (the install destination
        comes from ``install_path``), so keeping it a path keeps that
        path's meaning intact.
        """
        from carpenter.packages import sources

        src = make_source_pkg(tmp_path / "src", "p")
        spec = sources.parse_source("git+https://forge.example/org/p.git")
        install_package(
            src, tmp_path / "installed" / "p", conn=db_conn,
            provenance=spec, resolved="c" * 40,
        )
        rec = installer.get_install_record(db_conn, "p")
        assert rec["source_path"] == str(src.resolve())
        assert Path(rec["source_path"]).is_dir()
        assert rec["source_url"] == "https://forge.example/org/p.git"

    def test_reinstall_without_provenance_does_not_inherit_it(
        self, tmp_path, db_conn,
    ):
        """A re-install can come from somewhere else, so no carry-over."""
        from carpenter.packages import sources

        src = make_source_pkg(tmp_path / "src", "p")
        dest = tmp_path / "installed" / "p"
        install_package(
            src, dest, conn=db_conn,
            provenance=sources.parse_source(
                "git+https://forge.example/org/p.git",
            ),
            resolved="c" * 40,
        )
        install_package(src, dest, conn=db_conn)
        rec = installer.get_install_record(db_conn, "p")
        assert [rec[k] for k in _PROVENANCE_KEYS] == [None] * 7

    def test_list_install_records_carries_provenance(self, tmp_path, db_conn):
        from carpenter.packages import sources

        src = make_source_pkg(tmp_path / "src", "p")
        install_package(
            src, tmp_path / "installed" / "p", conn=db_conn,
            provenance=sources.parse_source(
                "git+https://forge.example/org/p.git@main",
            ),
            resolved="c" * 40,
        )
        records = list_install_records(db_conn)
        assert len(records) == 1
        assert records[0]["source_ref"] == "main"
        assert records[0]["source_id"]


class TestProvenanceMigration:
    """A DB created by the pre-provenance schema migrates in place."""

    @staticmethod
    def _old_schema_conn():
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        # The installed_packages table as it was BEFORE the source_*
        # columns existed (and before the three earlier additions, so
        # the whole column-presence migration runs).
        conn.executescript(
            """
            CREATE TABLE installed_packages (
                name TEXT PRIMARY KEY,
                version TEXT NOT NULL,
                hash TEXT NOT NULL,
                source_path TEXT NOT NULL,
                install_path TEXT NOT NULL,
                installed_at TEXT NOT NULL
            );
            """,
        )
        conn.execute(
            "INSERT INTO installed_packages "
            "(name, version, hash, source_path, install_path, installed_at) "
            "VALUES ('legacy', '0.1.0', 'abc', '/src/legacy', "
            "'/install/legacy', '2026-01-01T00:00:00+00:00')",
        )
        return conn

    def test_columns_are_added(self):
        conn = self._old_schema_conn()
        ensure_installer_tables(conn)
        cols = {
            row[1] for row in
            conn.execute("PRAGMA table_info(installed_packages)").fetchall()
        }
        assert set(_PROVENANCE_KEYS) <= cols
        conn.close()

    def test_pre_existing_row_reads_as_unknown_provenance(self):
        conn = self._old_schema_conn()
        ensure_installer_tables(conn)
        rec = installer.get_install_record(conn, "legacy")
        assert rec["source_path"] == "/src/legacy"
        assert [rec[k] for k in _PROVENANCE_KEYS] == [None] * 7
        conn.close()

    def test_migration_is_idempotent_and_installs_still_record(
        self, tmp_path,
    ):
        from carpenter.packages import sources

        conn = self._old_schema_conn()
        ensure_installer_tables(conn)
        ensure_installer_tables(conn)
        src = make_source_pkg(tmp_path / "src", "p")
        install_package(
            src, tmp_path / "installed" / "p", conn=conn,
            provenance=sources.parse_source(
                "git+https://forge.example/org/p.git@main",
            ),
            resolved="c" * 40,
        )
        rec = installer.get_install_record(conn, "p")
        assert rec["source_kind"] == "git"
        assert rec["source_ref"] == "main"
        # The legacy row is untouched by the new install.
        assert installer.get_install_record(conn, "legacy")["source_kind"] is None
        conn.close()
