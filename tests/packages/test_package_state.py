"""Tests for the per-package state primitive (D24 / Phase 3a, PR-A).

Covers:
  - CRUD via module-level functions and PackageStateHandle
  - CAS happy path and contention / version mismatch
  - Isolation: a handle for pkg-A cannot reach pkg-B's keys
  - FK cascade delete when ``installed_packages`` row goes away
  - Archive on uninstall + restore-from-archive helper
"""

from __future__ import annotations

import sqlite3

import pytest

from carpenter.packages import state as pkg_state
from carpenter.packages.installer import ensure_installer_tables
from carpenter.packages.state import PackageStateHandle


@pytest.fixture
def db_conn():
    """In-memory SQLite with installer + package_state tables, FK enforced."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_installer_tables(conn)
    conn.executescript("""
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
    yield conn
    conn.close()


def _seed_package(conn: sqlite3.Connection, name: str) -> None:
    """Insert a minimal installed_packages row so FK is satisfiable."""
    conn.execute(
        "INSERT INTO installed_packages "
        "(name, version, hash, source_path, install_path, installed_at) "
        "VALUES (?, '0.1.0', 'abc', '/tmp/s', '/tmp/d', '2026-05-20T00:00:00Z')",
        (name,),
    )
    conn.commit()


# ── CRUD ─────────────────────────────────────────────────────────────


