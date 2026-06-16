"""Tests for D24 B-full install side effects.

Covers:

* Allowlist proposals merge into ``security_policies`` at install time
  (one-way ratchet on uninstall — SD5).
* KB articles copy to ``<kb_root>/packages/<pkg>/<slug>.md`` and are
  cleaned up on uninstall.
* Trigger subscriptions register in-memory with ``source_package`` and
  unregister cleanly on uninstall.
* Manifest validation rejects unknown policy types.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter.packages.installer import (
    InstallError,
    compute_proposal_diff,
    ensure_installer_tables,
    get_install_record,
    install_package,
    uninstall_package,
)
from carpenter.packages.manifest import ManifestError, load_manifest


# ── Test scaffolding ────────────────────────────────────────────────


def _make_security_policies_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS security_policies (
            id INTEGER PRIMARY KEY,
            policy_type TEXT NOT NULL,
            value TEXT NOT NULL,
            UNIQUE(policy_type, value)
        );
        CREATE TABLE IF NOT EXISTS arcs (
            id INTEGER PRIMARY KEY,
            template_name TEXT,
            status TEXT
        );
        """
    )


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_installer_tables(conn)
    _make_security_policies_table(conn)
    yield conn
    conn.close()


@pytest.fixture
def isolated_policies():
    """Reset the SecurityPolicies singleton between tests."""
    from carpenter.security import policies as _p
    saved = _p._singleton
    _p._singleton = _p.SecurityPolicies()
    yield _p._singleton
    _p._singleton = saved


@pytest.fixture
def fake_kb_root(tmp_path, monkeypatch):
    """Point KB_ROOT at a tmp directory by patching config.CONFIG."""
    kb_dir = tmp_path / "kb_root"
    kb_dir.mkdir()
    from carpenter import config
    saved = dict(config.CONFIG)
    config.CONFIG["kb"] = {"dir": str(kb_dir)}
    yield kb_dir
    config.CONFIG.clear()
    config.CONFIG.update(saved)


@pytest.fixture
def reset_subscriptions():
    from carpenter.core.engine import subscriptions
    subscriptions.reset()
    yield subscriptions
    subscriptions.reset()


def _write_pkg(
    root: Path, name: str, manifest_yaml: str,
    files: dict[str, str] | None = None,
) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.yaml").write_text(dedent(manifest_yaml))
    for rel, content in (files or {}).items():
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dedent(content))
    return pkg


# ── Manifest parsing: allowlist_proposals ───────────────────────────


class TestAllowlistProposalsManifest:
    def test_valid_proposals_parse(self, tmp_path):
        pkg = _write_pkg(
            tmp_path, "p", """
                name: p
                version: "0.1.0"
                description: x
                allowlist_proposals:
                  - {type: domain, value: example.com}
                  - {type: email, value: foo@example.com}
            """,
        )
        m = load_manifest(pkg / "manifest.yaml")
        assert len(m.allowlist_proposals) == 2
        assert m.allowlist_proposals[0].policy_type == "domain"
        assert m.allowlist_proposals[1].value == "foo@example.com"

    def test_unknown_policy_type_rejected(self, tmp_path):
        pkg = _write_pkg(
            tmp_path, "p", """
                name: p
                version: "0.1.0"
                description: x
                allowlist_proposals:
                  - {type: not_a_real_type, value: anything}
            """,
        )
        with pytest.raises(ManifestError, match="not a recognised policy type"):
            load_manifest(pkg / "manifest.yaml")

    def test_duplicate_proposal_rejected(self, tmp_path):
        pkg = _write_pkg(
            tmp_path, "p", """
                name: p
                version: "0.1.0"
                description: x
                allowlist_proposals:
                  - {type: domain, value: example.com}
                  - {type: domain, value: example.com}
            """,
        )
        with pytest.raises(ManifestError, match="duplicate"):
            load_manifest(pkg / "manifest.yaml")

    def test_unknown_subkey_rejected(self, tmp_path):
        pkg = _write_pkg(
            tmp_path, "p", """
                name: p
                version: "0.1.0"
                description: x
                allowlist_proposals:
                  - {type: domain, value: example.com, junk: 1}
            """,
        )
        with pytest.raises(ManifestError, match="unknown keys"):
            load_manifest(pkg / "manifest.yaml")


