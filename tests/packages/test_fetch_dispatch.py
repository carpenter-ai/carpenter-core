"""Tests for the fetch dispatcher, its contract, and the quarantine root.

WP1 ships only the local-directory fetcher, so the remote side is
exercised with a stub registered through the same public
:func:`register_fetcher` seam a git or archive fetcher will use.  That
keeps the contract — a fresh, empty, per-call quarantine directory in,
a validated :class:`FetchResult` out — under test before there is an
implementation to break it.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import ClassVar

import pytest

from carpenter import config
from carpenter.packages import fetch
from carpenter.packages.fetch import (
    FetchError,
    FetchLimits,
    FetchResult,
    Fetcher,
    cleanup,
    fetch_source,
    register_fetcher,
    staging_root_dir,
)
from carpenter.packages.sources import SourceSpec, parse_source
from carpenter.security import egress

from .conftest import write_package

MANIFEST = dedent("""\
    name: demo
    version: "0.1.0"
    description: A fetch-dispatch fixture package.
""")


@pytest.fixture
def isolated_fetchers():
    """Restore the fetcher registry after a test registers a stub."""
    saved = dict(fetch._FETCHERS)
    yield
    fetch._FETCHERS.clear()
    fetch._FETCHERS.update(saved)


@pytest.fixture
def local_pkg(tmp_path) -> Path:
    """A local package directory holding ``manifest.yaml``."""
    root = tmp_path / "src"
    root.mkdir()
    return write_package(root, name="demo", manifest_yaml=MANIFEST)


# ── limits and result shapes ────────────────────────────────────────


class TestFetchLimits:
    def test_defaults(self):
        limits = FetchLimits()
        assert limits.max_bytes == 64 * 1024 * 1024
        assert limits.max_files == 5000
        assert limits.max_file_bytes == 16 * 1024 * 1024
        assert limits.timeout_s == 60.0

    def test_frozen(self):
        with pytest.raises(Exception):
            FetchLimits().max_bytes = 1  # type: ignore[misc]


def test_local_fetcher_satisfies_the_protocol():
    assert isinstance(fetch.LocalDirectoryFetcher(), Fetcher)


# ── the local fetcher ───────────────────────────────────────────────


class TestLocalFetch:
    def test_directory_is_used_in_place(self, local_pkg):
        result = fetch_source(parse_source(str(local_pkg)))
        assert result.tree == local_pkg.resolve()
        assert result.staging is None
        assert result.resolved == ""
        assert (result.bytes_fetched, result.files_fetched) == (0, 0)

    def test_subdir_selects_the_package_root(self, tmp_path):
        root = tmp_path / "monorepo"
        write_package(root / "packages", name="demo", manifest_yaml=MANIFEST)
        spec = SourceSpec(
            kind="local", raw=str(root), path=root,
            subdir="packages/demo",
        )
        assert fetch_source(spec).tree == root / "packages" / "demo"

    def test_missing_directory_refused(self, tmp_path):
        spec = parse_source(str(tmp_path / "absent"))
        with pytest.raises(FetchError, match="does not exist"):
            fetch_source(spec)

    def test_directory_without_a_manifest_refused(self, tmp_path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(FetchError, match="no manifest.yaml"):
            fetch_source(parse_source(str(tmp_path / "empty")))

    def test_cleanup_never_removes_the_operators_tree(self, local_pkg):
        result = fetch_source(parse_source(str(local_pkg)))
        cleanup(result)
        cleanup(result)  # idempotent
        assert (local_pkg / "manifest.yaml").is_file()

    def test_local_fetch_needs_no_staging_root(self, local_pkg, monkeypatch):
        """A local install must not depend on base_dir being set."""
        monkeypatch.setitem(config.CONFIG, "base_dir", "")
        assert fetch_source(parse_source(str(local_pkg))).staging is None


# ── dispatch ────────────────────────────────────────────────────────


class _StubRemoteFetcher:
    """Writes a package into the quarantine it is handed."""

    kinds: ClassVar[frozenset[str]] = frozenset({"git"})

    def __init__(self, *, resolved: str = "c" * 40, escape_to: Path | None = None):
        self.resolved = resolved
        self.escape_to = escape_to
        self.calls: list[tuple[SourceSpec, Path, FetchLimits]] = []

    def fetch(self, spec, dest, *, limits):
        self.calls.append((spec, dest, limits))
        if self.escape_to is not None:
            write_package(
                self.escape_to, name="demo", manifest_yaml=MANIFEST,
            )
            tree = self.escape_to / "demo"
        else:
            tree = write_package(dest, name="demo", manifest_yaml=MANIFEST)
        return FetchResult(
            tree=tree, staging=dest, resolved=self.resolved,
            bytes_fetched=len(MANIFEST), files_fetched=1,
        )


class TestDispatch:
    def test_unregistered_kind_names_the_kind(self, isolated_fetchers):
        # Deregister everything: which kinds ship a fetcher is a moving
        # target, and the dispatcher's refusal is what is under test.  The
        # lookup happens before the egress guard, so this reaches no network.
        fetch._FETCHERS.clear()
        spec = parse_source("https://files.example/d/demo-1.0.tar.gz")
        with pytest.raises(FetchError, match="kind 'archive'"):
            fetch_source(spec)

    def test_registered_fetcher_gets_a_fresh_quarantine(
        self, isolated_fetchers, tmp_path,
    ):
        stub = _StubRemoteFetcher()
        register_fetcher(stub)
        staging_root = tmp_path / "staging"
        spec = parse_source("git+https://forge.example/org/repo.git@main")

        result = fetch_source(spec, staging_root=staging_root)

        assert result.resolved == "c" * 40
        assert result.staging is not None
        assert result.staging.parent == staging_root
        assert result.tree.is_relative_to(result.staging)
        assert (result.tree / "manifest.yaml").is_file()
        assert result.files_fetched == 1
        # The fetcher saw the spec, its own empty dest, and the limits.
        got_spec, got_dest, got_limits = stub.calls[0]
        assert got_spec is spec
        assert got_dest == result.staging
        assert got_limits == FetchLimits()

    def test_custom_limits_are_passed_through(
        self, isolated_fetchers, tmp_path,
    ):
        stub = _StubRemoteFetcher()
        register_fetcher(stub)
        limits = FetchLimits(max_bytes=1024, max_files=2, timeout_s=1.0)
        fetch_source(
            parse_source("git+https://forge.example/org/repo.git"),
            limits=limits, staging_root=tmp_path / "staging",
        )
        assert stub.calls[0][2] is limits

    def test_quarantine_dirs_are_per_call(self, isolated_fetchers, tmp_path):
        register_fetcher(_StubRemoteFetcher())
        spec = parse_source("git+https://forge.example/org/repo.git")
        staging_root = tmp_path / "staging"
        first = fetch_source(spec, staging_root=staging_root)
        second = fetch_source(spec, staging_root=staging_root)
        assert first.staging != second.staging
        assert first.staging.is_dir() and second.staging.is_dir()

    def test_cleanup_removes_only_that_quarantine(
        self, isolated_fetchers, tmp_path,
    ):
        register_fetcher(_StubRemoteFetcher())
        spec = parse_source("git+https://forge.example/org/repo.git")
        staging_root = tmp_path / "staging"
        keep = fetch_source(spec, staging_root=staging_root)
        drop = fetch_source(spec, staging_root=staging_root)
        cleanup(drop)
        assert not drop.staging.exists()
        assert keep.staging.is_dir()
        assert staging_root.is_dir()

    def test_failed_fetch_leaves_no_quarantine(
        self, isolated_fetchers, tmp_path,
    ):
        class _Boom:
            kinds: ClassVar[frozenset[str]] = frozenset({"git"})

            def fetch(self, spec, dest, *, limits):
                (dest / "partial.bin").write_bytes(b"half a download")
                raise FetchError("the remote hung up")

        register_fetcher(_Boom())
        staging_root = tmp_path / "staging"
        with pytest.raises(FetchError, match="hung up"):
            fetch_source(
                parse_source("git+https://forge.example/org/repo.git"),
                staging_root=staging_root,
            )
        assert list(staging_root.iterdir()) == []

    def test_tree_outside_the_quarantine_refused(
        self, isolated_fetchers, tmp_path,
    ):
        escape = tmp_path / "elsewhere"
        escape.mkdir()
        register_fetcher(_StubRemoteFetcher(escape_to=escape))
        staging_root = tmp_path / "staging"
        with pytest.raises(FetchError, match="outside its staging directory"):
            fetch_source(
                parse_source("git+https://forge.example/org/repo.git"),
                staging_root=staging_root,
            )
        assert list(staging_root.iterdir()) == []

    def test_non_result_return_refused(self, isolated_fetchers, tmp_path):
        class _Wrong:
            kinds: ClassVar[frozenset[str]] = frozenset({"git"})

            def fetch(self, spec, dest, *, limits):
                return dest

        register_fetcher(_Wrong())
        with pytest.raises(FetchError, match="not a FetchResult"):
            fetch_source(
                parse_source("git+https://forge.example/org/repo.git"),
                staging_root=tmp_path / "staging",
            )


# ── the egress gate ─────────────────────────────────────────────────


def _resolver(mapping):
    """A hermetic stand-in for ``egress._resolve_host`` (no DNS).

    The guard exposes ``_resolve_host`` as a module-level indirection for
    exactly this; the fetch gate must be testable without adding a
    bypass of its own.
    """
    def _resolve(host, port):
        try:
            return mapping[host.lower()]
        except KeyError:
            raise OSError(f"unknown host {host}") from None
    return _resolve


class _StubKindFetcher:
    """A stub registered for a kind no real fetcher handles.

    The gate lives in the dispatcher, so it must apply to a kind that
    did not exist when it was written — not just to git and archive.
    """

    kinds: ClassVar[frozenset[str]] = frozenset({"probe"})

    def __init__(self):
        self.calls: list[SourceSpec] = []

    def fetch(self, spec, dest, *, limits):
        self.calls.append(spec)
        tree = write_package(dest, name="demo", manifest_yaml=MANIFEST)
        return FetchResult(
            tree=tree, staging=dest, resolved="",
            bytes_fetched=len(MANIFEST), files_fetched=1,
        )


PROBE_HOST = "forge.probe.invalid"
PROBE_URL = f"https://{PROBE_HOST}/org/pkgs.tar.gz"


def _probe_spec() -> SourceSpec:
    return SourceSpec(kind="probe", raw=PROBE_URL, url=PROBE_URL)


class TestEgressGate:
    """Every non-local fetch passes the platform's egress guard first."""

    @pytest.fixture
    def private_host(self, monkeypatch):
        """``PROBE_HOST`` resolves to a non-public address, hermetically."""
        monkeypatch.setattr(
            egress, "_resolve_host", _resolver({PROBE_HOST: ["127.0.0.1"]}),
        )

    def test_non_public_destination_is_refused(
        self, isolated_fetchers, private_host, tmp_path,
    ):
        stub = _StubKindFetcher()
        register_fetcher(stub)
        staging_root = tmp_path / "staging"

        with pytest.raises(FetchError) as excinfo:
            fetch_source(_probe_spec(), staging_root=staging_root)

        message = str(excinfo.value)
        assert PROBE_HOST in message
        assert "web_egress_allowlist" in message
        assert "An operator must add the host" in message
        # The guard's refusal is the cause, and the fetcher never ran.
        assert isinstance(excinfo.value.__cause__, egress.EgressDenied)
        assert stub.calls == []
        assert not staging_root.exists()

    def test_allowlisted_host_reaches_the_fetcher(
        self, isolated_fetchers, private_host, monkeypatch, tmp_path,
    ):
        stub = _StubKindFetcher()
        register_fetcher(stub)
        monkeypatch.setitem(
            config.CONFIG, "web_egress_allowlist", [PROBE_HOST],
        )

        result = fetch_source(_probe_spec(), staging_root=tmp_path / "staging")

        assert [s.url for s in stub.calls] == [PROBE_URL]
        assert (result.tree / "manifest.yaml").is_file()

    def test_a_local_spec_never_consults_the_guard(
        self, local_pkg, monkeypatch,
    ):
        checked: list[str] = []

        def _refuse_everything(url):
            checked.append(url)
            raise egress.EgressDenied("the guard was consulted")

        monkeypatch.setattr(egress, "check_url", _refuse_everything)

        result = fetch_source(parse_source(str(local_pkg)))

        assert result.tree == local_pkg.resolve()
        assert checked == []

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8000/d/demo.tar.gz",
        "http://localhost:8000/d/demo.tar.gz",
        "http://[::1]:8000/d/demo.tar.gz",
    ])
    def test_loopback_forms_work_once_allowlisted(
        self, isolated_fetchers, monkeypatch, tmp_path, url,
    ):
        """The grammar's loopback exception survives the gate.

        It is not exempt from it: a test server on loopback is reachable
        because the operator listed loopback, like any other non-public
        destination.
        """
        class _LoopbackStub(_StubKindFetcher):
            kinds: ClassVar[frozenset[str]] = frozenset({"archive"})

        stub = _LoopbackStub()
        register_fetcher(stub)
        monkeypatch.setitem(
            config.CONFIG, "web_egress_allowlist",
            ["127.0.0.1", "localhost", "::1"],
        )

        result = fetch_source(
            parse_source(url), staging_root=tmp_path / "staging",
        )

        assert len(stub.calls) == 1
        assert (result.tree / "manifest.yaml").is_file()

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8000/d/demo.tar.gz",
        "http://[::1]:8000/d/demo.tar.gz",
    ])
    def test_loopback_is_refused_until_allowlisted(
        self, isolated_fetchers, tmp_path, url,
    ):
        class _LoopbackStub(_StubKindFetcher):
            kinds: ClassVar[frozenset[str]] = frozenset({"archive"})

        stub = _LoopbackStub()
        register_fetcher(stub)

        with pytest.raises(FetchError, match="web_egress_allowlist"):
            fetch_source(parse_source(url), staging_root=tmp_path / "staging")
        assert stub.calls == []

    def test_a_remote_spec_without_a_url_is_refused(
        self, isolated_fetchers, tmp_path,
    ):
        register_fetcher(_StubKindFetcher())
        spec = SourceSpec(kind="probe", raw="probe", url=None)
        with pytest.raises(FetchError, match="has no URL"):
            fetch_source(spec, staging_root=tmp_path / "staging")


