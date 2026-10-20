"""Tests for the git package fetcher.

These are hermetic: every clone talks to a real git server on loopback,
served by dulwich's own WSGI application
(:func:`dulwich.web.make_wsgi_chain`) through
:mod:`wsgiref.simple_server` in a daemon thread.  Nothing reaches the
network, and the dulwich *client* path — smart protocol, basic auth,
HTTP status handling — is genuinely exercised rather than mocked.

Two consequences worth knowing when reading these tests:

* The grammar accepts ``git+http://`` only for a loopback literal, which
  is exactly the exception these fixtures need.
* The dispatcher's egress gate refuses ``127.0.0.1`` unless it is
  allowlisted, so the test config sets ``web_egress_allowlist``.  That
  is not a test-only contortion: an operator whose forge sits on a
  private address makes the same entry.
"""

from __future__ import annotations

import base64
import logging
import os
import socket
import threading
import time
from pathlib import Path
from textwrap import dedent
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest
from dulwich import porcelain
from dulwich.errors import GitProtocolError
from dulwich.repo import Repo
from dulwich.server import DictBackend
from dulwich.web import make_wsgi_chain

from carpenter import config
from carpenter.packages import fetch
from carpenter.packages.fetch import FetchError, FetchLimits, Fetcher, fetch_source
from carpenter.packages.fetch_git import GitFetcher, _harden_tree
from carpenter.packages.sources import SourceSpec, parse_source
from carpenter.security import egress

from .conftest import write_package

MANIFEST = dedent("""\
    name: demo
    version: "0.1.0"
    description: A git-fetch fixture package.
""")

_AUTHOR = b"Fixture <fixture@example.invalid>"


# ── fixture repositories ────────────────────────────────────────────


def _commit_all(root: Path, message: bytes = b"fixture commit") -> str:
    """Stage everything under ``root`` and commit it; return the sha40."""
    paths = [
        str(p) for p in sorted(root.rglob("*"))
        if ".git" not in p.parts and not p.is_dir()
    ]
    porcelain.add(str(root), paths=paths)
    sha = porcelain.commit(
        str(root), message=message, author=_AUTHOR, committer=_AUTHOR,
    )
    return sha.decode()


def _init(root: Path) -> None:
    porcelain.init(str(root))


@pytest.fixture
def package_repo(tmp_path: Path) -> Path:
    """A one-commit repository whose root is the package directory."""
    pkg = write_package(tmp_path / "src", name="demo", manifest_yaml=MANIFEST,
                        files={"tools.py": "VALUE = 1\n"})
    _init(pkg)
    _commit_all(pkg)
    return pkg


# ── the loopback git server ─────────────────────────────────────────


class _QuietHandler(WSGIRequestHandler):
    """A request handler that does not write to the test's stderr."""

    def log_message(self, *args, **kwargs):  # noqa: D102
        pass


@pytest.fixture
def git_server():
    """Serve repositories over loopback HTTP; return a factory.

    ``serve({"demo": path})`` publishes ``<base>/demo`` and returns
    ``<base>``.  An optional ``middleware`` wraps dulwich's WSGI app,
    which is how the credential and timeout tests observe or delay what
    the client does.
    """
    started: list = []

    def serve(repos: dict[str, Path], *, middleware=None) -> str:
        backend = DictBackend({
            f"/{name}".encode(): Repo(str(path))
            for name, path in repos.items()
        })
        app = make_wsgi_chain(backend)
        if middleware is not None:
            app = middleware(app)
        server = make_server("127.0.0.1", 0, app, handler_class=_QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}"

    yield serve

    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _recording_auth(seen: list, *, require: bool):
    """WSGI middleware recording (and optionally demanding) basic auth."""
    def middleware(app):
        def wrapped(environ, start_response):
            seen.append(environ.get("HTTP_AUTHORIZATION"))
            if require and not environ.get("HTTP_AUTHORIZATION"):
                start_response("401 Unauthorized", [
                    ("Content-Type", "text/plain"),
                    ("WWW-Authenticate", 'Basic realm="forge"'),
                ])
                return [b"authentication required"]
            return app(environ, start_response)
        return wrapped
    return middleware