# ── install + allowlist merge ──────────────────────────────────────


class TestAllowlistMerge:
    def test_install_merges_proposals_into_db(
        self, db_conn, tmp_path, isolated_policies,
    ):
        src = _write_pkg(
            tmp_path / "src", "p", """
                name: p
                version: "0.1.0"
                description: x
                allowlist_proposals:
                  - {type: domain, value: example.com}
                  - {type: domain, value: other.org}
            """,
        )
        dest = tmp_path / "installed" / "p"
        result = install_package(src, dest, conn=db_conn)
        assert sorted(result.allowlist_added) == [
            ("domain", "example.com"),
            ("domain", "other.org"),
        ]
        rows = db_conn.execute(
            "SELECT policy_type, value FROM security_policies "
            "ORDER BY value"
        ).fetchall()
        assert rows == [("domain", "example.com"), ("domain", "other.org")]
        # In-memory singleton has them too.
        assert "example.com" in isolated_policies.get_allowlist("domain")
        assert "other.org" in isolated_policies.get_allowlist("domain")

    def test_update_adds_new_entries_only(
        self, db_conn, tmp_path, isolated_policies,
    ):
        src = _write_pkg(
            tmp_path / "src", "p", """
                name: p
                version: "0.1.0"
                description: x
                allowlist_proposals:
                  - {type: domain, value: a.com}
            """,
        )
        dest = tmp_path / "installed" / "p"
        r1 = install_package(src, dest, conn=db_conn)
        assert r1.allowlist_added == (("domain", "a.com"),)

        # Update the manifest to add a second entry; first stays.
        (src / "manifest.yaml").write_text(dedent("""
            name: p
            version: "0.2.0"
            description: x
            allowlist_proposals:
              - {type: domain, value: a.com}
              - {type: domain, value: b.com}
        """))
        # Need a different source path for re-hashing semantics
        # (the same path is fine; the hash will differ).
        r2 = install_package(src, dest, conn=db_conn)
        assert r2.was_update
        # Only b.com is "added" relative to the prior install.
        assert r2.allowlist_added == (("domain", "b.com"),)
        # Both rows still in DB.
        rows = sorted(db_conn.execute(
            "SELECT value FROM security_policies WHERE policy_type='domain'"
        ).fetchall())
        assert ("a.com",) in rows
        assert ("b.com",) in rows

    def test_update_dropping_entry_does_not_remove(
        self, db_conn, tmp_path, isolated_policies,
    ):
        """SD5: removed proposals are NOT removed from security_policies."""
        src = _write_pkg(
            tmp_path / "src", "p", """
                name: p
                version: "0.1.0"
                description: x
                allowlist_proposals:
                  - {type: domain, value: a.com}
                  - {type: domain, value: b.com}
            """,
        )
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_conn)

        (src / "manifest.yaml").write_text(dedent("""
            name: p
            version: "0.2.0"
            description: x
            allowlist_proposals:
              - {type: domain, value: a.com}
        """))
        r2 = install_package(src, dest, conn=db_conn)
        assert r2.allowlist_removed == (("domain", "b.com"),)
        # b.com NOT removed from security_policies (one-way ratchet).
        rows = {r[0] for r in db_conn.execute(
            "SELECT value FROM security_policies WHERE policy_type='domain'"
        ).fetchall()}
        assert "a.com" in rows
        assert "b.com" in rows

    def test_merge_inside_db_transaction_no_nested_connection(
        self, tmp_path, monkeypatch,
    ):
        """Regression: installing a package WITH allowlist_proposals
        inside a real ``db_transaction()`` must not trip the same-thread
        deadlock guard.

        Before the fix, ``_merge_allowlist_proposals`` called
        ``get_policies()`` without threading the active connection.  When
        the SecurityPolicies singleton was cold (None), the lazy
        ``_load_from_db()`` opened a SECOND connection on the same thread
        inside the install transaction, tripping the ``get_db()`` deadlock
        guard and printing a traceback on every capability-package
        install.  We assert no RuntimeError escapes and the rows persist.
        """
        from carpenter.db import db_transaction, get_db
        from carpenter.security import policies as _p

        # Force the cold-start (lazy-load) path: singleton is None, so
        # ``get_policies()`` will run ``_load_from_db`` on first call.
        saved = _p._singleton
        _p._singleton = None
        # Ensure the security_policies table exists in the real test DB.
        conn0 = get_db()
        try:
            _make_security_policies_table(conn0)
            from carpenter.packages.installer import ensure_installer_tables
            ensure_installer_tables(conn0)
            conn0.commit()
        finally:
            conn0.close()

        src = _write_pkg(
            tmp_path / "src", "p", """
                name: p
                version: "0.1.0"
                description: x
                allowlist_proposals:
                  - {type: domain, value: nested.example.com}
            """,
        )
        dest = tmp_path / "installed" / "p"
        try:
            # No RuntimeError("get_db() called while a db_transaction() ...")
            # must escape here.
            with db_transaction() as db:
                result = install_package(src, dest, conn=db)
            assert result.allowlist_added == (
                ("domain", "nested.example.com"),
            )
            # Rows persisted (transaction committed cleanly).
            with db_transaction() as db:
                rows = db.execute(
                    "SELECT value FROM security_policies "
                    "WHERE policy_type='domain' "
                    "AND value='nested.example.com'"
                ).fetchall()
            assert len(rows) == 1
        finally:
            _p._singleton = saved

    def test_uninstall_does_not_touch_allowlists(
        self, db_conn, tmp_path, isolated_policies,
    ):
        src = _write_pkg(
            tmp_path / "src", "p", """
                name: p
                version: "0.1.0"
                description: x
                allowlist_proposals:
                  - {type: domain, value: a.com}
            """,
        )
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_conn)
        uninstall_package("p", conn=db_conn)
        rows = db_conn.execute(
            "SELECT value FROM security_policies WHERE policy_type='domain'"
        ).fetchall()
        assert ("a.com",) in rows