class TestRegisterFetcher:
    def test_registers_every_declared_kind(self, isolated_fetchers, tmp_path):
        class _Both:
            kinds: ClassVar[frozenset[str]] = frozenset({"git", "archive"})

            def fetch(self, spec, dest, *, limits):
                return FetchResult(
                    tree=write_package(
                        dest, name="demo", manifest_yaml=MANIFEST,
                    ),
                    staging=dest, resolved="", bytes_fetched=0,
                    files_fetched=1,
                )

        register_fetcher(_Both())
        for spec in (
            "git+https://forge.example/o/r.git",
            "https://files.example/d/demo-1.0.tar.gz",
        ):
            result = fetch_source(
                parse_source(spec), staging_root=tmp_path / "staging",
            )
            assert result.tree.name == "demo"

    def test_later_registration_replaces_the_earlier(
        self, isolated_fetchers, tmp_path,
    ):
        first, second = _StubRemoteFetcher(), _StubRemoteFetcher()
        register_fetcher(first)
        register_fetcher(second)
        fetch_source(
            parse_source("git+https://forge.example/o/r.git"),
            staging_root=tmp_path / "staging",
        )
        assert not first.calls and second.calls

    def test_fetcher_without_kinds_refused(self):
        class _NoKinds:
            def fetch(self, spec, dest, *, limits):  # pragma: no cover
                ...

        with pytest.raises(FetchError, match="declares no 'kinds'"):
            register_fetcher(_NoKinds())

    def test_fetcher_without_fetch_refused(self):
        class _NoFetch:
            kinds: ClassVar[frozenset[str]] = frozenset({"git"})

        with pytest.raises(FetchError, match="no callable 'fetch'"):
            register_fetcher(_NoFetch())