def _slow(seconds: float):
    """WSGI middleware that stalls the very first request."""
    stalled: list = []

    def middleware(app):
        def wrapped(environ, start_response):
            if not stalled:
                stalled.append(True)
                time.sleep(seconds)
            return app(environ, start_response)
        return wrapped
    return middleware


# ── config, staging and a fetch helper ──────────────────────────────


@pytest.fixture
def configured(tmp_path: Path, monkeypatch) -> dict:
    """Platform config with loopback allowlisted and no sources yet."""
    cfg = {
        "base_dir": str(tmp_path / "instance"),
        "web_egress_allowlist": ["127.0.0.1"],
        "package_sources": [],
    }
    monkeypatch.setattr(config, "CONFIG", cfg)
    return cfg


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    root = tmp_path / "staging"
    root.mkdir()
    return root


@pytest.fixture
def do_fetch(staging: Path):
    """Fetch a spec through the real dispatcher (egress gate included)."""
    def _fetch(spec: SourceSpec, **limit_kwargs):
        limits = FetchLimits(**limit_kwargs) if limit_kwargs else FetchLimits()
        return fetch_source(spec, limits=limits, staging_root=staging)
    return _fetch


# ── registration and the protocol ───────────────────────────────────


def test_the_fetcher_registers_itself_for_git():
    assert isinstance(fetch._FETCHERS.get("git"), GitFetcher)


def test_the_fetcher_satisfies_the_protocol():
    assert isinstance(GitFetcher(), Fetcher)


# ── ref handling ────────────────────────────────────────────────────


class TestRefs:
    def test_clone_at_a_branch(self, tmp_path, package_repo, git_server,
                               configured, do_fetch):
        first = _commit_all(package_repo, b"second commit")
        porcelain.branch_create(str(package_repo), "release")
        (package_repo / "later.py").write_text("LATER = 1\n")
        head = _commit_all(package_repo, b"third commit")
        assert head != first

        base = git_server({"demo": package_repo})
        spec = parse_source(f"git+{base}/demo@release")
        result = do_fetch(spec)

        assert result.resolved == first
        assert (result.tree / "manifest.yaml").is_file()
        assert not (result.tree / "later.py").exists()
        assert result.files_fetched == 2
        assert result.bytes_fetched > 0

    def test_clone_at_an_annotated_tag_resolves_to_the_commit(
        self, package_repo, git_server, configured, do_fetch,
    ):
        tagged = _commit_all(package_repo, b"to be tagged")
        porcelain.tag_create(
            str(package_repo), tag=b"v1.2.3", message=b"release",
            author=_AUTHOR, annotated=True,
        )
        tag_object = Repo(str(package_repo)).get_refs()[b"refs/tags/v1.2.3"]
        assert tag_object.decode() != tagged, "fixture must use an annotated tag"

        base = git_server({"demo": package_repo})
        result = do_fetch(parse_source(f"git+{base}/demo@v1.2.3"))

        # The tag is peeled: ``resolved`` is a commit, never a tag object.
        assert result.resolved == tagged

    def test_clone_at_an_explicit_commit(self, package_repo, git_server,
                                         configured, do_fetch):
        first = Repo(str(package_repo)).head().decode()
        (package_repo / "later.py").write_text("LATER = 1\n")
        _commit_all(package_repo, b"second commit")

        base = git_server({"demo": package_repo})
        result = do_fetch(parse_source(f"git+{base}/demo@{first}"))

        assert result.resolved == first
        assert not (result.tree / "later.py").exists()

    def test_an_unreachable_explicit_commit_is_refused(
        self, package_repo, git_server, configured, do_fetch,
    ):
        base = git_server({"demo": package_repo})
        absent = "0" * 39 + "1"
        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source(f"git+{base}/demo@{absent}"))
        assert absent in str(exc.value)
        assert "reachable" in str(exc.value)

    def test_a_missing_branch_is_named(self, package_repo, git_server,
                                       configured, do_fetch):
        base = git_server({"demo": package_repo})
        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source(f"git+{base}/demo@no-such-branch"))
        assert "no-such-branch" in str(exc.value)

    def test_remote_head_with_no_ref(self, package_repo, git_server,
                                     configured, do_fetch):
        head = Repo(str(package_repo)).head().decode()
        base = git_server({"demo": package_repo})
        spec = parse_source(f"git+{base}/demo")
        assert spec.ref is None

        result = do_fetch(spec)

        assert result.resolved == head
        assert (result.tree / "manifest.yaml").is_file()