# ── KB article install ─────────────────────────────────────────────


class TestKBArticleInstall:
    def test_kb_articles_copy_to_per_package_folder(
        self, db_conn, tmp_path, fake_kb_root,
    ):
        src = _write_pkg(
            tmp_path / "src", "p", """
                name: p
                version: "0.1.0"
                description: x
                kb_namespace: p
                kb_articles:
                  - {path: kb/p/overview.md, slug: overview}
                  - {path: kb/p/details.md, slug: nested/details}
            """,
            files={
                "kb/p/overview.md": "# Overview\n\nHello.",
                "kb/p/details.md": "# Details\n\nMore.",
            },
        )
        dest = tmp_path / "installed" / "p"
        result = install_package(src, dest, conn=db_conn)
        assert result.kb_articles_installed == 2

        pkg_kb = fake_kb_root / "packages" / "p"
        assert (pkg_kb / "overview.md").read_text() == "# Overview\n\nHello."
        assert (pkg_kb / "nested" / "details.md").read_text() == "# Details\n\nMore."

    def test_kb_update_replaces_content(
        self, db_conn, tmp_path, fake_kb_root,
    ):
        src = _write_pkg(
            tmp_path / "src", "p", """
                name: p
                version: "0.1.0"
                description: x
                kb_namespace: p
                kb_articles:
                  - {path: kb/p/note.md, slug: note}
            """,
            files={"kb/p/note.md": "v1"},
        )
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_conn)
        pkg_kb = fake_kb_root / "packages" / "p"
        assert (pkg_kb / "note.md").read_text() == "v1"

        (src / "kb" / "p" / "note.md").write_text("v2")
        install_package(src, dest, conn=db_conn)
        assert (pkg_kb / "note.md").read_text() == "v2"

    def test_kb_uninstall_removes_folder(
        self, db_conn, tmp_path, fake_kb_root,
    ):
        src = _write_pkg(
            tmp_path / "src", "p", """
                name: p
                version: "0.1.0"
                description: x
                kb_namespace: p
                kb_articles:
                  - {path: kb/p/x.md, slug: x}
            """,
            files={"kb/p/x.md": "content"},
        )
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_conn)
        pkg_kb = fake_kb_root / "packages" / "p"
        assert pkg_kb.is_dir()
        uninstall_package("p", conn=db_conn)
        assert not pkg_kb.exists()

    def test_two_packages_same_slug_no_collision(
        self, db_conn, tmp_path, fake_kb_root,
    ):
        for pname, body in (("a", "from-a"), ("b", "from-b")):
            src = _write_pkg(
                tmp_path / "src" / pname, pname, f"""
                    name: {pname}
                    version: "0.1.0"
                    description: x
                    kb_namespace: {pname}
                    kb_articles:
                      - {{path: kb/{pname}/shared.md, slug: shared}}
                """,
                files={f"kb/{pname}/shared.md": body},
            )
            install_package(
                src, tmp_path / "installed" / pname, conn=db_conn,
            )
        assert (
            (fake_kb_root / "packages" / "a" / "shared.md").read_text()
            == "from-a"
        )
        assert (
            (fake_kb_root / "packages" / "b" / "shared.md").read_text()
            == "from-b"
        )

    def test_kb_install_inside_db_transaction_no_nested_connection(
        self, tmp_path, test_db, caplog,
    ):
        """Regression: installing a package WITH kb_articles inside a real
        ``db_transaction()`` must not trip the same-thread deadlock guard.

        Before the fix, ``_install_kb_articles`` called
        ``KBStore.sync_from_filesystem()`` without threading the active
        install connection; the re-index opened a SECOND connection on the
        install thread (read + ``_upsert_entry`` + ``_search.update_entry``)
        inside the install transaction, tripping ``carpenter.db.get_db``'s
        deadlock guard and printing a traceback on every package that ships
        KB articles.  We assert no RuntimeError escapes/logs and the entry
        is indexed into ``kb_entries`` (committed with the install).
        """
        from carpenter.db import db_transaction, get_db

        # Ensure the installer tables exist in the real test DB.
        conn0 = get_db()
        try:
            ensure_installer_tables(conn0)
            conn0.commit()
        finally:
            conn0.close()

        src = _write_pkg(
            tmp_path / "src", "kbpkg", """
                name: kbpkg
                version: "0.1.0"
                description: x
                kb_namespace: kbpkg
                kb_articles:
                  - {path: kb/kbpkg/overview.md, slug: overview}
            """,
            files={"kb/kbpkg/overview.md": "# Overview\n\nHello kbpkg."},
        )
        dest = tmp_path / "installed" / "kbpkg"

        with caplog.at_level("ERROR"):
            with db_transaction() as db:
                result = install_package(src, dest, conn=db)
        assert result.kb_articles_installed == 1

        # No deadlock-guard RuntimeError surfaced via logging.exception.
        guard_msgs = [
            r.getMessage() + (r.exc_text or "")
            for r in caplog.records
            if "db_transaction() is active" in (
                r.getMessage() + (r.exc_text or "")
            )
        ]
        assert guard_msgs == [], guard_msgs

        # The article was indexed into kb_entries by the threaded sync,
        # committed atomically with the install transaction.
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT path FROM kb_entries WHERE path = ?",
                ("packages/kbpkg/overview",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None


# ── Trigger subscription registration ──────────────────────────────


class TestTriggerSubscriptions:
    def test_install_registers_subscriptions(
        self, db_conn, tmp_path, reset_subscriptions,
    ):
        src = _write_pkg(
            tmp_path / "src", "p", """
                name: p
                version: "0.1.0"
                description: x
                trigger_subscriptions:
                  - {event: ping.received, handler: handlers.h:run}
            """,
            files={
                "handlers/__init__.py": "",
                "handlers/h.py": "def run(payload):\n    return payload\n",
            },
        )
        dest = tmp_path / "installed" / "p"
        result = install_package(src, dest, conn=db_conn)
        assert result.trigger_subscriptions_registered == 1
        subs = reset_subscriptions.get_subscriptions()
        names = [s.name for s in subs if s.source_package == "p"]
        assert names == ["_pkg.p.0"]
        # The on-disk record was written.
        rec = dest / "_subscriptions.json"
        entries = json.loads(rec.read_text())
        assert entries == [
            {"event": "ping.received", "handler": "handlers.h:run"},
        ]

    def test_uninstall_removes_subscriptions(
        self, db_conn, tmp_path, reset_subscriptions,
    ):
        src = _write_pkg(
            tmp_path / "src", "p", """
                name: p
                version: "0.1.0"
                description: x
                trigger_subscriptions:
                  - {event: ping.received, handler: handlers.h:run}
            """,
            files={
                "handlers/__init__.py": "",
                "handlers/h.py": "def run(payload):\n    return payload\n",
            },
        )
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_conn)
        # Sanity: registered.
        assert any(
            s.source_package == "p"
            for s in reset_subscriptions.get_subscriptions()
        )
        uninstall_package("p", conn=db_conn)
        # No more package-tagged subs.
        assert not any(
            s.source_package == "p"
            for s in reset_subscriptions.get_subscriptions()
        )

    def test_reinstall_does_not_double_register(
        self, db_conn, tmp_path, reset_subscriptions,
    ):
        src = _write_pkg(
            tmp_path / "src", "p", """
                name: p
                version: "0.1.0"
                description: x
                trigger_subscriptions:
                  - {event: ping.received, handler: handlers.h:run}
            """,
            files={
                "handlers/__init__.py": "",
                "handlers/h.py": "def run(payload):\n    return payload\n",
            },
        )
        dest = tmp_path / "installed" / "p"
        install_package(src, dest, conn=db_conn)
        install_package(src, dest, conn=db_conn)
        pkg_subs = [
            s for s in reset_subscriptions.get_subscriptions()
            if s.source_package == "p"
        ]
        assert len(pkg_subs) == 1


