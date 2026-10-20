"""Listing what a configured package source offers.

Hermetic: every fetch goes through the real dispatcher
(:func:`carpenter.packages.fetch.fetch_source`, so the egress gate, the
quarantine and the result validation all run) but the fetcher itself is
a stub that copies a fixture tree instead of touching a network.  The
stub is registered for the ``git`` kind and the registry is restored
afterwards, because a configured source's URL parses to a real kind —
there is no way to point ``package_sources`` at a test-only kind.

What these tests are really guarding:

* one unreadable manifest must never make the rest of a source
  invisible;
* every spec string a listing emits must parse back, or the listing is
  giving out install instructions that do not work;
* a listing must leave nothing behind — no quarantine, and no imported
  package module;
* ``require_manifest=False`` buys a listing the index root and nothing
  else: a single-package source with no manifest is still refused.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from textwrap import dedent
from typing import ClassVar

import pytest

from carpenter import config
from carpenter.packages import discover, fetch
from carpenter.packages.discover import (
    DiscoveryError,
    RemotePackage,
    cached_listing,
    list_remote_packages,
    list_source_aliases,
    listing_cache_path,
    listing_from_tree,
    shape_for,
)
from carpenter.packages.fetch import FetchResult
from carpenter.packages.sources import ConfiguredSource, parse_source

from .conftest import write_package
# Imported for use as a fixture: the loopback dulwich server that
# ``test_fetch_git`` serves its repositories from.  One copy of that
# machinery is enough, and the end-to-end listing test below needs the
# real GitFetcher to have a real repository to clone.
from .test_fetch_git import _commit_all as git_commit_all
from .test_fetch_git import _init as git_init
from .test_fetch_git import git_server  # noqa: F401 — a pytest fixture

HOST = "forge.discover.invalid"
MONO_URL = f"git+https://{HOST}/org/packages.git"
SOLO_URL = f"git+https://{HOST}/org/solo.git"
ARCHIVE_URL = f"https://{HOST}/dist/solo.tar.gz"
COMMIT = "a" * 40
SEED = (
    Path(__file__).resolve().parents[2]
    / "config_seed" / "chat_tools" / "packages_install_request.py"
)


def _manifest(name: str, *, version: str = "0.1.0", description: str = "") -> str:
    body = description or f"The {name} package.\nA second line nobody shows."
    return dedent(
        f"""\
        name: {name}
        version: "{version}"
        description: |
          {body.splitlines()[0]}
        """,
    ) + "".join(f"  {line}\n" for line in body.splitlines()[1:])


# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def base_dir(tmp_path, monkeypatch):
    """An isolated ``base_dir`` with the stub host allowed for egress."""
    base = tmp_path / "carpenter-base"
    base.mkdir()
    monkeypatch.setattr(
        "carpenter.config.CONFIG",
        {
            "base_dir": str(base),
            "web_egress_allowlist": [HOST],
            "package_sources": [],
        },
    )
    return base


def _configure(*entries: dict) -> None:
    """Point ``package_sources`` at the given entries."""
    config.CONFIG["package_sources"] = list(entries)


MONO_SOURCE = {
    "name": "house",
    "url": MONO_URL,
    "subdir_template": "packages/{name}",
}
SOLO_SOURCE = {"name": "solo", "url": SOLO_URL}


class _StubFetcher:
    """Serves a prepared tree as if it had been fetched over the wire.

    Registered for the real ``git``/``archive`` kinds, because that is
    what a configured source's URL parses to.  ``serve`` is copied into
    the quarantine the dispatcher created, so the dispatcher's own
    checks (inside the quarantine, a directory, holds a manifest) apply
    exactly as they would to the real fetcher.
    """

    kinds: ClassVar[frozenset[str]] = frozenset({"git", "archive"})

    def __init__(self):
        self.serve: Path | None = None
        self.resolved = COMMIT
        self.calls: list = []
        self.manifest_required: list[bool] = []

    def fetch(self, spec, dest, *, limits, require_manifest=True):
        self.calls.append(spec)
        # Recorded, not acted on: the real fetchers refuse a
        # manifest-less tree themselves, and the dispatcher's
        # ``_validate_result`` refuses one from any fetcher, so leaving
        # the flag unenforced here keeps the dispatcher's own check
        # under test rather than this double's.
        self.manifest_required.append(require_manifest)
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
    saved = dict(fetch._FETCHERS)
    f = _StubFetcher()
    fetch.register_fetcher(f)
    yield f
    fetch._FETCHERS.clear()
    fetch._FETCHERS.update(saved)


@pytest.fixture
def monorepo(tmp_path):
    """An index root holding three packages, as a monorepo would."""
    index = tmp_path / "source" / "packages"
    index.mkdir(parents=True)
    for name in ("alpha", "beta", "gamma"):
        write_package(index, name=name, manifest_yaml=_manifest(name))
    return index


@pytest.fixture
def solo(tmp_path):
    """A source whose root IS the package."""
    root = tmp_path / "solo-source"
    root.mkdir()
    (root / "manifest.yaml").write_text(_manifest("solo", version="2.0.0"))
    (root / "README.md").write_text("hello\n")
    return root


def _head(repo: Path) -> str:
    """The sha40 at ``repo``'s HEAD."""
    from dulwich.repo import Repo

    return Repo(str(repo)).head().decode()