# ── the subdir ──────────────────────────────────────────────────────


class TestSubdir:
    @pytest.fixture
    def monorepo(self, tmp_path: Path) -> Path:
        root = tmp_path / "mono"
        write_package(root / "packages", name="demo",
                      manifest_yaml=MANIFEST, files={"tools.py": "A = 1\n"})
        write_package(root / "packages", name="other",
                      manifest_yaml=MANIFEST.replace("demo", "other"))
        (root / "README.md").write_text("a self-hosted forge monorepo\n")
        _init(root)
        _commit_all(root)
        return root

    def test_subdir_points_at_the_package_inside_a_monorepo(
        self, monorepo, git_server, configured, do_fetch,
    ):
        base = git_server({"mono": monorepo})
        spec = parse_source(f"git+{base}/mono#subdir=packages/demo")
        result = do_fetch(spec)

        assert result.tree.name == "demo"
        assert result.tree.parent.name == "packages"
        assert (result.tree / "tools.py").is_file()
        # Only the package subtree is materialised: a sibling package and
        # the repository's own files are never written.
        assert not (Path(result.staging) / "tree" / "README.md").exists()
        assert not (result.tree.parent / "other").exists()

    def test_a_missing_subdir_names_the_subdir(self, monorepo, git_server,
                                               configured, do_fetch):
        base = git_server({"mono": monorepo})
        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source(f"git+{base}/mono#subdir=packages/absent"))
        assert "packages/absent" in str(exc.value)

    def test_a_subdir_without_a_manifest_names_the_subdir(
        self, tmp_path, git_server, configured, do_fetch,
    ):
        root = tmp_path / "mono2"
        write_package(root / "packages", name="demo", manifest_yaml=MANIFEST)
        notpkg = root / "packages" / "notpkg"
        notpkg.mkdir(parents=True)
        (notpkg / "readme.txt").write_text("no manifest here\n")
        _init(root)
        _commit_all(root)

        base = git_server({"mono2": root})
        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source(f"git+{base}/mono2#subdir=packages/notpkg"))
        assert "packages/notpkg" in str(exc.value)
        assert "manifest.yaml" in str(exc.value)


# ── what the installer is handed ────────────────────────────────────


class TestReturnedTree:
    def test_no_git_directory_survives_anywhere_in_the_quarantine(
        self, package_repo, git_server, configured, do_fetch,
    ):
        base = git_server({"demo": package_repo})
        result = do_fetch(parse_source(f"git+{base}/demo"))

        assert not (result.tree / ".git").exists()
        staging = Path(result.staging)
        assert list(staging.rglob(".git")) == []
        assert list(staging.rglob("*.pack")) == []

    def test_the_tree_sits_inside_the_quarantine(
        self, package_repo, git_server, configured, do_fetch,
    ):
        base = git_server({"demo": package_repo})
        result = do_fetch(parse_source(f"git+{base}/demo"))
        assert result.tree.resolve().is_relative_to(
            Path(result.staging).resolve(),
        )

    def test_resolved_is_the_real_commit_and_counts_are_honest(
        self, package_repo, git_server, configured, do_fetch,
    ):
        head = Repo(str(package_repo)).head().decode()
        base = git_server({"demo": package_repo})
        result = do_fetch(parse_source(f"git+{base}/demo"))

        assert result.resolved == head
        assert len(result.resolved) == 40
        written = [p for p in result.tree.rglob("*") if p.is_file()]
        assert result.files_fetched == len(written)
        assert result.bytes_fetched == sum(p.stat().st_size for p in written)

    def test_an_executable_file_keeps_its_bit(
        self, tmp_path, git_server, configured, do_fetch,
    ):
        pkg = write_package(tmp_path / "x", name="demo",
                            manifest_yaml=MANIFEST,
                            files={"run.sh": "#!/bin/sh\nexit 0\n"})
        (pkg / "run.sh").chmod(0o755)
        _init(pkg)
        _commit_all(pkg)
        base = git_server({"demo": pkg})

        result = do_fetch(parse_source(f"git+{base}/demo"))
        assert os.access(result.tree / "run.sh", os.X_OK)