# ── compute_proposal_diff (pure helper) ────────────────────────────


class TestProposalDiff:
    def test_fresh_install_diff(self):
        # Build a fake manifest + None record.
        from carpenter.packages.manifest import (
            AllowlistProposal, PackageManifest,
        )
        m = PackageManifest(
            name="p", version="1", description="x",
            allowlist_proposals=(
                AllowlistProposal(policy_type="domain", value="a.com"),
            ),
        )
        diff = compute_proposal_diff(m, None)
        assert diff.added == (("domain", "a.com"),)
        assert diff.removed == ()

    def test_update_diff_added_and_removed(self):
        from carpenter.packages.manifest import (
            AllowlistProposal, PackageManifest,
        )
        m = PackageManifest(
            name="p", version="2", description="x",
            allowlist_proposals=(
                AllowlistProposal(policy_type="domain", value="b.com"),
            ),
        )
        prior = {
            "allowlist_proposals_json": json.dumps([
                {"type": "domain", "value": "a.com"},
            ]),
        }
        diff = compute_proposal_diff(m, prior)
        assert diff.added == (("domain", "b.com"),)
        assert diff.removed == (("domain", "a.com"),)


# ── B-full NIT #5: _compute_platform_templates() sanity ────────────


class TestComputePlatformTemplates:
    """Sanity check that the derived platform-template gate stays in
    sync with ``config_seed/templates/``.

    A drift here means either a new platform template was added without
    refreshing this test (in which case add it to ``KNOWN``) or — far
    worse — that ``config_seed/templates/`` failed to ship in the build
    and the gate quietly emptied, letting packages claim platform
    template names.
    """

    def test_known_platform_templates_present(self):
        from carpenter.packages.handler_registry import (
            _compute_platform_templates,
        )
        _compute_platform_templates.cache_clear()
        names = _compute_platform_templates()
        # A handful of well-known platform templates that have shipped
        # for many releases.  This list is intentionally small — adding
        # to it should be a deliberate decision tied to platform changes.
        for known in ("coding-change", "reflection", "pr-review"):
            assert known in names, (
                f"{known!r} missing from platform-template set "
                f"(got {sorted(names)}); did config_seed/templates/ "
                f"fail to ship?"
            )

    def test_returns_frozenset_and_is_cached(self):
        from carpenter.packages.handler_registry import (
            _compute_platform_templates,
        )
        _compute_platform_templates.cache_clear()
        first = _compute_platform_templates()
        second = _compute_platform_templates()
        assert isinstance(first, frozenset)
        # Cached: same object identity on repeat call.
        assert first is second

    def test_module_attr_proxies_to_function(self):
        # The PEP 562 ``__getattr__`` exposes ``_PLATFORM_TEMPLATES`` as
        # a module-level name backed by the cached function.
        import carpenter.packages.handler_registry as hr
        hr._compute_platform_templates.cache_clear()
        attr = hr._PLATFORM_TEMPLATES  # type: ignore[attr-defined]
        assert isinstance(attr, frozenset)
        assert "coding-change" in attr


