"""Tests for the reconcile *apply* step (materialize a resolved tree)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter.packages import reconcile
from carpenter.packages.installer import (
    compute_package_hash,
    ensure_installer_tables,
    get_install_record,
    install_package,
)
from carpenter.packages.reconcile_apply import (
    ReconcileApplyError,
    apply_reconciled_install,
)


# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_installer_tables(conn)
    yield conn
    conn.close()


def _manifest_yaml(name: str, version: str = "0.1.0") -> str:
    return dedent(f"""\
        name: {name}
        version: "{version}"
        description: Test package {name}.
    """)


def _make_source_pkg(
    root: Path, name: str, *, version: str = "0.1.0",
    extra_files: dict[str, str] | None = None,
) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.yaml").write_text(_manifest_yaml(name, version))
    for rel, content in (extra_files or {}).items():
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return pkg


def _install_one(
    db_conn, tmp_path: Path, name: str, *, version: str = "0.1.0",
    extra_files: dict[str, str] | None = None,
):
    src = _make_source_pkg(
        tmp_path / "src", name, version=version, extra_files=extra_files,
    )
    dest = tmp_path / "installed" / name
    install_package(src, dest, conn=db_conn)
    return dest


def _resolved(name: str, version: str, files: dict[str, str]) -> dict[str, bytes]:
    """Build a resolved tree (path -> bytes) with a manifest."""
    tree = {"manifest.yaml": _manifest_yaml(name, version).encode("utf-8")}
    for rel, content in files.items():
        tree[rel] = content.encode("utf-8")
    return tree


# ── happy path ──────────────────────────────────────────────────────


class TestApplyHappyPath:
    def test_apply_over_existing_install(self, db_conn, tmp_path):
        dest = _install_one(
            db_conn, tmp_path, "p", version="0.1.0",
            extra_files={"a.py": "old-a", "b.py": "old-b"},
        )
        resolved = _resolved(
            "p", "0.2.0", {"a.py": "new-a", "c.py": "new-c"},
        )

        result = apply_reconciled_install(
            "p", "0.2.0", resolved, conn=db_conn,
        )

        # Dest contains exactly the resolved files (b.py is gone).
        on_disk = {
            p.relative_to(dest).as_posix(): p.read_text()
            for p in dest.rglob("*") if p.is_file()
        }
        assert on_disk == {
            "manifest.yaml": _manifest_yaml("p", "0.2.0"),
            "a.py": "new-a",
            "c.py": "new-c",
        }
        assert "b.py" not in on_disk

        # Result reflects the write.
        assert result.dest_path == dest.resolve()
        assert result.version == "0.2.0"
        assert result.files_written == 3  # manifest + a.py + c.py

        # Hash matches compute_package_hash of the materialized tree.
        assert result.hash == compute_package_hash(dest)

        # installed_packages row updated (version + hash).
        record = get_install_record(db_conn, "p")
        assert record["version"] == "0.2.0"
        assert record["hash"] == result.hash
        assert record["hash"] == compute_package_hash(dest)

    def test_dest_resolved_from_record_when_none(self, db_conn, tmp_path):
        dest = _install_one(db_conn, tmp_path, "p", extra_files={"a.py": "x"})
        resolved = _resolved("p", "0.2.0", {"a.py": "y"})

        result = apply_reconciled_install(
            "p", "0.2.0", resolved, conn=db_conn, dest_path=None,
        )
        assert result.dest_path == dest.resolve()
        assert (dest / "a.py").read_text() == "y"

    def test_explicit_dest_path(self, db_conn, tmp_path):
        dest = tmp_path / "out" / "p"
        resolved = _resolved("p", "1.0.0", {"a.py": "z"})
        result = apply_reconciled_install(
            "p", "1.0.0", resolved, conn=db_conn, dest_path=dest,
        )
        assert result.dest_path == dest.resolve()
        assert (dest / "a.py").read_text() == "z"
        # A row was written even though there was no prior install.
        assert get_install_record(db_conn, "p")["version"] == "1.0.0"


# ── atomicity / rollback ────────────────────────────────────────────


class TestAtomicity:
    def test_failure_mid_apply_leaves_prior_install_intact(
        self, db_conn, tmp_path, monkeypatch,
    ):
        dest = _install_one(
            db_conn, tmp_path, "p", version="0.1.0",
            extra_files={"a.py": "old-a"},
        )
        prior_hash = compute_package_hash(dest)
        prior_record = dict(get_install_record(db_conn, "p"))

        # Inject a failure during the atomic swap.
        import carpenter.packages.reconcile_apply as ra

        def _boom(_src, _dst):
            raise OSError("simulated swap failure")

        monkeypatch.setattr(ra, "_atomic_copy_into_place", _boom)

        resolved = _resolved("p", "0.2.0", {"a.py": "new-a"})
        with pytest.raises(OSError, match="simulated swap failure"):
            apply_reconciled_install("p", "0.2.0", resolved, conn=db_conn)

        # Prior install bytes untouched.
        assert (dest / "a.py").read_text() == "old-a"
        assert compute_package_hash(dest) == prior_hash
        # DB row unchanged (version + hash still the prior values).
        record = get_install_record(db_conn, "p")
        assert record["version"] == prior_record["version"] == "0.1.0"
        assert record["hash"] == prior_record["hash"]

    def test_atomic_copy_rollback_restores_old_tree(self, db_conn, tmp_path):
        """Exercise _atomic_copy_into_place's own rollback path.

        Make the final ``os.replace`` of staging-into-place fail by making
        the dest's parent read-only after the old dir is rotated out.  The
        helper must restore the rotated-old dir to ``dest``.
        """
        dest = _install_one(
            db_conn, tmp_path, "p", version="0.1.0",
            extra_files={"a.py": "old-a"},
        )
        prior_hash = compute_package_hash(dest)

        import carpenter.packages.reconcile_apply as ra

        real_replace = ra.os.replace
        calls = {"n": 0}

        def _flaky_replace(src, dst):
            # First replace = rotate old out (allow); second = staging in
            # (fail) so rollback fires.
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("simulated replace-into-place failure")
            return real_replace(src, dst)

        import carpenter.packages.installer as inst

        # Patch os.replace as seen by the installer's atomic copy.
        original = inst.os.replace
        inst.os.replace = _flaky_replace  # type: ignore[assignment]
        try:
            resolved = _resolved("p", "0.2.0", {"a.py": "new-a"})
            with pytest.raises(OSError, match="replace-into-place"):
                apply_reconciled_install("p", "0.2.0", resolved, conn=db_conn)
        finally:
            inst.os.replace = original  # type: ignore[assignment]

        # Old tree restored; DB row unchanged.
        assert dest.is_dir()
        assert (dest / "a.py").read_text() == "old-a"
        assert compute_package_hash(dest) == prior_hash
        assert get_install_record(db_conn, "p")["version"] == "0.1.0"


# ── path safety ─────────────────────────────────────────────────────


class TestPathSafety:
    def test_rejects_parent_escape(self, db_conn, tmp_path):
        dest = tmp_path / "out" / "p"
        resolved = _resolved("p", "1.0.0", {"a.py": "x"})
        resolved["../escape.py"] = b"evil"
        with pytest.raises(ReconcileApplyError, match="escapes staging root"):
            apply_reconciled_install("p", "1.0.0", resolved, conn=db_conn, dest_path=dest)
        # Nothing was written outside.
        assert not (tmp_path / "out" / "escape.py").exists()
        assert not (tmp_path / "escape.py").exists()

    def test_rejects_absolute_path(self, db_conn, tmp_path):
        dest = tmp_path / "out" / "p"
        resolved = _resolved("p", "1.0.0", {"a.py": "x"})
        resolved["/etc/evil"] = b"evil"
        with pytest.raises(ReconcileApplyError, match="absolute"):
            apply_reconciled_install("p", "1.0.0", resolved, conn=db_conn, dest_path=dest)

    def test_rejects_deep_escape(self, db_conn, tmp_path):
        dest = tmp_path / "out" / "p"
        resolved = _resolved("p", "1.0.0", {"a.py": "x"})
        resolved["sub/../../escape.py"] = b"evil"
        with pytest.raises(ReconcileApplyError, match="escapes staging root"):
            apply_reconciled_install("p", "1.0.0", resolved, conn=db_conn, dest_path=dest)

    def test_unsafe_key_writes_nothing(self, db_conn, tmp_path):
        """A single bad key aborts before any partial materialization."""
        dest = tmp_path / "out" / "p"
        resolved = _resolved("p", "1.0.0", {"a.py": "x", "b.py": "y"})
        resolved["../escape.py"] = b"evil"
        with pytest.raises(ReconcileApplyError):
            apply_reconciled_install("p", "1.0.0", resolved, conn=db_conn, dest_path=dest)
        assert not dest.exists()


# ── misc guards ─────────────────────────────────────────────────────


class TestGuards:
    def test_empty_tree_rejected(self, db_conn, tmp_path):
        with pytest.raises(ReconcileApplyError, match="empty"):
            apply_reconciled_install(
                "p", "1.0.0", {}, conn=db_conn, dest_path=tmp_path / "p",
            )

    def test_missing_manifest_rejected(self, db_conn, tmp_path):
        resolved = {"a.py": b"x"}
        with pytest.raises(ReconcileApplyError, match="manifest"):
            apply_reconciled_install(
                "p", "1.0.0", resolved, conn=db_conn, dest_path=tmp_path / "p",
            )

    def test_manifest_name_mismatch_rejected(self, db_conn, tmp_path):
        resolved = _resolved("other", "1.0.0", {"a.py": "x"})
        with pytest.raises(ReconcileApplyError, match="does not match"):
            apply_reconciled_install(
                "p", "1.0.0", resolved, conn=db_conn, dest_path=tmp_path / "p",
            )

    def test_dest_basename_mismatch_rejected(self, db_conn, tmp_path):
        resolved = _resolved("p", "1.0.0", {"a.py": "x"})
        with pytest.raises(ReconcileApplyError, match="basename"):
            apply_reconciled_install(
                "p", "1.0.0", resolved, conn=db_conn,
                dest_path=tmp_path / "wrong-name",
            )

    def test_no_record_no_dest_rejected(self, db_conn):
        resolved = _resolved("p", "1.0.0", {"a.py": "x"})
        with pytest.raises(ReconcileApplyError, match="no install record"):
            apply_reconciled_install("p", "1.0.0", resolved, conn=db_conn)


# ── round-trip with reconcile.classify ──────────────────────────────


class TestRoundTripWithReconcile:
    def test_classify_then_apply(self, db_conn, tmp_path):
        # old = what the previous version shipped; current = on-disk now
        # (user edited user.py); new = what the upgrade ships.
        old = {
            "manifest.yaml": _manifest_yaml("p", "0.1.0"),
            "shared.py": "v1",
            "user.py": "v1",
        }
        current = {
            "manifest.yaml": _manifest_yaml("p", "0.1.0"),
            "shared.py": "v1",
            "user.py": "user-edited",  # user-only change
        }
        new = {
            "manifest.yaml": _manifest_yaml("p", "0.2.0"),
            "shared.py": "v2",  # upstream-only change
            "user.py": "v1",
            "upstream_new.py": "fresh",  # added upstream
        }

        plan = reconcile.classify(old, new, current)
        status_by_path = {d.path: d.status for d in plan.deltas}
        assert status_by_path["shared.py"] == reconcile.FileStatus.UPSTREAM_ONLY
        assert status_by_path["user.py"] == reconcile.FileStatus.USER_ONLY
        assert (
            status_by_path["upstream_new.py"]
            == reconcile.FileStatus.ADDED_UPSTREAM
        )

        # Resolution policy: take-new for upstream-only / added-upstream,
        # keep-current for user-only.
        resolved: dict[str, bytes] = {}
        for delta in plan.deltas:
            path = delta.path
            st = delta.status
            if st in (
                reconcile.FileStatus.UPSTREAM_ONLY,
                reconcile.FileStatus.ADDED_UPSTREAM,
                reconcile.FileStatus.UNCHANGED,
            ):
                resolved[path] = new[path].encode("utf-8")
            elif st == reconcile.FileStatus.USER_ONLY:
                resolved[path] = current[path].encode("utf-8")
            else:
                raise AssertionError(f"unexpected status {st} for {path}")

        # First do a baseline install so a record/dest exist.
        dest = _install_one(
            db_conn, tmp_path, "p", version="0.1.0",
            extra_files={"shared.py": "v1", "user.py": "user-edited"},
        )

        result = apply_reconciled_install(
            "p", "0.2.0", resolved, conn=db_conn, dest_path=dest,
        )

        installed = {
            p.relative_to(dest).as_posix(): p.read_text()
            for p in dest.rglob("*") if p.is_file()
        }
        assert installed == {
            "manifest.yaml": _manifest_yaml("p", "0.2.0"),
            "shared.py": "v2",       # took upstream
            "user.py": "user-edited",  # kept user
            "upstream_new.py": "fresh",  # added upstream
        }
        assert result.hash == compute_package_hash(dest)
        assert get_install_record(db_conn, "p")["hash"] == result.hash