# ── the caps ────────────────────────────────────────────────────────


class TestLimits:
    def test_total_bytes_cap_on_the_clone(self, tmp_path, git_server,
                                          configured, do_fetch):
        pkg = write_package(tmp_path / "big", name="demo",
                            manifest_yaml=MANIFEST)
        # Incompressible, so the clone itself is over budget on disk.
        (pkg / "blob.bin").write_bytes(os.urandom(300_000))
        _init(pkg)
        _commit_all(pkg)
        base = git_server({"demo": pkg})

        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source(f"git+{base}/demo"), max_bytes=50_000)
        assert "max_bytes" in str(exc.value)
        assert "50000" in str(exc.value)

    def test_total_bytes_cap_on_what_the_tree_expands_to(
        self, tmp_path, git_server, configured, do_fetch,
    ):
        pkg = write_package(tmp_path / "zip", name="demo",
                            manifest_yaml=MANIFEST)
        # Highly compressible: the clone is tiny, the tree is not.
        (pkg / "padding.txt").write_text("a" * 400_000)
        _init(pkg)
        _commit_all(pkg)
        base = git_server({"demo": pkg})

        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source(f"git+{base}/demo"), max_bytes=50_000)
        message = str(exc.value)
        assert "max_bytes" in message
        assert "400" in message  # the observed expansion, not the pack size

    def test_file_count_cap(self, tmp_path, git_server, configured, do_fetch):
        pkg = write_package(
            tmp_path / "many", name="demo", manifest_yaml=MANIFEST,
            files={f"mod{i}.py": f"N = {i}\n" for i in range(6)},
        )
        _init(pkg)
        _commit_all(pkg)
        base = git_server({"demo": pkg})

        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source(f"git+{base}/demo"), max_files=3)
        assert "max_files" in str(exc.value)
        assert "allowed 3" in str(exc.value)

    def test_per_file_cap(self, tmp_path, git_server, configured, do_fetch):
        pkg = write_package(tmp_path / "fat", name="demo",
                            manifest_yaml=MANIFEST)
        (pkg / "fat.txt").write_text("b" * 5_000)
        _init(pkg)
        _commit_all(pkg)
        base = git_server({"demo": pkg})

        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source(f"git+{base}/demo"), max_file_bytes=1_000)
        message = str(exc.value)
        assert "max_file_bytes" in message
        assert "5000 bytes in 'fat.txt'" in message

    def test_wall_clock_timeout(self, package_repo, git_server, configured,
                                do_fetch):
        base = git_server({"demo": package_repo}, middleware=_slow(1.0))
        started = time.monotonic()
        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source(f"git+{base}/demo"), timeout_s=0.2)
        elapsed = time.monotonic() - started
        assert "timeout_s" in str(exc.value)
        assert elapsed < 1.0, "the caller must not wait for the clone"

    def test_a_breached_cap_returns_no_tree(self, tmp_path, git_server,
                                            configured, staging, do_fetch):
        pkg = write_package(tmp_path / "many2", name="demo",
                            manifest_yaml=MANIFEST,
                            files={f"m{i}.py": "X = 1\n" for i in range(6)})
        _init(pkg)
        _commit_all(pkg)
        base = git_server({"demo": pkg})

        with pytest.raises(FetchError):
            do_fetch(parse_source(f"git+{base}/demo"), max_files=2)
        # The dispatcher removed the quarantine: nothing partial is left.
        assert list(staging.iterdir()) == []