# ── Install-time pristine-archive caching (reconcile prerequisite) ──


class TestInstallArchiveCaching:
    def test_install_caches_pristine_archive_round_trip(
        self, db_conn, tmp_path,
    ):
        """On a successful install, the installed version's pristine tree
        is archived into the local cache and round-trips via
        ``load_pristine_tree`` against the recorded install hash."""
        from carpenter.packages import archive_cache

        src = _write_pkg(
            tmp_path / "src", "p", """
                name: p
                version: "0.3.0"
                description: x
            """,
            files={"extra.txt": "hello reconcile\n"},
        )
        dest = tmp_path / "installed" / "p"
        result = install_package(src, dest, conn=db_conn)

        # The archive exists at the cache path keyed by name/version.
        cached = archive_cache.cache_dir() / "p" / "0.3.0.tar.gz"
        assert cached.is_file()

        # Round-trip: load_pristine_tree verifies against the install hash.
        tree = archive_cache.load_pristine_tree(
            "p", "0.3.0", expected_root_hash=result.hash,
        )
        assert tree["manifest.yaml"]
        assert tree["extra.txt"] == b"hello reconcile\n"

    def test_install_succeeds_when_archive_cache_write_fails(
        self, db_conn, tmp_path, monkeypatch,
    ):
        """Archiving is best-effort: a cache-write failure must NOT fail
        the install."""
        from carpenter.packages import archive_cache

        def _boom(*_a, **_k):
            raise OSError("simulated cache write failure")

        monkeypatch.setattr(archive_cache, "store_archive", _boom)

        src = _write_pkg(
            tmp_path / "src", "p", """
                name: p
                version: "0.4.0"
                description: x
            """,
        )
        dest = tmp_path / "installed" / "p"
        # Must not raise despite the cache failure.
        result = install_package(src, dest, conn=db_conn)
        assert result.version == "0.4.0"
        # Install record persisted.
        rec = get_install_record(db_conn, "p")
        assert rec is not None
        assert rec["version"] == "0.4.0"