# ── the quarantine root ─────────────────────────────────────────────


class TestStagingRoot:
    def test_location_and_mode(self):
        root = staging_root_dir()
        assert root == Path(config.CONFIG["base_dir"]) / "cache" / "package-staging"
        assert root.is_dir()
        assert root.stat().st_mode & 0o777 == 0o700

    def test_idempotent(self):
        assert staging_root_dir() == staging_root_dir()

    def test_requires_base_dir(self, monkeypatch):
        monkeypatch.setitem(config.CONFIG, "base_dir", "")
        with pytest.raises(FetchError, match="'base_dir' is not set"):
            staging_root_dir()

    def test_default_root_is_used_when_none_is_given(
        self, isolated_fetchers,
    ):
        register_fetcher(_StubRemoteFetcher())
        result = fetch_source(parse_source("git+https://forge.example/o/r.git"))
        try:
            assert result.staging.parent == staging_root_dir()
        finally:
            cleanup(result)

    def test_not_on_any_registry_search_path(self):
        """A quarantined tree must never be discoverable as a package.

        The staging root holds bytes that have not passed the manifest
        or security guards, so discovery finding one would defeat the
        whole point of quarantining it.
        """
        from carpenter.packages.registry import (
            PackageRegistry, default_search_paths,
        )

        staging = staging_root_dir()
        # A perfectly valid package, sitting in quarantine.
        write_package(staging, name="demo", manifest_yaml=MANIFEST)

        search_paths = default_search_paths()
        for path in search_paths:
            assert path.resolve() != staging.resolve()
            assert not staging.resolve().is_relative_to(path.resolve())

        found = PackageRegistry()._discover_manifests(search_paths)
        assert not [
            m for m, _ in found if staging.resolve() in m.resolve().parents
        ]