# ── hardening ───────────────────────────────────────────────────────


class TestHardening:
    def test_a_symlink_escaping_the_tree_is_refused(
        self, tmp_path, git_server, configured, do_fetch,
    ):
        pkg = write_package(tmp_path / "link", name="demo",
                            manifest_yaml=MANIFEST)
        os.symlink("../../../etc/passwd", pkg / "escape")
        _init(pkg)
        _commit_all(pkg)
        base = git_server({"demo": pkg})

        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source(f"git+{base}/demo"))
        message = str(exc.value)
        assert "escape" in message
        assert "outside the fetched tree" in message

    def test_an_absolute_symlink_is_refused(self, tmp_path, git_server,
                                            configured, do_fetch):
        pkg = write_package(tmp_path / "abs", name="demo",
                            manifest_yaml=MANIFEST)
        os.symlink("/etc/hosts", pkg / "hosts")
        _init(pkg)
        _commit_all(pkg)
        base = git_server({"demo": pkg})

        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source(f"git+{base}/demo"))
        assert "absolute" in str(exc.value)

    def test_an_in_tree_symlink_is_kept(self, tmp_path, git_server,
                                        configured, do_fetch):
        pkg = write_package(tmp_path / "ok", name="demo",
                            manifest_yaml=MANIFEST,
                            files={"real.py": "A = 1\n"})
        os.symlink("real.py", pkg / "alias.py")
        _init(pkg)
        _commit_all(pkg)
        base = git_server({"demo": pkg})

        result = do_fetch(parse_source(f"git+{base}/demo"))
        link = result.tree / "alias.py"
        assert link.is_symlink()
        assert os.readlink(link) == "real.py"

    def test_a_repository_with_submodules_is_refused(
        self, tmp_path, git_server, configured, do_fetch,
    ):
        pkg = write_package(
            tmp_path / "sub", name="demo", manifest_yaml=MANIFEST,
            files={".gitmodules": (
                '[submodule "dep"]\n'
                "\tpath = dep\n"
                "\turl = https://forge.example/org/dep.git\n"
            )},
        )
        _init(pkg)
        _commit_all(pkg)
        base = git_server({"demo": pkg})

        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source(f"git+{base}/demo"))
        message = str(exc.value)
        assert ".gitmodules" in message
        assert "not transitive" in message

    def test_the_hardening_pass_refuses_a_hard_link(self, tmp_path):
        # A git tree cannot express a hard link, so the pass is exercised
        # on a tree that already holds one: it is the backstop for
        # anything that materialises bytes some other way.
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "manifest.yaml").write_text(MANIFEST)
        outside = tmp_path / "outside.txt"
        outside.write_text("owned elsewhere\n")
        os.link(outside, tree / "linked.txt")

        with pytest.raises(FetchError) as exc:
            _harden_tree(tree, SourceSpec(kind="git", raw="git+https://x/y",
                                          url="https://x/y"))
        assert "hard link" in str(exc.value)

    def test_the_hardening_pass_refuses_a_device_or_fifo(self, tmp_path):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "manifest.yaml").write_text(MANIFEST)
        os.mkfifo(tree / "pipe")

        with pytest.raises(FetchError) as exc:
            _harden_tree(tree, SourceSpec(kind="git", raw="git+https://x/y",
                                          url="https://x/y"))
        assert "regular file" in str(exc.value)


# ── credentials (D8) ────────────────────────────────────────────────


TOKEN = "tk-not-a-real-token-6b1f9c"


