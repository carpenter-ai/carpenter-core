"""Tests for updating an installed package from its recorded source.

Hermetic: no network and no real remote.  Every fetch goes through a
stub fetcher registered for a test-only source kind via the public
:func:`carpenter.packages.fetch.register_fetcher` seam, which is also
the seam the git and archive fetchers use — those have their own tests
and are deliberately not exercised here.  The dispatcher's egress gate
still runs for a non-local kind, so the test config allowlists the
stub's host exactly as an operator would allowlist a self-hosted forge.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path
from textwrap import dedent
from typing import ClassVar

import pytest

from carpenter.packages import archive_cache, fetch, update
from carpenter.packages.archive_cache import (
    ArchiveVerificationError,
    load_pristine_tree,
    store_archive,
)
from carpenter.packages.fetch import FetchError, FetchResult
from carpenter.packages.installer import (
    InstallError,
    compute_package_hash,
    default_install_dest,
    ensure_installer_tables,
    get_install_record,
    granted_verbs_for_package,
    install_package,
)
from carpenter.packages.sources import SourceSpec, source_id
from carpenter.packages.update import (
    STRATEGY_CLOBBER,
    STRATEGY_NOOP,
    STRATEGY_RECONCILE,
    UpdateError,
    apply_update,
    cleanup_plan,
    plan_update,
)

# A source kind no real fetcher handles, so registering a stub for it
# cannot shadow a shipped fetcher.
KIND = "updatestub"
HOST = "forge.update.invalid"
URL = f"https://{HOST}/org/pkgs"
OTHER_URL = f"https://{HOST}/other/pkgs"

#: Distinguishes "the test did not say" from "the test said: none".
_DEFAULT = object()


# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def base_dir(tmp_path, monkeypatch):
    """Isolated ``base_dir`` + the stub host allowlisted for egress."""
    base = tmp_path / "carpenter-base"
    base.mkdir()
    monkeypatch.setattr(
        "carpenter.config.CONFIG",
        {"base_dir": str(base), "web_egress_allowlist": [HOST]},
    )
    return base


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_installer_tables(conn)
    yield conn
    conn.close()


class _StubFetcher:
    """Serves a prepared local tree as if it had been fetched.

    ``serve`` is the directory copied into the quarantine the dispatcher
    hands over; ``fail_with`` makes the fetch raise instead, which is how
    the "fetch failed, nothing was touched" case is driven.
    """

    kinds: ClassVar[frozenset[str]] = frozenset({KIND})

    def __init__(self):
        self.serve: Path | None = None
        self.resolved = "b" * 40
        self.fail_with: Exception | None = None
        self.calls: list[SourceSpec] = []

    def fetch(self, spec, dest, *, limits):
        self.calls.append(spec)
        if self.fail_with is not None:
            raise self.fail_with
        assert self.serve is not None, "test did not set stub.serve"
        tree = Path(dest) / "tree"
        shutil.copytree(self.serve, tree)
        files = [p for p in tree.rglob("*") if p.is_file()]
        return FetchResult(
            tree=tree,
            staging=Path(dest),
            resolved=self.resolved,
            bytes_fetched=sum(p.stat().st_size for p in files),
            files_fetched=len(files),
        )


@pytest.fixture
def stub():
    """A stub fetcher registered for ``KIND``, unregistered after."""
    saved = dict(fetch._FETCHERS)
    f = _StubFetcher()
    fetch.register_fetcher(f)
    yield f
    fetch._FETCHERS.clear()
    fetch._FETCHERS.update(saved)


# ── helpers ─────────────────────────────────────────────────────────


def _manifest(name: str, version: str) -> str:
    return dedent(f"""\
        name: {name}
        version: "{version}"
        description: Update-flow fixture package.
    """)


def _write_tree(
    root: Path, name: str, version: str, files: dict[str, str],
) -> Path:
    """Write a package tree at ``root/<label>`` and return it."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.yaml").write_text(_manifest(name, version))
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return root


def _spec(*, url: str = URL, ref: str | None = "main") -> SourceSpec:
    return SourceSpec(kind=KIND, raw=url, url=url, ref=ref)