def _tree_contents(tree: Path) -> dict[str, bytes]:
    """Every file under ``tree``, by relative path, for a byte comparison."""
    return {
        str(path.relative_to(tree)): path.read_bytes()
        for path in sorted(tree.rglob("*")) if path.is_file()
    }


def _source(entry: dict) -> ConfiguredSource:
    return ConfiguredSource(
        name=entry["name"], url=entry["url"],
        subdir_template=entry.get("subdir_template"),
        credential_ref=entry.get("credential_ref"),
    )


# ── the shape of a source ───────────────────────────────────────────


class TestShape:
    """How the index root is derived from ``subdir_template``."""

    @pytest.mark.parametrize(
        ("template", "is_index", "index_root", "fetch_subdir"),
        [
            (None, False, "", None),
            ("packages/{name}", True, "packages", "packages"),
            ("{name}", True, "", None),
            ("a/b/{name}/src", True, "a/b", "a/b"),
            ("pkg-{name}", True, "", None),
            ("fixed/dir", False, "", "fixed/dir"),
        ],
    )
    def test_the_directory_above_the_placeholder_is_the_index(
        self, template, is_index, index_root, fetch_subdir,
    ):
        shape = shape_for(_source({**MONO_SOURCE, "subdir_template": template}))
        assert shape.is_index is is_index
        assert shape.index_root == index_root
        assert shape.fetch_subdir == fetch_subdir

    def test_a_template_a_listing_cannot_invert_is_refused(self):
        with pytest.raises(DiscoveryError) as exc:
            shape_for(_source({**MONO_SOURCE, "subdir_template": "{name}/{name}"}))
        assert "path segments" in str(exc.value)
        assert "Installing a package by name" in str(exc.value)


# ── listing a materialised tree ─────────────────────────────────────