class TestCredentials:
    def _source_config(self, configured: dict, base: str, **extra) -> None:
        configured["package_sources"] = [{
            "name": "forge",
            "url": f"git+{base}/demo",
            "credential_ref": "CARPENTER_TEST_FORGE_TOKEN",
            **extra,
        }]

    def test_the_token_reaches_the_server_and_no_log(
        self, package_repo, git_server, configured, do_fetch, monkeypatch,
        caplog,
    ):
        seen: list = []
        base = git_server({"demo": package_repo},
                          middleware=_recording_auth(seen, require=True))
        self._source_config(configured, base)
        monkeypatch.setenv("CARPENTER_TEST_FORGE_TOKEN", TOKEN)

        with caplog.at_level(logging.DEBUG, logger="carpenter"):
            spec = parse_source("demo")
            assert spec.alias == "forge"
            result = do_fetch(spec)

        expected = "Basic " + base64.b64encode(
            f"{TOKEN}:".encode(),
        ).decode()
        assert expected in seen, "the server never saw the credential"
        assert (result.tree / "manifest.yaml").is_file()

        # The value appears nowhere an operator or a log reader can see it.
        assert TOKEN not in caplog.text
        assert TOKEN not in str(result)
        assert TOKEN not in (spec.url or "")
        assert TOKEN not in spec.raw

    def test_a_user_and_token_pair_is_split(
        self, package_repo, git_server, configured, do_fetch, monkeypatch,
    ):
        seen: list = []
        base = git_server({"demo": package_repo},
                          middleware=_recording_auth(seen, require=True))
        self._source_config(configured, base)
        monkeypatch.setenv("CARPENTER_TEST_FORGE_TOKEN", f"bot:{TOKEN}")

        do_fetch(parse_source("demo"))

        expected = "Basic " + base64.b64encode(
            f"bot:{TOKEN}".encode(),
        ).decode()
        assert expected in seen

    def test_an_unset_credential_names_the_variable_not_the_value(
        self, package_repo, git_server, configured, do_fetch, monkeypatch,
    ):
        base = git_server({"demo": package_repo})
        self._source_config(configured, base)
        monkeypatch.delenv("CARPENTER_TEST_FORGE_TOKEN", raising=False)

        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source("demo"))
        message = str(exc.value)
        assert "CARPENTER_TEST_FORGE_TOKEN" in message
        assert "operator must supply it" in message

    def test_an_empty_credential_is_treated_as_unset(
        self, package_repo, git_server, configured, do_fetch, monkeypatch,
    ):
        base = git_server({"demo": package_repo})
        self._source_config(configured, base)
        monkeypatch.setenv("CARPENTER_TEST_FORGE_TOKEN", "   ")

        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source("demo"))
        assert "CARPENTER_TEST_FORGE_TOKEN" in str(exc.value)

    def test_a_rejected_credential_does_not_leak_it(
        self, package_repo, git_server, configured, do_fetch, monkeypatch,
        caplog,
    ):
        seen: list = []
        base = git_server({"demo": package_repo},
                          middleware=_recording_auth(seen, require=True))
        configured["package_sources"] = [{
            "name": "forge",
            # The same repository, reached with no credential_ref at all:
            # the server demands auth, the client has none to send.
            "url": f"git+{base}/demo",
        }]
        monkeypatch.setenv("CARPENTER_TEST_FORGE_TOKEN", TOKEN)

        with caplog.at_level(logging.DEBUG, logger="carpenter"):
            with pytest.raises(FetchError) as exc:
                do_fetch(parse_source("demo"))
        message = str(exc.value)
        assert "401" in message
        assert "credential" in message
        assert TOKEN not in message
        assert TOKEN not in caplog.text

    def test_a_subdir_template_source_still_authenticates(
        self, tmp_path, git_server, configured, staging, monkeypatch,
    ):
        root = tmp_path / "mono3"
        write_package(root / "packages", name="demo", manifest_yaml=MANIFEST)
        _init(root)
        _commit_all(root)
        seen: list = []
        base = git_server({"demo": root},
                          middleware=_recording_auth(seen, require=True))
        configured["package_sources"] = [{
            "name": "forge",
            "url": f"git+{base}/demo",
            "subdir_template": "packages/{name}",
            "credential_ref": "CARPENTER_TEST_FORGE_TOKEN",
        }]
        monkeypatch.setenv("CARPENTER_TEST_FORGE_TOKEN", TOKEN)

        spec = parse_source("demo")
        assert spec.subdir == "packages/demo"
        result = fetch_source(spec, staging_root=staging)
        assert (result.tree / "manifest.yaml").is_file()
        assert any(entry for entry in seen if entry)