class TestCrud:
    def test_set_get_roundtrip(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        v = pkg_state.set("pkg-a", "watermark", 42, conn=db_conn)
        assert v == 1
        assert pkg_state.get("pkg-a", "watermark", conn=db_conn) == 42
        # Update bumps version monotonically.
        v2 = pkg_state.set("pkg-a", "watermark", 43, conn=db_conn)
        assert v2 == 2
        assert pkg_state.get("pkg-a", "watermark", conn=db_conn) == 43

    def test_get_default_on_missing(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        assert pkg_state.get("pkg-a", "missing", default="nope", conn=db_conn) == "nope"

    def test_delete(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_state.set("pkg-a", "k", "v", conn=db_conn)
        assert pkg_state.delete("pkg-a", "k", conn=db_conn) is True
        assert pkg_state.delete("pkg-a", "k", conn=db_conn) is False
        assert pkg_state.get("pkg-a", "k", conn=db_conn) is None

    def test_list_keys_sorted(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        for k in ("c", "a", "b"):
            pkg_state.set("pkg-a", k, 1, conn=db_conn)
        assert pkg_state.list_keys("pkg-a", conn=db_conn) == ["a", "b", "c"]

    def test_get_with_version(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        assert pkg_state.get_with_version("pkg-a", "k", conn=db_conn) is None
        pkg_state.set("pkg-a", "k", {"x": 1}, conn=db_conn)
        result = pkg_state.get_with_version("pkg-a", "k", conn=db_conn)
        assert result == ({"x": 1}, 1)


# ── CAS ──────────────────────────────────────────────────────────────


class TestCas:
    def test_cas_insert_new(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        # expected_version=0 inserts when missing.
        assert pkg_state.cas("pkg-a", "k", 0, "v", conn=db_conn) is True
        assert pkg_state.get_with_version("pkg-a", "k", conn=db_conn) == ("v", 1)

    def test_cas_insert_missing_with_nonzero_expected_fails(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        assert pkg_state.cas("pkg-a", "k", 5, "v", conn=db_conn) is False
        assert pkg_state.get("pkg-a", "k", conn=db_conn) is None

    def test_cas_happy_path(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_state.set("pkg-a", "k", 1, conn=db_conn)
        assert pkg_state.cas("pkg-a", "k", 1, 2, conn=db_conn) is True
        assert pkg_state.get_with_version("pkg-a", "k", conn=db_conn) == (2, 2)

    def test_cas_version_mismatch(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_state.set("pkg-a", "k", 1, conn=db_conn)
        # Concurrent writer bumps the row to version 2.
        pkg_state.set("pkg-a", "k", 99, conn=db_conn)
        # Stale caller still thinks version is 1.
        assert pkg_state.cas("pkg-a", "k", 1, 2, conn=db_conn) is False
        # State unchanged from the concurrent writer's perspective.
        assert pkg_state.get_with_version("pkg-a", "k", conn=db_conn) == (99, 2)


# ── Isolation (I9) ───────────────────────────────────────────────────


class TestIsolation:
    def test_handle_cannot_touch_other_package(self, db_conn, monkeypatch):
        _seed_package(db_conn, "pkg-a")
        _seed_package(db_conn, "pkg-b")

        # The handle's module-level functions own their connections, so
        # we wrap db_conn in a lightweight proxy that ignores .close().
        # This lets the handle call db.close() per operation without
        # tearing down our shared test connection.
        class _NoCloseConn:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, *a, **kw):
                return self._inner.execute(*a, **kw)

            def commit(self):
                return self._inner.commit()

            def close(self):
                # Intentionally no-op so the fixture connection survives.
                pass

        proxy = _NoCloseConn(db_conn)
        monkeypatch.setattr(pkg_state, "get_db", lambda: proxy)

        handle_a = PackageStateHandle("pkg-a")
        handle_b = PackageStateHandle("pkg-b")

        handle_a.set("secret", "A's secret")
        handle_b.set("secret", "B's secret")

        # handle_a only ever sees pkg-a's keys.
        assert handle_a.get("secret") == "A's secret"
        assert handle_b.get("secret") == "B's secret"
        assert handle_a.list_keys() == ["secret"]
        assert handle_b.list_keys() == ["secret"]

        # No public API on the handle takes a package_name argument; the
        # bound name is fixed at construction.  ``__slots__`` prevents
        # smuggling extra attributes on the handle.
        assert handle_a.package_name == "pkg-a"
        with pytest.raises(AttributeError):
            handle_a.other_pkg = "pkg-b"  # type: ignore[attr-defined]

    def test_handle_rejects_empty_package_name(self):
        with pytest.raises(ValueError):
            PackageStateHandle("")
        with pytest.raises(ValueError):
            PackageStateHandle("   ")


# ── FK cascade ───────────────────────────────────────────────────────


class TestCascadeDelete:
    def test_state_rows_disappear_when_package_uninstalled(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        _seed_package(db_conn, "pkg-b")
        pkg_state.set("pkg-a", "k", "av", conn=db_conn)
        pkg_state.set("pkg-b", "k", "bv", conn=db_conn)
        # Drop pkg-a's install record; FK cascade wipes its state.
        db_conn.execute("DELETE FROM installed_packages WHERE name = ?", ("pkg-a",))
        db_conn.commit()
        assert pkg_state.get("pkg-a", "k", conn=db_conn) is None
        # pkg-b is unaffected.
        assert pkg_state.get("pkg-b", "k", conn=db_conn) == "bv"


# ── Archive on uninstall + restore ───────────────────────────────────


class TestArchiveAndRestore:
    def test_archive_then_cascade_preserves_archive(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_state.set("pkg-a", "watermark", 1234, conn=db_conn)
        pkg_state.set("pkg-a", "config", {"x": 1}, conn=db_conn)
        # Archive before cascading the install record delete.
        archived = pkg_state.archive_for_uninstall("pkg-a", conn=db_conn)
        assert archived == 2
        # Live rows are gone after archive_for_uninstall.
        assert pkg_state.list_keys("pkg-a", conn=db_conn) == []
        # Now drop the install record; cascade is effectively a no-op.
        db_conn.execute("DELETE FROM installed_packages WHERE name = ?", ("pkg-a",))
        db_conn.commit()
        # Archive rows survive.
        rows = db_conn.execute(
            "SELECT key, value_json, version FROM package_state_archive "
            "WHERE package_name = ? ORDER BY key", ("pkg-a",)
        ).fetchall()
        assert [r[0] for r in rows] == ["config", "watermark"]
        assert pkg_state.list_archived_packages(conn=db_conn) == ["pkg-a"]

    def test_restore_from_archive_round_trip(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_state.set("pkg-a", "watermark", 1234, conn=db_conn)
        pkg_state.archive_for_uninstall("pkg-a", conn=db_conn)
        # Simulate uninstall + reinstall: drop and recreate install record.
        db_conn.execute("DELETE FROM installed_packages WHERE name = ?", ("pkg-a",))
        db_conn.commit()
        _seed_package(db_conn, "pkg-a")
        # Restore.
        restored = pkg_state.restore_from_archive("pkg-a", conn=db_conn)
        assert restored == 1
        # Live row is back at its archived version.
        assert pkg_state.get_with_version("pkg-a", "watermark", conn=db_conn) == (1234, 1)
        # Archive is cleared after restore.
        assert pkg_state.list_archived_packages(conn=db_conn) == []

    def test_archive_for_uninstall_idempotent_replace(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_state.set("pkg-a", "k", "v1", conn=db_conn)
        pkg_state.archive_for_uninstall("pkg-a", conn=db_conn)
        # Re-install, write a fresh value, archive again.
        pkg_state.set("pkg-a", "k", "v2", conn=db_conn)
        archived = pkg_state.archive_for_uninstall("pkg-a", conn=db_conn)
        assert archived == 1
        rows = db_conn.execute(
            "SELECT value_json FROM package_state_archive "
            "WHERE package_name = ? AND key = ?", ("pkg-a", "k"),
        ).fetchall()
        assert len(rows) == 1
        # Newer archive replaced the older one.
        assert '"v2"' in rows[0][0]