class TestListingFromTree:
    """The shape-agnostic half: manifests in, listing out."""

    def test_a_monorepo_source_lists_every_package(self, base_dir, monorepo):
        listing = listing_from_tree(
            _source(MONO_SOURCE), monorepo, resolved=COMMIT,
        )
        assert [p.name for p in listing.packages] == ["alpha", "beta", "gamma"]
        assert listing.errors == ()
        assert listing.resolved == COMMIT
        assert listing.source == "house"
        alpha = listing.packages[0]
        assert alpha.subdir == "packages/alpha"
        assert alpha.version == "0.1.0"
        # First line only: the fixture's second line must not be here.
        assert alpha.description == "The alpha package."
        assert "second line" not in alpha.description

    def test_a_single_package_source_lists_one_package(self, base_dir, solo):
        listing = listing_from_tree(_source(SOLO_SOURCE), solo, resolved=COMMIT)
        assert [p.name for p in listing.packages] == ["solo"]
        assert listing.packages[0].subdir is None
        assert listing.packages[0].version == "2.0.0"

    def test_one_unparseable_manifest_does_not_hide_the_others(
        self, base_dir, monorepo,
    ):
        (monorepo / "broken").mkdir()
        (monorepo / "broken" / "manifest.yaml").write_text("name: [oops\n")
        listing = listing_from_tree(
            _source(MONO_SOURCE), monorepo, resolved=COMMIT,
        )
        assert [p.name for p in listing.packages] == ["alpha", "beta", "gamma"]
        assert len(listing.errors) == 1
        assert listing.errors[0].startswith("broken: ")

    def test_an_error_never_quotes_the_quarantine_path(self, base_dir, monorepo):
        (monorepo / "broken").mkdir()
        (monorepo / "broken" / "manifest.yaml").write_text("name: [oops\n")
        listing = listing_from_tree(
            _source(MONO_SOURCE), monorepo, resolved=COMMIT,
        )
        assert str(monorepo) not in listing.errors[0]
        assert "<source>" in listing.errors[0]

    def test_non_package_children_are_skipped_silently(self, base_dir, monorepo):
        (monorepo / "docs").mkdir()
        (monorepo / "_scratch").mkdir()
        (monorepo / "notes.txt").write_text("hi\n")
        listing = listing_from_tree(
            _source(MONO_SOURCE), monorepo, resolved=COMMIT,
        )
        assert [p.name for p in listing.packages] == ["alpha", "beta", "gamma"]
        assert listing.errors == ()

    def test_a_placeholder_prefix_selects_the_matching_children(
        self, base_dir, tmp_path,
    ):
        index = tmp_path / "prefixed"
        index.mkdir()
        write_package(index, name="pkg-one", manifest_yaml=_manifest("one"))
        write_package(index, name="other", manifest_yaml=_manifest("other"))
        source = _source({**MONO_SOURCE, "subdir_template": "pkg-{name}"})
        listing = listing_from_tree(source, index, resolved=COMMIT)
        assert [p.name for p in listing.packages] == ["one"]
        assert listing.packages[0].subdir == "pkg-one"

    def test_a_directory_disagreeing_with_its_manifest_is_flagged(
        self, base_dir, monorepo,
    ):
        write_package(monorepo, name="delta", manifest_yaml=_manifest("epsilon"))
        listing = listing_from_tree(
            _source(MONO_SOURCE), monorepo, resolved=COMMIT,
        )
        assert "epsilon" in [p.name for p in listing.packages]
        assert any("does not match the manifest name" in e for e in listing.errors)

    def test_a_single_package_tree_without_a_manifest_is_refused(
        self, base_dir, tmp_path,
    ):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(DiscoveryError) as exc:
            listing_from_tree(_source(SOLO_SOURCE), empty, resolved=COMMIT)
        assert "manifest.yaml" in str(exc.value)

    def test_the_redacted_url_is_what_a_listing_carries(self, base_dir, solo):
        entry = {"name": "solo", "url": f"git+https://bot:secret@{HOST}/org/solo.git"}
        listing = listing_from_tree(_source(entry), solo, resolved=COMMIT)
        assert "secret" not in listing.url
        assert "***" in listing.url


# ── the specs a listing hands out ───────────────────────────────────