# ── the listing seam: require_manifest ──────────────────────────────


class _StubIndexFetcher:
    """Writes an *index root* — a directory OF packages — and returns it.

    This is the shape a monorepo listing needs and that an installer
    must never be handed: ``packages/`` holding two package directories
    and no ``manifest.yaml`` of its own.
    """

    kinds: ClassVar[frozenset[str]] = frozenset({"git"})

    def __init__(self, *, escape_to: Path | None = None):
        self.escape_to = escape_to
        self.manifest_required: list[bool] = []

    def fetch(self, spec, dest, *, limits, require_manifest=True):
        self.manifest_required.append(require_manifest)
        root = self.escape_to if self.escape_to is not None else dest / "tree"
        index = root / "packages"
        index.mkdir(parents=True)
        for name in ("alpha", "beta"):
            write_package(index, name=name, manifest_yaml=MANIFEST)
        return FetchResult(
            tree=index, staging=dest, resolved="d" * 40,
            bytes_fetched=2 * len(MANIFEST), files_fetched=2,
        )


class _LegacyFetcher:
    """A fetcher written before ``require_manifest`` existed.

    ``register_fetcher`` is a public seam a platform package may use, so
    an implementation whose ``fetch`` does not take the keyword has to
    keep working — the dispatcher must not turn every install through it
    into a ``TypeError``.
    """

    kinds: ClassVar[frozenset[str]] = frozenset({"git"})

    def __init__(self):
        self.calls = 0

    def fetch(self, spec, dest, *, limits):
        self.calls += 1
        return FetchResult(
            tree=write_package(dest, name="demo", manifest_yaml=MANIFEST),
            staging=dest, resolved="e" * 40,
            bytes_fetched=len(MANIFEST), files_fetched=1,
        )