def _install(
    db_conn,
    tmp_path: Path,
    *,
    name: str = "demo",
    version: str = "0.1.0",
    files: dict[str, str] | None = None,
    provenance=_DEFAULT,
    resolved: str = "a" * 40,
    **kw,
):
    """Install a fixture package, recording remote provenance by default."""
    src = _write_tree(
        tmp_path / f"src-{name}-{version}", name, version,
        files if files is not None else {"a.py": "a-v1\n", "b.py": "b-v1\n"},
    )
    return install_package(
        src, default_install_dest(name), conn=db_conn,
        provenance=_spec() if provenance is _DEFAULT else provenance,
        resolved=resolved, **kw,
    )


def _serve(stub, tmp_path: Path, *, name="demo", version="0.2.0", files):
    stub.serve = _write_tree(
        tmp_path / f"remote-{name}-{version}", name, version, files,
    )
    return stub.serve


def _on_disk(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): p.read_text()
        for p in root.rglob("*") if p.is_file()
    }


# ── a clean upgrade ─────────────────────────────────────────────────


class TestCleanUpgrade:
    def test_no_local_edits_adopts_the_new_version(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        installed = _install(db_conn, tmp_path)
        _serve(stub, tmp_path, files={"a.py": "a-v2\n", "c.py": "c-v2\n"})

        plan = plan_update("demo", conn=db_conn)
        assert plan.strategy == STRATEGY_RECONCILE
        assert plan.local_edits == ()
        assert plan.conflicts == ()
        assert plan.installed_version == "0.1.0"
        assert plan.fetched_version == "0.2.0"
        assert plan.resolved == "b" * 40
        # The ref was followed, not the pinned commit.
        assert stub.calls[0].ref == "main"

        result = apply_update(plan, conn=db_conn)

        assert result.changed is True
        assert _on_disk(installed.dest_path) == {
            "manifest.yaml": _manifest("demo", "0.2.0"),
            "a.py": "a-v2\n",
            "c.py": "c-v2\n",
        }
        record = get_install_record(db_conn, "demo")
        assert record["version"] == "0.2.0"
        assert record["hash"] == plan.fetched_hash
        # No edits merged in, so the shipped tree IS the installed tree.
        assert record["shipped_hash"] == plan.fetched_hash
        assert record["source_resolved"] == "b" * 40
        assert record["source_kind"] == KIND
        # The quarantine is retired by a successful apply.
        assert plan.staging is not None and not plan.staging.exists()

    def test_restart_is_reported_not_acted_on(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        _install(db_conn, tmp_path)
        _serve(stub, tmp_path, files={"a.py": "a-v2\n"})
        plan = plan_update("demo", conn=db_conn)

        # Nothing of the package is imported in this process.
        result = apply_update(plan, conn=db_conn)
        assert result.needs_restart is False
        assert "restart" in result.restart_reason

        # With a cached module under the package namespace, the honest
        # answer flips — the conftest purges the namespace afterwards.
        sys.modules["_carpenter_pkg_.demo.thing"] = object()  # type: ignore[assignment]
        _serve(stub, tmp_path, version="0.3.0", files={"a.py": "a-v3\n"})
        plan = plan_update("demo", conn=db_conn)
        result = apply_update(plan, conn=db_conn)
        assert result.needs_restart is True
        assert "already imported" in result.restart_reason

    def test_granted_capability_survives_an_update(
        self, base_dir, db_conn, tmp_path,
    ):
        """A re-record must not silently revoke platform trust."""
        cap_manifest = dedent("""\
            name: capdemo
            version: "{version}"
            description: Capability fixture.
            credential_requirements:
              - kind: env
                provider: demo
                env_key_prefix: CAPDEMO
                required_keys:
                  - HOST
                  - PASSWORD
            platform_capabilities:
              - verb: capdemo.echo
                kind: egress
                module: handlers.echo
                handler: handle
                grant:
                  protocol: demo
                  host_from: HOST
                  port: 993
                  credential_ref: CAPDEMO
        """)

        def _write(root: Path, version: str, body: str) -> Path:
            root.mkdir(parents=True, exist_ok=True)
            (root / "manifest.yaml").write_text(
                cap_manifest.format(version=version),
            )
            (root / "handlers").mkdir(exist_ok=True)
            (root / "handlers" / "__init__.py").write_text("")
            (root / "handlers" / "echo.py").write_text(body)
            return root

        src = _write(tmp_path / "cap-v1", "0.1.0", "def handle(p, c):\n    return 1\n")
        install_package(
            src, default_install_dest("capdemo"), conn=db_conn,
            provenance=_spec(), resolved="a" * 40,
            capability_input_fn=lambda _p: "yes",
        )
        assert granted_verbs_for_package(db_conn, "capdemo") == {"capdemo.echo"}

        saved = dict(fetch._FETCHERS)
        f = _StubFetcher()
        fetch.register_fetcher(f)
        try:
            f.serve = _write(
                tmp_path / "cap-v2", "0.2.0",
                "def handle(p, c):\n    return 2\n",
            )
            apply_update(plan_update("capdemo", conn=db_conn), conn=db_conn)
        finally:
            fetch._FETCHERS.clear()
            fetch._FETCHERS.update(saved)

        assert granted_verbs_for_package(db_conn, "capdemo") == {"capdemo.echo"}
        assert get_install_record(db_conn, "capdemo")["version"] == "0.2.0"


# ── local edits ─────────────────────────────────────────────────────


class TestLocalEdits:
    def test_edit_to_an_untouched_file_is_preserved(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        installed = _install(db_conn, tmp_path)
        (installed.dest_path / "b.py").write_text("b-MINE\n")
        _serve(stub, tmp_path, files={
            "a.py": "a-v2\n", "b.py": "b-v1\n",
        })

        plan = plan_update("demo", conn=db_conn)
        assert plan.strategy == STRATEGY_RECONCILE
        assert plan.local_edits == ("b.py",)
        assert plan.conflicts == ()

        result = apply_update(plan, conn=db_conn)

        assert result.local_edits_preserved == ("b.py",)
        assert _on_disk(installed.dest_path) == {
            "manifest.yaml": _manifest("demo", "0.2.0"),
            "a.py": "a-v2\n",
            "b.py": "b-MINE\n",
        }
        record = get_install_record(db_conn, "demo")
        # The merged tree is what is on disk; the pristine tree the new
        # version shipped is what the NEXT update anchors on.
        assert record["hash"] == compute_package_hash(installed.dest_path)
        assert record["hash"] != plan.fetched_hash
        assert record["shipped_hash"] == plan.fetched_hash

    def test_a_file_the_operator_added_is_kept(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        installed = _install(db_conn, tmp_path)
        (installed.dest_path / "mine.py").write_text("local\n")
        _serve(stub, tmp_path, files={"a.py": "a-v2\n", "b.py": "b-v1\n"})

        plan = plan_update("demo", conn=db_conn)
        assert plan.local_edits == ("mine.py",)
        apply_update(plan, conn=db_conn)
        assert (installed.dest_path / "mine.py").read_text() == "local\n"

    def test_second_update_still_sees_the_surviving_edit(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        """The anchor must be the shipped tree, not the merged one.

        Anchoring the next update on the merged tree would make the
        surviving edit look like part of the shipped version, so an
        upstream change to that file would be applied as
        ``UPSTREAM_ONLY`` — clobbering the edit with no conflict
        reported.
        """
        installed = _install(db_conn, tmp_path)
        (installed.dest_path / "b.py").write_text("b-MINE\n")
        _serve(stub, tmp_path, files={"a.py": "a-v2\n", "b.py": "b-v1\n"})
        apply_update(plan_update("demo", conn=db_conn), conn=db_conn)

        # v3 changes b.py, which the operator also changed.
        _serve(stub, tmp_path, version="0.3.0", files={
            "a.py": "a-v2\n", "b.py": "b-v3\n",
        })
        plan = plan_update("demo", conn=db_conn)
        assert plan.strategy == STRATEGY_RECONCILE
        assert plan.conflicts == ("b.py",)
        assert (installed.dest_path / "b.py").read_text() == "b-MINE\n"


# ── conflicts ───────────────────────────────────────────────────────


class TestConflicts:
    def _conflicting(self, db_conn, stub, tmp_path):
        installed = _install(db_conn, tmp_path)
        (installed.dest_path / "a.py").write_text("a-MINE\n")
        _serve(stub, tmp_path, files={"a.py": "a-v2\n", "b.py": "b-v1\n"})
        return installed

    def test_refused_without_accept_conflicts(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        installed = self._conflicting(db_conn, stub, tmp_path)
        before = dict(get_install_record(db_conn, "demo"))
        plan = plan_update("demo", conn=db_conn)
        assert plan.conflicts == ("a.py",)
        assert any("CONFLICT" in n for n in plan.notes)

        with pytest.raises(UpdateError) as exc:
            apply_update(plan, conn=db_conn)
        assert "accept_conflicts" in str(exc.value)
        assert "a.py" in str(exc.value)

        # Nothing moved, and the plan is still applicable.
        assert (installed.dest_path / "a.py").read_text() == "a-MINE\n"
        assert dict(get_install_record(db_conn, "demo")) == before
        assert plan.staging is not None and plan.staging.is_dir()

        result = apply_update(plan, conn=db_conn, accept_conflicts=True)
        assert result.conflicts_overridden == ("a.py",)
        assert (installed.dest_path / "a.py").read_text() == "a-v2\n"
        cleanup_plan(plan)

    def test_accepting_takes_the_new_version(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        installed = self._conflicting(db_conn, stub, tmp_path)
        plan = plan_update("demo", conn=db_conn)
        result = apply_update(plan, conn=db_conn, accept_conflicts=True)
        assert result.conflicts_overridden == ("a.py",)
        assert _on_disk(installed.dest_path) == {
            "manifest.yaml": _manifest("demo", "0.2.0"),
            "a.py": "a-v2\n",
            "b.py": "b-v1\n",
        }

    def test_a_tree_changed_after_planning_is_refused(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        installed = _install(db_conn, tmp_path)
        _serve(stub, tmp_path, files={"a.py": "a-v2\n", "b.py": "b-v1\n"})
        plan = plan_update("demo", conn=db_conn)
        # The operator edits the tree between plan and apply.
        (installed.dest_path / "b.py").write_text("b-MINE\n")

        with pytest.raises(UpdateError, match="changed after this plan"):
            apply_update(plan, conn=db_conn)
        assert (installed.dest_path / "b.py").read_text() == "b-MINE\n"
        cleanup_plan(plan)


# ── no pristine anchor: the clobber fallback ────────────────────────


class TestClobberFallback:
    def _no_anchor(self, base_dir, db_conn, stub, tmp_path):
        installed = _install(db_conn, tmp_path)
        shutil.rmtree(base_dir / "cache" / "package-archives")
        (installed.dest_path / "b.py").write_text("b-MINE\n")
        _serve(stub, tmp_path, files={"a.py": "a-v2\n", "b.py": "b-v2\n"})
        return installed

    def test_plan_says_what_will_be_lost_and_changes_nothing(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        installed = self._no_anchor(base_dir, db_conn, stub, tmp_path)
        before_disk = _on_disk(installed.dest_path)
        before_row = dict(get_install_record(db_conn, "demo"))

        plan = plan_update("demo", conn=db_conn)

        assert plan.strategy == STRATEGY_CLOBBER
        # Without the anchor, an edit cannot be told from a shipped file.
        assert plan.local_edits == ()
        joined = " ".join(plan.notes)
        assert "LOCAL EDITS WILL BE LOST" in joined
        assert "a.py" in joined and "b.py" in joined
        assert "OVERWRITTEN" in joined
        # Planning alone writes nothing.
        assert _on_disk(installed.dest_path) == before_disk
        assert dict(get_install_record(db_conn, "demo")) == before_row
        cleanup_plan(plan)

    def test_apply_replaces_the_tree_and_reports_it(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        installed = self._no_anchor(base_dir, db_conn, stub, tmp_path)
        plan = plan_update("demo", conn=db_conn)
        result = apply_update(plan, conn=db_conn)

        assert result.strategy == STRATEGY_CLOBBER
        assert set(result.overwritten) == {"manifest.yaml", "a.py", "b.py"}
        assert _on_disk(installed.dest_path) == {
            "manifest.yaml": _manifest("demo", "0.2.0"),
            "a.py": "a-v2\n",
            "b.py": "b-v2\n",
        }
        record = get_install_record(db_conn, "demo")
        assert record["version"] == "0.2.0"
        assert record["hash"] == plan.fetched_hash
        # The clobber re-seeds the anchor, so the next update can merge.
        assert result.archive_cached is True

    def test_an_unverifiable_archive_also_falls_back(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        """A cached archive that is not the installed tree is no anchor."""
        _install(db_conn, tmp_path)
        other = _write_tree(
            tmp_path / "wrong", "demo", "0.1.0", {"x.py": "x\n"},
        )
        sid = source_id(_spec())
        cached = (
            base_dir / "cache" / "package-archives" / "demo" / sid
            / "0.1.0.tar.gz"
        )
        archive_cache.archive_tree(other, cached)
        _serve(stub, tmp_path, files={"a.py": "a-v2\n"})

        plan = plan_update("demo", conn=db_conn)
        assert plan.strategy == STRATEGY_CLOBBER
        assert any("NO PRISTINE ANCHOR" in n for n in plan.notes)
        cleanup_plan(plan)


# ── no-op ───────────────────────────────────────────────────────────


class TestNoop:
    def test_hash_equal_writes_nothing(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        installed = _install(db_conn, tmp_path)
        # The remote ships exactly the installed bytes.
        stub.serve = installed.source_path
        before_row = dict(get_install_record(db_conn, "demo"))
        before_disk = _on_disk(installed.dest_path)

        plan = plan_update("demo", conn=db_conn)
        assert plan.strategy == STRATEGY_NOOP
        assert plan.is_noop
        assert any("UP TO DATE" in n for n in plan.notes)

        result = apply_update(plan, conn=db_conn)
        assert result.changed is False
        assert dict(get_install_record(db_conn, "demo")) == before_row
        assert _on_disk(installed.dest_path) == before_disk

    def test_a_noop_leaves_local_edits_alone(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        installed = _install(db_conn, tmp_path)
        stub.serve = installed.source_path
        (installed.dest_path / "b.py").write_text("b-MINE\n")

        plan = plan_update("demo", conn=db_conn)
        assert plan.strategy == STRATEGY_NOOP
        assert any("local modifications" in n for n in plan.notes)
        apply_update(plan, conn=db_conn)
        assert (installed.dest_path / "b.py").read_text() == "b-MINE\n"


# ── version sanity ──────────────────────────────────────────────────


class TestVersionSanity:
    def test_same_version_different_bytes_is_reported(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        installed = _install(db_conn, tmp_path)
        _serve(stub, tmp_path, version="0.1.0", files={
            "a.py": "a-CHANGED\n", "b.py": "b-v1\n",
        })

        plan = plan_update("demo", conn=db_conn)
        assert plan.installed_version == plan.fetched_version == "0.1.0"
        assert plan.strategy == STRATEGY_RECONCILE
        assert any("SAME VERSION, DIFFERENT BYTES" in n for n in plan.notes)

        old_pristine = get_install_record(db_conn, "demo")["shipped_hash"]
        apply_update(plan, conn=db_conn)

        # The pristine archive of the bytes that WERE installed is kept
        # rather than overwritten, so that anchor is not destroyed.
        sid = source_id(_spec())
        cache = base_dir / "cache" / "package-archives" / "demo" / sid
        superseded = list(cache.glob("0.1.0.superseded-*.tar.gz"))
        assert len(superseded) == 1
        assert archive_cache._archive_root_hash(superseded[0]) == old_pristine
        # And both are still reachable by content.
        assert load_pristine_tree(
            "demo", "0.1.0", old_pristine, source_id=sid,
        )["a.py"] == b"a-v1\n"
        assert load_pristine_tree(
            "demo", "0.1.0", plan.fetched_hash, source_id=sid,
        )["a.py"] == b"a-CHANGED\n"
        assert installed.dest_path.is_dir()

    def test_a_backwards_version_string_is_only_reported(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        _install(db_conn, tmp_path, version="0.2.0")
        _serve(stub, tmp_path, version="0.1.9", files={"a.py": "older\n"})

        plan = plan_update("demo", conn=db_conn)
        assert any("POSSIBLE DOWNGRADE" in n for n in plan.notes)
        # Reported, never enforced: it applies if the operator says so.
        result = apply_update(plan, conn=db_conn)
        assert result.to_version == "0.1.9"


# ── refusals ────────────────────────────────────────────────────────


class TestRefusals:
    def test_not_installed(self, base_dir, db_conn):
        with pytest.raises(UpdateError, match="not installed"):
            plan_update("absent", conn=db_conn)

    def test_no_recorded_source(self, base_dir, db_conn, tmp_path):
        _install(db_conn, tmp_path, provenance=None)
        with pytest.raises(UpdateError) as exc:
            plan_update("demo", conn=db_conn)
        message = str(exc.value)
        assert "no recorded source" in message
        assert "packages install" in message

    def test_a_local_source_is_refused_with_the_alternative(
        self, base_dir, db_conn, tmp_path,
    ):
        src = _write_tree(
            tmp_path / "local-src", "demo", "0.1.0", {"a.py": "a\n"},
        )
        install_package(
            src, default_install_dest("demo"), conn=db_conn,
            provenance=SourceSpec(kind="local", raw=str(src), path=src),
        )
        with pytest.raises(UpdateError) as exc:
            plan_update("demo", conn=db_conn)
        assert "--path" in str(exc.value)

    def test_a_redacted_url_cannot_be_refetched(
        self, base_dir, db_conn, tmp_path,
    ):
        credentialled = f"https://user:tok@{HOST}/org/pkgs"
        _install(
            db_conn, tmp_path,
            provenance=_spec(url=credentialled),
        )
        assert "***" in get_install_record(db_conn, "demo")["source_url"]
        with pytest.raises(UpdateError, match="redacted"):
            plan_update("demo", conn=db_conn)

    def test_a_missing_install_directory_is_refused(
        self, base_dir, db_conn, tmp_path,
    ):
        installed = _install(db_conn, tmp_path)
        shutil.rmtree(installed.dest_path)
        with pytest.raises(UpdateError, match="does not exist"):
            plan_update("demo", conn=db_conn)

    def test_a_renamed_package_is_refused(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        _install(db_conn, tmp_path)
        _serve(stub, tmp_path, name="other", files={"a.py": "a\n"})
        with pytest.raises(UpdateError, match="now publishes a package"):
            plan_update("demo", conn=db_conn)

    def test_a_kind_with_no_fetcher_is_refused(
        self, base_dir, db_conn, tmp_path,
    ):
        _install(db_conn, tmp_path)
        with pytest.raises(UpdateError, match="could not fetch"):
            plan_update("demo", conn=db_conn)


# ── a failed fetch ──────────────────────────────────────────────────


class TestFetchFailure:
    def test_nothing_is_touched(self, base_dir, db_conn, stub, tmp_path):
        installed = _install(db_conn, tmp_path)
        before_disk = _on_disk(installed.dest_path)
        before_row = dict(get_install_record(db_conn, "demo"))
        stub.fail_with = FetchError("the remote hung up")

        with pytest.raises(UpdateError) as exc:
            plan_update("demo", conn=db_conn)
        assert "could not fetch" in str(exc.value)
        assert "hung up" in str(exc.value)

        assert _on_disk(installed.dest_path) == before_disk
        assert dict(get_install_record(db_conn, "demo")) == before_row
        # The dispatcher removed the quarantine it created.
        staging = base_dir / "cache" / "package-staging"
        assert not staging.exists() or list(staging.iterdir()) == []


# ── a failed apply ──────────────────────────────────────────────────


class TestFailedApply:
    def test_db_row_and_tree_are_untouched(
        self, base_dir, db_conn, stub, tmp_path, monkeypatch,
    ):
        installed = _install(db_conn, tmp_path)
        (installed.dest_path / "b.py").write_text("b-MINE\n")
        _serve(stub, tmp_path, files={"a.py": "a-v2\n", "b.py": "b-v1\n"})
        plan = plan_update("demo", conn=db_conn)
        before_disk = _on_disk(installed.dest_path)
        before_row = dict(get_install_record(db_conn, "demo"))

        def _boom(source, dest):
            raise InstallError("atomic swap failed")

        monkeypatch.setattr(
            "carpenter.packages.reconcile_apply._atomic_copy_into_place",
            _boom,
        )
        with pytest.raises(UpdateError, match="failed"):
            apply_update(plan, conn=db_conn)

        assert _on_disk(installed.dest_path) == before_disk
        assert dict(get_install_record(db_conn, "demo")) == before_row
        cleanup_plan(plan)

    def test_a_vanished_quarantine_is_refused(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        _install(db_conn, tmp_path)
        _serve(stub, tmp_path, files={"a.py": "a-v2\n"})
        plan = plan_update("demo", conn=db_conn)
        cleanup_plan(plan)
        with pytest.raises(UpdateError, match="is gone"):
            apply_update(plan, conn=db_conn)

    def test_swapped_fetched_bytes_are_refused(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        """What the operator approved is what gets installed."""
        _install(db_conn, tmp_path)
        _serve(stub, tmp_path, files={"a.py": "a-v2\n"})
        plan = plan_update("demo", conn=db_conn)
        (plan.fetched_tree / "a.py").write_text("a-SWAPPED\n")
        with pytest.raises(UpdateError, match="no longer hashes"):
            apply_update(plan, conn=db_conn)
        cleanup_plan(plan)

    def test_an_uninstalled_package_cannot_be_applied_to(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        _install(db_conn, tmp_path)
        _serve(stub, tmp_path, files={"a.py": "a-v2\n"})
        plan = plan_update("demo", conn=db_conn)
        db_conn.execute("DELETE FROM installed_packages WHERE name = 'demo'")
        with pytest.raises(UpdateError, match="no longer installed"):
            apply_update(plan, conn=db_conn)
        cleanup_plan(plan)


# ── the archive cache's source discriminator ────────────────────────


class TestArchiveCacheSourceDiscriminator:
    def test_two_sources_same_name_and_version_do_not_collide(
        self, base_dir, tmp_path,
    ):
        one = _write_tree(tmp_path / "one", "demo", "1.0.0", {"x.py": "one\n"})
        two = _write_tree(tmp_path / "two", "demo", "1.0.0", {"x.py": "two\n"})
        id_one = source_id(_spec(url=URL))
        id_two = source_id(_spec(url=OTHER_URL))
        assert id_one != id_two

        path_one = store_archive("demo", "1.0.0", one, source_id=id_one)
        path_two = store_archive("demo", "1.0.0", two, source_id=id_two)
        assert path_one != path_two
        assert path_one.parent.name == id_one
        assert path_two.parent.name == id_two

        hash_one = compute_package_hash(one)
        hash_two = compute_package_hash(two)
        assert load_pristine_tree(
            "demo", "1.0.0", hash_one, source_id=id_one,
        )["x.py"] == b"one\n"
        assert load_pristine_tree(
            "demo", "1.0.0", hash_two, source_id=id_two,
        )["x.py"] == b"two\n"
        # And one source's id never serves the other's bytes.
        with pytest.raises(ArchiveVerificationError):
            load_pristine_tree(
                "demo", "1.0.0", hash_one, source_id=id_two,
            )

    def test_an_old_layout_archive_is_still_read(self, base_dir, tmp_path):
        """Archives written before the source_id level still load."""
        tree = _write_tree(tmp_path / "old", "demo", "1.0.0", {"x.py": "old\n"})
        legacy = store_archive("demo", "1.0.0", tree)  # no source_id
        assert legacy == (
            base_dir / "cache" / "package-archives" / "demo" / "1.0.0.tar.gz"
        )
        loaded = load_pristine_tree(
            "demo", "1.0.0", compute_package_hash(tree),
            source_id=source_id(_spec()),
        )
        assert loaded["x.py"] == b"old\n"

    def test_an_update_reconciles_against_an_old_layout_archive(
        self, base_dir, db_conn, stub, tmp_path,
    ):
        installed = _install(db_conn, tmp_path)
        # Move the install's archive down to the old, source-blind path.
        sid = source_id(_spec())
        pkg_cache = base_dir / "cache" / "package-archives" / "demo"
        shutil.move(
            str(pkg_cache / sid / "0.1.0.tar.gz"),
            str(pkg_cache / "0.1.0.tar.gz"),
        )
        shutil.rmtree(pkg_cache / sid)
        (installed.dest_path / "b.py").write_text("b-MINE\n")
        _serve(stub, tmp_path, files={"a.py": "a-v2\n", "b.py": "b-v1\n"})

        plan = plan_update("demo", conn=db_conn)
        assert plan.strategy == STRATEGY_RECONCILE
        assert plan.local_edits == ("b.py",)
        apply_update(plan, conn=db_conn)
        assert (installed.dest_path / "b.py").read_text() == "b-MINE\n"


# ── module surface ──────────────────────────────────────────────────


def test_public_surface_is_re_exported():
    from carpenter import packages

    for symbol in update.__all__:
        assert hasattr(update, symbol)
    for symbol in (
        "plan_update", "apply_update", "UpdatePlan", "UpdateResult",
        "UpdateError", "cleanup_plan",
    ):
        assert hasattr(packages, symbol)


# ── the CLI verb ────────────────────────────────────────────────────


class TestCliUpdate:
    """``python3 -m carpenter packages update`` end to end.

    Uses the real DB path (the ``test_db`` fixture) because the verb
    opens its own connection and transaction, exactly as an operator
    run does.
    """

    @pytest.fixture(autouse=True)
    def _cli_env(self, test_db, monkeypatch):
        from carpenter import config
        from carpenter.db import db_transaction
        from carpenter.packages import installer

        monkeypatch.setitem(config.CONFIG, "web_egress_allowlist", [HOST])
        with db_transaction() as db:
            installer.ensure_installer_tables(db)

    def _install_via_api(self, tmp_path, *, files=None, version="0.1.0"):
        from carpenter.db import db_transaction

        with db_transaction() as db:
            return _install(
                db, tmp_path, version=version, files=files,
            )

    def test_yes_applies_and_reports_the_plan(
        self, stub, tmp_path, capsys, monkeypatch,
    ):
        from carpenter import cli_packages

        installed = self._install_via_api(tmp_path)
        (installed.dest_path / "b.py").write_text("b-MINE\n")
        _serve(stub, tmp_path, files={"a.py": "a-v2\n", "b.py": "b-v1\n"})
        monkeypatch.setattr("builtins.input", lambda *a, **k: pytest.fail(
            "--yes must not prompt"))

        rc = cli_packages.cmd_packages(["update", "demo", "--yes"])

        captured = capsys.readouterr()
        assert rc == 0, captured.err
        assert "Strategy : reconcile" in captured.err
        assert "Your changes (1)" in captured.err
        assert "Updated demo 0.1.0 -> 0.2.0" in captured.out
        assert "Your edits were KEPT" in captured.out
        assert "Restart needed:" in captured.out
        assert (installed.dest_path / "b.py").read_text() == "b-MINE\n"

    def test_declining_at_the_prompt_changes_nothing(
        self, stub, tmp_path, capsys, monkeypatch,
    ):
        from carpenter import cli_packages

        installed = self._install_via_api(tmp_path)
        _serve(stub, tmp_path, files={"a.py": "a-v2\n"})
        monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "no")

        rc = cli_packages.cmd_packages(["update", "demo"])

        assert rc == 0
        assert "declined" in capsys.readouterr().out
        assert (installed.dest_path / "a.py").read_text() == "a-v1\n"

    def test_non_interactive_without_yes_is_refused(
        self, stub, tmp_path, capsys, monkeypatch,
    ):
        from carpenter import cli_packages

        installed = self._install_via_api(tmp_path)
        _serve(stub, tmp_path, files={"a.py": "a-v2\n"})
        monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)

        rc = cli_packages.cmd_packages(["update", "demo"])

        assert rc == 1
        assert "--yes" in capsys.readouterr().err
        assert (installed.dest_path / "a.py").read_text() == "a-v1\n"

    def test_conflicts_are_refused_until_accepted(
        self, stub, tmp_path, capsys, monkeypatch,
    ):
        from carpenter import cli_packages

        installed = self._install_via_api(tmp_path)
        (installed.dest_path / "a.py").write_text("a-MINE\n")
        _serve(stub, tmp_path, files={"a.py": "a-v2\n", "b.py": "b-v1\n"})
        monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)

        rc = cli_packages.cmd_packages(["update", "demo", "--yes"])
        err = capsys.readouterr().err
        assert rc == 1
        assert "--accept-conflicts" in err
        assert (installed.dest_path / "a.py").read_text() == "a-MINE\n"

        _serve(stub, tmp_path, files={"a.py": "a-v2\n", "b.py": "b-v1\n"})
        rc = cli_packages.cmd_packages(
            ["update", "demo", "--yes", "--accept-conflicts"],
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "DISCARDED in favour of the new version" in out
        assert (installed.dest_path / "a.py").read_text() == "a-v2\n"

    def test_up_to_date_reports_and_exits_zero(
        self, stub, tmp_path, capsys, monkeypatch,
    ):
        from carpenter import cli_packages

        installed = self._install_via_api(tmp_path)
        stub.serve = installed.source_path
        monkeypatch.setattr("builtins.input", lambda *a, **k: pytest.fail(
            "a no-op must not prompt"))

        rc = cli_packages.cmd_packages(["update", "demo"])

        captured = capsys.readouterr()
        assert rc == 0
        assert "already up to date" in captured.out
        assert "Strategy : noop" in captured.err

    def test_an_unupdatable_package_is_refused(self, tmp_path, capsys):
        from carpenter import cli_packages
        from carpenter.db import db_transaction

        with db_transaction() as db:
            _install(db, tmp_path, provenance=None)

        rc = cli_packages.cmd_packages(["update", "demo", "--yes"])
        assert rc == 1
        assert "no recorded source" in capsys.readouterr().err

    def test_the_quarantine_is_always_cleaned_up(
        self, stub, tmp_path, monkeypatch,
    ):
        from carpenter import cli_packages
        from carpenter import config

        self._install_via_api(tmp_path)
        _serve(stub, tmp_path, files={"a.py": "a-v2\n"})
        monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)

        assert cli_packages.cmd_packages(["update", "demo"]) == 1

        staging = (
            Path(config.CONFIG["base_dir"]) / "cache" / "package-staging"
        )
        assert not staging.exists() or list(staging.iterdir()) == []