class TestEmittedSpecs:
    """A listing that gives out specs that do not parse is useless."""

    def _specs(self, source, tree) -> list[RemotePackage]:
        return list(listing_from_tree(source, tree, resolved=COMMIT).packages)

    def test_every_emitted_spec_parses_back(self, base_dir, monorepo, solo):
        cases = [
            (_source(MONO_SOURCE), monorepo),
            (_source(SOLO_SOURCE), solo),
            (_source({**MONO_SOURCE, "subdir_template": "a/{name}/pkg"}), None),
            (_source({"name": "arc", "url": ARCHIVE_URL}), solo),
        ]
        checked = 0
        for source, tree in cases:
            if tree is None:
                continue
            for pkg in self._specs(source, tree):
                spec = parse_source(pkg.spec, config={"package_sources": []})
                assert spec.kind in ("git", "archive")
                assert spec.subdir == pkg.subdir
                checked += 1
        assert checked >= 5

    def test_a_git_spec_pins_the_listed_commit(self, base_dir, monorepo):
        pkg = self._specs(_source(MONO_SOURCE), monorepo)[0]
        assert pkg.spec == (
            f"git+https://{HOST}/org/packages.git@{COMMIT}#subdir=packages/alpha"
        )
        assert parse_source(pkg.spec, config={"package_sources": []}).ref == COMMIT

    def test_an_archive_spec_pins_the_listed_digest(self, base_dir, solo):
        source = _source({"name": "arc", "url": ARCHIVE_URL})
        listing = listing_from_tree(source, solo, resolved="b" * 64)
        spec = listing.packages[0].spec
        assert spec == f"{ARCHIVE_URL}#sha256={'b' * 64}"
        assert parse_source(spec, config={"package_sources": []}).digest == "b" * 64

    def test_a_nested_template_keeps_the_tail_in_the_subdir(
        self, base_dir, tmp_path,
    ):
        index = tmp_path / "nested"
        (index / "one" / "pkg").mkdir(parents=True)
        (index / "one" / "pkg" / "manifest.yaml").write_text(_manifest("one"))
        source = _source({**MONO_SOURCE, "subdir_template": "a/{name}/pkg"})
        listing = listing_from_tree(source, index, resolved=COMMIT)
        assert listing.packages[0].subdir == "a/one/pkg"
        assert parse_source(
            listing.packages[0].spec, config={"package_sources": []},
        ).subdir == "a/one/pkg"


# ── the alias registry ──────────────────────────────────────────────


class TestAliases:
    def test_the_aliases_are_the_configured_order(self, base_dir):
        _configure(MONO_SOURCE, SOLO_SOURCE)
        assert list_source_aliases() == ("house", "solo")

    def test_an_unknown_alias_lists_the_known_ones(self, base_dir):
        _configure(MONO_SOURCE, SOLO_SOURCE)
        with pytest.raises(DiscoveryError) as exc:
            list_remote_packages("nope")
        assert "'nope'" in str(exc.value)
        assert "house, solo" in str(exc.value)

    def test_with_nothing_configured_the_refusal_names_the_keys(self, base_dir):
        _configure()
        with pytest.raises(DiscoveryError) as exc:
            list_remote_packages("house")
        assert "package_sources" in str(exc.value)
        assert "web_egress_allowlist" in str(exc.value)

    def test_a_malformed_registry_is_refused_not_skipped(self, base_dir):
        config.CONFIG["package_sources"] = [{"name": "bad"}]
        with pytest.raises(DiscoveryError):
            list_source_aliases()


# ── end to end through the dispatcher ───────────────────────────────