# ── operator-facing errors ──────────────────────────────────────────


class TestErrors:
    def test_a_missing_repository_is_a_404_message(
        self, package_repo, git_server, configured, do_fetch,
    ):
        base = git_server({"demo": package_repo})
        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source(f"git+{base}/absent"))
        message = str(exc.value)
        assert "404" in message
        assert "not a git repository" in message

    def test_a_refused_connection_says_so(self, configured, do_fetch):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source(f"git+http://127.0.0.1:{port}/demo"))
        message = str(exc.value)
        assert "did not accept a connection" in message
        assert "reachable from the platform host" in message

    def test_a_dns_failure_says_so(self, tmp_path, configured, monkeypatch):
        # The dispatcher's egress gate resolves first, so a real unresolvable
        # host never reaches the fetcher: the mapping is exercised by
        # raising what dulwich raises, with the fetcher called directly.
        def boom(*args, **kwargs):
            raise GitProtocolError(
                "HTTPConnectionPool(host='forge.invalid', port=443): Max "
                "retries exceeded with url: /org/pkg.git/info/refs (Caused "
                "by NameResolutionError(\"Failed to resolve 'forge.invalid' "
                "([Errno -2] Name or service not known)\"))",
            )
        monkeypatch.setattr(porcelain, "clone", boom)
        dest = tmp_path / "quarantine"
        dest.mkdir()

        spec = parse_source("git+https://forge.invalid/org/pkg.git")
        with pytest.raises(FetchError) as exc:
            GitFetcher().fetch(spec, dest, limits=FetchLimits())
        message = str(exc.value)
        assert "could not be resolved" in message
        assert "DNS" in message
        assert "Traceback" not in message

    def test_a_non_git_spec_is_refused(self, tmp_path):
        spec = SourceSpec(kind="archive", raw="https://x/y.tar.gz",
                          url="https://x/y.tar.gz")
        with pytest.raises(FetchError) as exc:
            GitFetcher().fetch(spec, tmp_path, limits=FetchLimits())
        assert "archive" in str(exc.value)

    def test_the_egress_gate_refuses_loopback_without_an_allowlist(
        self, package_repo, git_server, tmp_path, monkeypatch, staging,
    ):
        monkeypatch.setattr(config, "CONFIG", {
            "base_dir": str(tmp_path / "instance"),
            "web_egress_allowlist": [],
            "package_sources": [],
        })
        base = git_server({"demo": package_repo})
        with pytest.raises(FetchError) as exc:
            fetch_source(parse_source(f"git+{base}/demo"),
                         staging_root=staging)
        assert "web_egress_allowlist" in str(exc.value)


# ── redirects: every hop re-checked, and no hop off the origin ───────


def _redirect_to(target_base: str, path_prefix: str = ""):
    """WSGI middleware that 307s the first request to another base URL.

    Mirrors what a forge (or an attacker who controls one allowlisted
    name) can do: the clone's very first request —
    ``/info/refs?service=git-upload-pack`` — is answered with a
    ``Location`` pointing somewhere else entirely.
    """
    seen: list[str] = []

    def middleware(app):
        def wrapped(environ, start_response):
            path = environ.get("PATH_INFO", "")
            query = environ.get("QUERY_STRING", "")
            seen.append(path + (f"?{query}" if query else ""))
            if len(seen) == 1:
                location = f"{target_base}{path_prefix}{path}"
                if query:
                    location += f"?{query}"
                start_response("307 Temporary Redirect", [
                    ("Content-Type", "text/plain"),
                    ("Location", location),
                ])
                return [b"moved"]
            return app(environ, start_response)
        return wrapped

    middleware.seen = seen  # type: ignore[attr-defined]
    return middleware