GIT_URL = "git+https://forge.example/org/repo.git"


class TestRequireManifest:
    """The one check a listing fetch may turn off, and nothing else."""

    def test_the_default_refuses_an_index_root(
        self, isolated_fetchers, tmp_path,
    ):
        register_fetcher(_StubIndexFetcher())
        with pytest.raises(FetchError, match="no manifest.yaml"):
            fetch_source(
                parse_source(GIT_URL), staging_root=tmp_path / "staging",
            )

    def test_an_index_root_is_returned_when_it_is_not_required(
        self, isolated_fetchers, tmp_path,
    ):
        register_fetcher(_StubIndexFetcher())
        result = fetch_source(
            parse_source(GIT_URL), staging_root=tmp_path / "staging",
            require_manifest=False,
        )
        assert not (result.tree / "manifest.yaml").exists()
        assert sorted(p.name for p in result.tree.iterdir()) == [
            "alpha", "beta",
        ]
        assert result.resolved == "d" * 40

    def test_the_flag_reaches_the_fetcher(self, isolated_fetchers, tmp_path):
        stub = _StubIndexFetcher()
        register_fetcher(stub)
        staging_root = tmp_path / "staging"
        fetch_source(parse_source(GIT_URL), staging_root=staging_root,
                     require_manifest=False)
        with pytest.raises(FetchError):
            fetch_source(parse_source(GIT_URL), staging_root=staging_root)
        assert stub.manifest_required == [False, True]

    def test_the_quarantine_still_contains_the_tree(
        self, isolated_fetchers, tmp_path,
    ):
        """Relaxing the manifest check must not relax containment."""
        escape = tmp_path / "elsewhere"
        escape.mkdir()
        register_fetcher(_StubIndexFetcher(escape_to=escape))
        staging_root = tmp_path / "staging"
        with pytest.raises(FetchError, match="outside its staging directory"):
            fetch_source(parse_source(GIT_URL), staging_root=staging_root,
                         require_manifest=False)
        assert list(staging_root.iterdir()) == []

    def test_a_non_result_is_still_refused(self, isolated_fetchers, tmp_path):
        class _Wrong:
            kinds: ClassVar[frozenset[str]] = frozenset({"git"})

            def fetch(self, spec, dest, *, limits, require_manifest=True):
                return dest

        register_fetcher(_Wrong())
        with pytest.raises(FetchError, match="not a FetchResult"):
            fetch_source(parse_source(GIT_URL),
                         staging_root=tmp_path / "staging",
                         require_manifest=False)

    def test_a_tree_that_is_not_a_directory_is_still_refused(
        self, isolated_fetchers, tmp_path,
    ):
        class _File:
            kinds: ClassVar[frozenset[str]] = frozenset({"git"})

            def fetch(self, spec, dest, *, limits, require_manifest=True):
                target = dest / "not-a-dir"
                target.write_text("bytes\n")
                return FetchResult(tree=target, staging=dest, resolved="",
                                   bytes_fetched=6, files_fetched=1)

        register_fetcher(_File())
        with pytest.raises(FetchError, match="is not a directory"):
            fetch_source(parse_source(GIT_URL),
                         staging_root=tmp_path / "staging",
                         require_manifest=False)

    def test_a_failed_fetch_still_leaves_no_quarantine(
        self, isolated_fetchers, tmp_path,
    ):
        register_fetcher(_StubIndexFetcher())
        staging_root = tmp_path / "staging"
        with pytest.raises(FetchError):
            fetch_source(parse_source(GIT_URL), staging_root=staging_root)
        assert list(staging_root.iterdir()) == []

    def test_the_egress_gate_still_runs_first(
        self, isolated_fetchers, monkeypatch, tmp_path,
    ):
        """A listing fetch is an outbound request like any other."""
        stub = _StubIndexFetcher()
        register_fetcher(stub)
        monkeypatch.setattr(
            egress, "_resolve_host", _resolver({"forge.example": "127.0.0.1"}),
        )
        monkeypatch.setitem(config.CONFIG, "web_egress_allowlist", [])
        with pytest.raises(FetchError, match="web_egress_allowlist"):
            fetch_source(parse_source(GIT_URL),
                         staging_root=tmp_path / "staging",
                         require_manifest=False)
        assert stub.manifest_required == []

    def test_a_local_index_root_is_returned_too(self, tmp_path):
        """The local fetcher honours the flag by having no check of its own."""
        root = tmp_path / "monorepo"
        write_package(root / "packages", name="demo", manifest_yaml=MANIFEST)
        spec = SourceSpec(kind="local", raw=str(root), path=root,
                          subdir="packages")

        with pytest.raises(FetchError, match="no manifest.yaml"):
            fetch_source(spec)
        result = fetch_source(spec, require_manifest=False)
        assert result.tree == root / "packages"
        assert result.staging is None

    def test_a_fetcher_predating_the_flag_still_installs(
        self, isolated_fetchers, tmp_path,
    ):
        legacy = _LegacyFetcher()
        register_fetcher(legacy)
        result = fetch_source(
            parse_source(GIT_URL), staging_root=tmp_path / "staging",
        )
        assert (result.tree / "manifest.yaml").is_file()
        assert legacy.calls == 1

    def test_a_fetcher_predating_the_flag_cannot_serve_a_listing(
        self, isolated_fetchers, tmp_path,
    ):
        legacy = _LegacyFetcher()
        register_fetcher(legacy)
        with pytest.raises(FetchError, match="does not accept"):
            fetch_source(parse_source(GIT_URL),
                         staging_root=tmp_path / "staging",
                         require_manifest=False)
        assert legacy.calls == 0