class TestListRemotePackages:
    def test_a_single_package_source_is_listed_end_to_end(
        self, base_dir, stub, solo,
    ):
        _configure(SOLO_SOURCE)
        stub.serve = solo
        listing = list_remote_packages("solo")
        assert [p.name for p in listing.packages] == ["solo"]
        assert listing.resolved == COMMIT
        assert len(stub.calls) == 1
        # The whole tree root is what a single-package listing fetches.
        assert stub.calls[0].subdir is None
        assert stub.calls[0].alias == "solo"

    def test_the_quarantine_is_always_cleaned_up(self, base_dir, stub, solo):
        _configure(SOLO_SOURCE)
        stub.serve = solo
        list_remote_packages("solo")
        staging = fetch.staging_root_dir()
        assert list(staging.iterdir()) == []

    def test_the_quarantine_is_cleaned_up_even_when_reading_fails(
        self, base_dir, stub, solo, monkeypatch,
    ):
        _configure(SOLO_SOURCE)
        stub.serve = solo
        monkeypatch.setattr(
            discover, "listing_from_tree",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        with pytest.raises(RuntimeError):
            list_remote_packages("solo")
        assert list(fetch.staging_root_dir().iterdir()) == []

    def test_no_package_module_is_imported(self, base_dir, stub, tmp_path):
        root = tmp_path / "with-tools"
        root.mkdir()
        (root / "manifest.yaml").write_text(
            dedent(
                """\
                name: solo
                version: "0.1.0"
                description: Ships a chat tool.
                chat_tools:
                  - tools.py
                """,
            ),
        )
        marker = tmp_path / "imported-marker"
        (root / "tools.py").write_text(
            f"open({str(marker)!r}, 'w').write('x')  # must never run\n",
        )
        _configure(SOLO_SOURCE)
        stub.serve = root
        list_remote_packages("solo")
        assert not marker.exists()
        assert not [m for m in sys.modules if m.startswith("_carpenter_pkg_")]

    def test_an_index_source_is_listed_end_to_end(
        self, base_dir, stub, monorepo,
    ):
        """A monorepo index root is not a package, and lists anyway.

        The listing fetch is the one caller that passes
        ``require_manifest=False``, so the dispatcher hands back the
        directory above the packages instead of refusing it.
        """
        _configure(MONO_SOURCE)
        stub.serve = monorepo
        listing = list_remote_packages("house")
        assert [p.name for p in listing.packages] == ["alpha", "beta", "gamma"]
        assert [p.subdir for p in listing.packages] == [
            "packages/alpha", "packages/beta", "packages/gamma",
        ]
        assert listing.errors == ()
        # The index root is what was fetched, not the whole repository.
        assert stub.calls[0].subdir == "packages"
        assert list(fetch.staging_root_dir().iterdir()) == []

    def test_a_listing_fetch_does_not_require_a_manifest(
        self, base_dir, stub, monorepo, solo,
    ):
        """Whatever the shape, a listing asks for a directory.

        ``listing_from_tree`` is then the authority on whether a
        single-package source really holds a package, so the fetch
        layer's manifest assertion is never what a listing relies on.
        """
        _configure(MONO_SOURCE, SOLO_SOURCE)
        stub.serve = monorepo
        list_remote_packages("house")
        stub.serve = solo
        list_remote_packages("solo")
        assert stub.manifest_required == [False, False]

    def test_a_single_package_source_without_a_manifest_still_refuses(
        self, base_dir, stub, tmp_path,
    ):
        """``require_manifest=False`` must not make a listing credulous."""
        empty = tmp_path / "not-a-package"
        empty.mkdir()
        (empty / "README.md").write_text("nothing to install here\n")
        _configure(SOLO_SOURCE)
        stub.serve = empty
        with pytest.raises(DiscoveryError) as exc:
            list_remote_packages("solo")
        assert "single package" in str(exc.value)
        assert "manifest.yaml" in str(exc.value)
        assert list(fetch.staging_root_dir().iterdir()) == []

    def test_a_missing_index_root_names_the_directory_it_read(
        self, base_dir, stub, solo,
    ):
        """The operator typed a template, not a subdir; name the root."""
        _configure(MONO_SOURCE)
        stub.serve = solo  # no ``packages/`` anywhere in it

        def refuse(spec, dest, *, limits, require_manifest=True):
            raise fetch.FetchError("the git source has no directory 'packages'")

        stub.fetch = refuse  # type: ignore[method-assign]
        with pytest.raises(DiscoveryError) as exc:
            list_remote_packages("house")
        message = str(exc.value)
        assert "reads the directory 'packages'" in message
        assert "packages/{name}" in message

    def test_a_refused_fetch_is_reported_not_raised_raw(
        self, base_dir, stub, solo,
    ):
        """The egress gate runs before any fetcher, and its message
        naming ``web_egress_allowlist`` must reach the caller intact —
        that key is the operator's one step for a self-hosted source."""
        _configure({"name": "solo", "url": "git+https://localhost/org/solo.git"})
        stub.serve = solo
        config.CONFIG["web_egress_allowlist"] = []
        with pytest.raises(DiscoveryError) as exc:
            list_remote_packages("solo")
        assert "web_egress_allowlist" in str(exc.value)
        assert stub.calls == []


# ── the cache ───────────────────────────────────────────────────────


class TestCache:
    def test_a_second_call_is_served_from_the_cache(self, base_dir, stub, solo):
        _configure(SOLO_SOURCE)
        stub.serve = solo
        first = list_remote_packages("solo")
        second = list_remote_packages("solo")
        assert len(stub.calls) == 1
        assert second == first

    def test_refresh_bypasses_the_cache(self, base_dir, stub, solo):
        _configure(SOLO_SOURCE)
        stub.serve = solo
        list_remote_packages("solo")
        stub.resolved = "c" * 40
        again = list_remote_packages("solo", refresh=True)
        assert len(stub.calls) == 2
        assert again.resolved == "c" * 40

    def test_an_expired_listing_is_refetched(self, base_dir, stub, solo):
        _configure(SOLO_SOURCE)
        stub.serve = solo
        list_remote_packages("solo")
        path = listing_cache_path(_source(SOLO_SOURCE))
        data = json.loads(path.read_text())
        data["fetched_at"] = "2001-01-01T00:00:00+00:00"
        path.write_text(json.dumps(data))
        list_remote_packages("solo")
        assert len(stub.calls) == 2

    def test_a_zero_ttl_always_refetches(self, base_dir, stub, solo):
        _configure(SOLO_SOURCE)
        config.CONFIG["package_sources_listing_ttl_hours"] = 0
        stub.serve = solo
        list_remote_packages("solo")
        list_remote_packages("solo")
        assert len(stub.calls) == 2

    def test_an_unusable_ttl_falls_back_to_the_default(self, base_dir):
        config.CONFIG["package_sources_listing_ttl_hours"] = "soon"
        assert discover.listing_ttl_hours() == float(discover.DEFAULT_TTL_HOURS)
        config.CONFIG["package_sources_listing_ttl_hours"] = -3
        assert discover.listing_ttl_hours() == float(discover.DEFAULT_TTL_HOURS)

    def test_the_cache_file_is_named_for_the_alias_and_the_source(
        self, base_dir, stub, solo,
    ):
        _configure(SOLO_SOURCE)
        stub.serve = solo
        list_remote_packages("solo")
        path = listing_cache_path(_source(SOLO_SOURCE))
        assert path.parent == base_dir / "cache" / "package-listings"
        assert path.name.startswith("solo-") and path.name.endswith(".json")
        assert path.is_file()

    def test_a_retargeted_alias_does_not_serve_the_old_listing(
        self, base_dir, stub, solo,
    ):
        _configure(SOLO_SOURCE)
        stub.serve = solo
        list_remote_packages("solo")
        _configure({"name": "solo", "url": f"git+https://{HOST}/org/elsewhere.git"})
        list_remote_packages("solo")
        assert len(stub.calls) == 2

    def test_a_corrupt_cache_file_is_a_miss(self, base_dir, stub, solo):
        _configure(SOLO_SOURCE)
        stub.serve = solo
        list_remote_packages("solo")
        listing_cache_path(_source(SOLO_SOURCE)).write_text("{not json")
        list_remote_packages("solo")
        assert len(stub.calls) == 2

    def test_cached_listing_reads_without_fetching(self, base_dir, stub, solo):
        _configure(SOLO_SOURCE)
        stub.serve = solo
        list_remote_packages("solo")
        stub.calls.clear()
        cached = cached_listing("solo")
        assert cached is not None
        assert [p.name for p in cached.packages] == ["solo"]
        assert stub.calls == []

    def test_cached_listing_is_none_before_any_fetch(self, base_dir):
        _configure(SOLO_SOURCE)
        assert cached_listing("solo") is None

    def test_the_cache_round_trips_errors_and_subdirs(
        self, base_dir, stub, monorepo, monkeypatch,
    ):
        """A cached listing is the same listing, errors included."""
        _configure(MONO_SOURCE)
        source = _source(MONO_SOURCE)
        listing = replace(
            listing_from_tree(source, monorepo, resolved=COMMIT),
            errors=("broken: nope",),
        )
        discover._write_cache(listing, source)
        assert cached_listing("house") == listing


# ── the chat tool ───────────────────────────────────────────────────


def _load_tool():
    """Load the seed module and return its listing handler."""
    spec = importlib.util.spec_from_file_location(
        "_test_packages_install_request_listing", str(SEED),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.list_available_packages


@pytest.fixture
def tool():
    return _load_tool()


class TestChatTool:
    def test_it_is_a_platform_tool_and_not_chat_only(self):
        from carpenter.chat_tool_registry import CHAT_ONLY_TOOLS, PLATFORM_TOOLS

        assert "list_available_packages" in PLATFORM_TOOLS
        assert "list_available_packages" not in CHAT_ONLY_TOOLS

    def test_it_declares_only_read_capabilities(self):
        """Read-only, un-confirmed, and at the platform boundary.

        The platform boundary is not decoration: the listing cache file
        is a write, and a chat-boundary tool may not write at all.  The
        capabilities say what the CALLER gets, which is reads.
        """
        from carpenter.chat_tool_registry import READ_CAPABILITIES

        meta = _load_tool()._chat_tool_meta
        assert set(meta["capabilities"]) <= READ_CAPABILITIES
        assert meta["trust_boundary"] == "platform"
        assert meta["requires_user_confirm"] is False
        assert meta["always_available"] is False

    def test_one_source_lists_its_packages_with_specs(
        self, base_dir, stub, solo, tool,
    ):
        _configure(SOLO_SOURCE)
        stub.serve = solo
        reply = tool({"source": "solo"})
        assert "solo v2.0.0" in reply
        assert f"spec: {SOLO_URL}@{COMMIT}" in reply
        assert COMMIT[:12] in reply
        assert "Nothing was installed" in reply
        assert "none of this package code was run" in reply

    def test_no_argument_summarises_the_sources_from_cache_only(
        self, base_dir, stub, solo, tool,
    ):
        _configure(SOLO_SOURCE, MONO_SOURCE)
        stub.serve = solo
        list_remote_packages("solo")
        stub.calls.clear()
        reply = tool({})
        assert "solo — 1 package(s)" in reply
        assert "house — not listed yet" in reply
        assert stub.calls == []
        assert "Nothing was installed" in reply

    def test_with_no_source_configured_it_says_what_to_configure(
        self, base_dir, tool,
    ):
        _configure()
        reply = tool({})
        assert "package_sources" in reply
        assert "web_egress_allowlist" in reply
        assert "Nothing was installed" in reply

    def test_an_unknown_alias_is_an_ordinary_string(self, base_dir, tool):
        _configure(SOLO_SOURCE)
        reply = tool({"source": "nope"})
        assert isinstance(reply, str)
        assert "Could not list packages" in reply
        assert "solo" in reply

    def test_it_never_refreshes_on_its_own(self, base_dir, stub, solo, tool):
        _configure(SOLO_SOURCE)
        stub.serve = solo
        tool({"source": "solo"})
        tool({"source": "solo"})
        assert len(stub.calls) == 1

    def test_an_unexpected_failure_does_not_raise_into_the_loop(
        self, base_dir, tool, monkeypatch,
    ):
        _configure(SOLO_SOURCE)
        monkeypatch.setattr(
            discover, "list_remote_packages",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        reply = tool({"source": "solo"})
        assert "Listing packages failed" in reply
        assert "Nothing was installed" in reply


# ── a real monorepo, over loopback, through the real fetcher ─────────


class TestAMonorepoEndToEnd:
    """No stub: the real :class:`GitFetcher` against a real repository.

    The loopback dulwich server is the one ``test_fetch_git`` uses, so
    this exercises the whole path a listing really takes — egress gate,
    clone, caps, hardening, quarantine — on a monorepo whose index root
    is not a package.
    """

    LOOPBACK = "127.0.0.1"

    @pytest.fixture
    def repo(self, tmp_path) -> Path:
        """Three good packages, one broken manifest, one non-package."""
        root = tmp_path / "monorepo"
        index = root / "packages"
        for name in ("alpha", "beta", "gamma"):
            write_package(
                index, name=name, manifest_yaml=_manifest(name),
                files={"tools.py": f"NAME = {name!r}\n"},
            )
        write_package(index, name="broken", manifest_yaml="name: [oops\n")
        notes = index / "notes"
        notes.mkdir(parents=True)
        (notes / "readme.txt").write_text("how this monorepo is laid out\n")
        (root / "README.md").write_text("a self-hosted forge monorepo\n")
        git_init(root)
        git_commit_all(root)
        return root

    @pytest.fixture
    def loopback(self, tmp_path, monkeypatch, repo, git_server) -> dict:
        """Serve ``repo`` and configure it as the ``house`` source."""
        base = git_server({"monorepo": repo})
        instance = tmp_path / "instance"
        instance.mkdir()
        entry = {
            "name": "house",
            "url": f"git+{base}/monorepo",
            "subdir_template": "packages/{name}",
        }
        monkeypatch.setattr(
            config, "CONFIG",
            {
                "base_dir": str(instance),
                "web_egress_allowlist": [self.LOOPBACK],
                "package_sources": [entry],
            },
        )
        return {"base": base, "entry": entry, "head": _head(repo)}

    def test_a_monorepo_lists_every_readable_package(self, loopback):
        listing = list_remote_packages("house")

        assert [p.name for p in listing.packages] == ["alpha", "beta", "gamma"]
        assert [p.subdir for p in listing.packages] == [
            "packages/alpha", "packages/beta", "packages/gamma",
        ]
        assert listing.resolved == loopback["head"]
        assert [p.version for p in listing.packages] == ["0.1.0"] * 3
        assert listing.packages[0].description == "The alpha package."

    def test_one_broken_manifest_lands_in_errors_and_hides_nothing(
        self, loopback,
    ):
        listing = list_remote_packages("house")

        assert len(listing.errors) == 1
        assert listing.errors[0].startswith("broken:")
        assert len(listing.packages) == 3
        # The non-package child is skipped silently: a monorepo is
        # allowed to hold a notes directory.
        assert "notes" not in str(listing.errors)
        # And no quarantine path leaks into what a reader is shown.
        assert "_fetch-" not in listing.errors[0]

    def test_every_emitted_spec_parses_back_pinned_to_the_commit(
        self, loopback,
    ):
        listing = list_remote_packages("house")
        for package in listing.packages:
            spec = parse_source(package.spec)
            assert spec.kind == "git"
            assert spec.subdir == package.subdir
            assert spec.ref == loopback["head"]

    def test_a_listed_spec_fetches_what_a_bare_name_would(self, loopback):
        """The spec names the same subdir, and the same bytes come back."""
        listing = list_remote_packages("house")
        listed = next(p for p in listing.packages if p.name == "beta")

        by_spec = parse_source(listed.spec)
        by_name = parse_source("beta")
        assert by_spec.subdir == by_name.subdir == "packages/beta"

        from carpenter.packages.fetch import fetch_source

        first = fetch_source(by_spec)
        second = fetch_source(by_name)
        try:
            assert first.resolved == second.resolved == loopback["head"]
            assert _tree_contents(first.tree) == _tree_contents(second.tree)
            assert "manifest.yaml" in _tree_contents(first.tree)
        finally:
            fetch.cleanup(first)
            fetch.cleanup(second)

    def test_the_listing_leaves_nothing_behind(self, loopback):
        list_remote_packages("house")
        assert list(fetch.staging_root_dir().iterdir()) == []
        assert not [m for m in sys.modules if m.startswith("_carpenter_pkg_")]

    def test_a_template_pointing_at_the_wrong_directory_says_so(
        self, loopback, monkeypatch,
    ):
        entry = {**loopback["entry"], "subdir_template": "libs/{name}"}
        config.CONFIG["package_sources"] = [entry]
        with pytest.raises(DiscoveryError) as exc:
            list_remote_packages("house")
        message = str(exc.value)
        assert "reads the directory 'libs'" in message
        assert "libs/{name}" in message
        assert list(fetch.staging_root_dir().iterdir()) == []