class TestRedirects:
    """F1: the clone transport must not walk through the egress gate."""

    def test_a_redirect_to_another_host_is_refused(
        self, package_repo, git_server, tmp_path, monkeypatch, staging,
    ):
        # Only the name "localhost" is allowlisted.  The attacker's
        # redirect target, 127.0.0.1, is NOT allowlisted and
        # ``check_url`` refuses it when asked directly -- so if the tree
        # arrives anyway, the gate has been walked through.
        monkeypatch.setattr(config, "CONFIG", {
            "base_dir": str(tmp_path / "instance"),
            "web_egress_allowlist": ["localhost"],
            "package_sources": [],
        })
        internal = git_server({"demo": package_repo})
        front = _redirect_to(internal)
        front_base = git_server({"demo": package_repo}, middleware=front)
        port = front_base.rsplit(":", 1)[1]

        with pytest.raises(FetchError) as exc:
            fetch_source(
                parse_source(f"git+http://localhost:{port}/demo"),
                staging_root=staging,
            )
        message = str(exc.value)
        assert "redirect" in message.lower()
        assert list(staging.iterdir()) == []

    def test_a_redirect_that_changes_the_port_is_refused(
        self, package_repo, git_server, configured, do_fetch, staging,
    ):
        internal = git_server({"demo": package_repo})
        front = _redirect_to(internal)
        front_base = git_server({"demo": package_repo}, middleware=front)

        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source(f"git+{front_base}/demo"))
        assert "redirect" in str(exc.value).lower()
        assert list(staging.iterdir()) == []

    def test_a_same_origin_redirect_is_followed_and_every_hop_checked(
        self, package_repo, git_server, configured, do_fetch, monkeypatch,
    ):
        checked: list[str] = []
        real_check = egress.check_url

        def recording(url):
            checked.append(url)
            return real_check(url)

        monkeypatch.setattr(egress, "check_url", recording)

        # The front server redirects /moved/demo/... to /demo/... on
        # itself: same scheme, host and port, so it is served.
        base_holder: list[str] = []

        def middleware(app):
            def wrapped(environ, start_response):
                path = environ.get("PATH_INFO", "")
                query = environ.get("QUERY_STRING", "")
                if path.startswith("/moved/"):
                    location = base_holder[0] + path[len("/moved"):]
                    if query:
                        location += f"?{query}"
                    start_response("307 Temporary Redirect", [
                        ("Content-Type", "text/plain"),
                        ("Location", location),
                    ])
                    return [b"moved"]
                return app(environ, start_response)
            return wrapped

        base = git_server({"demo": package_repo}, middleware=middleware)
        base_holder.append(base)

        result = do_fetch(parse_source(f"git+{base}/moved/demo"))
        assert (result.tree / "manifest.yaml").is_file()
        # Hop 0 from the dispatcher, plus at least one hop checked by
        # the transport itself.
        assert len(checked) >= 2
        assert any("/demo/info/refs" in url for url in checked)

    def test_the_hop_limit_is_enforced(
        self, package_repo, git_server, configured, do_fetch, staging,
    ):
        def endless(app):
            def wrapped(environ, start_response):
                path = environ.get("PATH_INFO", "")
                query = environ.get("QUERY_STRING", "")
                location = path + f"?{query}" if query else path
                start_response("307 Temporary Redirect", [
                    ("Content-Type", "text/plain"),
                    ("Location", location),
                ])
                return [b"moved"]
            return wrapped

        base = git_server({"demo": package_repo}, middleware=endless)
        with pytest.raises(FetchError) as exc:
            do_fetch(parse_source(f"git+{base}/demo"))
        assert "redirect" in str(exc.value).lower()
        assert list(staging.iterdir()) == []
