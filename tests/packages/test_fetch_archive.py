"""Tests for the archive-URL package fetcher.

Hermetic throughout: every request goes to a ``http.server`` bound to
``127.0.0.1`` on an ephemeral port, started per test and shut down with
it.  Fixture archives are built with
:func:`carpenter.packages.archive_cache.archive_tree` over a package
tree written by the shared ``write_package`` helper, so what is served
is exactly what the installer would otherwise have been handed.

``http://`` is only legal in the source grammar for a loopback literal,
and the egress guard refuses ``127.0.0.1`` unless it is listed in
``web_egress_allowlist`` — so the tests set that key, which is also what
an operator with a forge on a private address has to do.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import logging
import socket
import ssl
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from textwrap import dedent

import httpx
import pytest

from carpenter import config
from carpenter.packages import fetch, fetch_archive
from carpenter.packages.archive_cache import (
    ArchiveVerificationError,
    archive_tree,
)
from carpenter.packages.fetch import (
    FetchError,
    FetchLimits,
    Fetcher,
    cleanup,
    fetch_source,
    register_fetcher,
)
from carpenter.packages.fetch_archive import ArchiveUrlFetcher
from carpenter.packages.sources import SourceSpec, parse_source

from .conftest import write_package

MANIFEST = dedent("""\
    name: demo
    version: "0.1.0"
    description: An archive-fetch fixture package.
""")

LOOPBACK = "127.0.0.1"
SECRET = "s3cr3t-forge-read-token-do-not-log"
TOKEN_VAR = "CARPENTER_TEST_FORGE_TOKEN"


# ── the hermetic HTTP server ────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    """Dispatches GETs to routes the test registered, recording each."""

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        self.server.seen.append((path, dict(self.headers)))
        route = self.server.routes.get(path)
        if route is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        route(self)

    def log_message(self, *args) -> None:  # pragma: no cover — quiet tests
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Site:
    """A loopback HTTP server with per-path handlers, for one test."""

    def __init__(self) -> None:
        self._server = _Server((LOOPBACK, 0), _Handler)
        self._server.routes = {}
        self._server.seen = []
        # A short poll interval keeps ``shutdown()`` — and therefore
        # every test's teardown — well under a tenth of a second.
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def origin(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    def url(self, path: str) -> str:
        return self.origin + path

    @property
    def seen(self) -> list[tuple[str, dict]]:
        return self._server.seen

    def headers_for(self, path: str) -> dict:
        """Headers of the first request to ``path`` (lowercased keys)."""
        for seen_path, headers in self.seen:
            if seen_path == path:
                return {k.lower(): v for k, v in headers.items()}
        raise AssertionError(f"no request was made to {path!r}")

    def route(self, path: str, handler) -> None:
        self._server.routes[path] = handler

    def serve(
        self,
        path: str,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/gzip",
        content_length: int | None = None,
        extra_headers: dict | None = None,
    ) -> str:
        """Serve fixed bytes at ``path``; return its URL.

        ``content_length`` overrides the declared length, which is how
        the truncated-body case is simulated.
        """
        def handler(request) -> None:
            request.send_response(status)
            request.send_header("Content-Type", content_type)
            declared = len(body) if content_length is None else content_length
            request.send_header("Content-Length", str(declared))
            for key, value in (extra_headers or {}).items():
                request.send_header(key, value)
            request.end_headers()
            request.wfile.write(body)

        self.route(path, handler)
        return self.url(path)

    def status(self, path: str, status: int) -> str:
        """Serve a bare status code at ``path``; return its URL."""
        return self.serve(path, b"", status=status, content_length=0)

    def redirect(self, path: str, location: str, *, status: int = 302) -> str:
        """Serve a redirect at ``path``; return its URL."""
        def handler(request) -> None:
            request.send_response(status)
            request.send_header("Location", location)
            request.send_header("Content-Length", "0")
            request.end_headers()

        self.route(path, handler)
        return self.url(path)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def site():
    server = Site()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def other_site():
    """A second origin, for the cross-origin credential rule."""
    server = Site()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture(autouse=True)
def allow_loopback_egress(monkeypatch):
    """Allowlist the loopback literal the test server binds to.

    Realistic rather than a workaround: a self-hosted forge on a private
    address needs exactly this entry before the platform will fetch from
    it (design decision D2).
    """
    monkeypatch.setitem(config.CONFIG, "web_egress_allowlist", [LOOPBACK])
    monkeypatch.setitem(config.CONFIG, "package_sources", [])


@pytest.fixture
def no_token(monkeypatch):
    """Make sure no stray token is in the environment."""
    monkeypatch.delenv(TOKEN_VAR, raising=False)


# ── fixture archives ────────────────────────────────────────────────


@pytest.fixture
def flat_archive(tmp_path) -> bytes:
    """A ``.tar.gz`` whose root IS the package (manifest at top level)."""
    source = write_package(
        _fresh(tmp_path / "flat"), name="demo",
        manifest_yaml=MANIFEST,
        files={"tools.py": "VALUE = 1\n"},
    )
    return _archive_bytes(source, tmp_path / "flat.tar.gz")


@pytest.fixture
def nested_archive(tmp_path) -> bytes:
    """A ``.tar.gz`` holding exactly one top-level directory."""
    root = _fresh(tmp_path / "nested")
    write_package(
        root, name="demo-1.0",
        manifest_yaml=MANIFEST, files={"tools.py": "VALUE = 2\n"},
    )
    return _archive_bytes(root, tmp_path / "nested.tar.gz")


def _fresh(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _archive_bytes(source_dir: Path, out_path: Path) -> bytes:
    """Deterministically archive ``source_dir`` and return the bytes."""
    archive_tree(source_dir, out_path)
    return out_path.read_bytes()


def _tar_gz(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    """Build a ``.tar.gz`` from explicit members (for hostile shapes)."""
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for info, data in members:
                tar.addfile(
                info, io.BytesIO(data) if data is not None else None,
            )
    return raw.getvalue()


def _file_member(name: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = 0
    return info, data


def _manifest_member(prefix: str = "") -> tuple[tarfile.TarInfo, bytes]:
    return _file_member(f"{prefix}manifest.yaml", MANIFEST.encode())


class _Zeros(io.RawIOBase):
    """A readable stream of ``n`` zero bytes, never materialised.

    Lets a test build a genuine decompression bomb — a tiny gzip whose
    tar index declares an enormous member — without writing the expanded
    size anywhere.
    """

    def __init__(self, count: int) -> None:
        self._left = count

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        take = min(len(buffer), self._left)
        buffer[:take] = b"\0" * take
        self._left -= take
        return take


def _bomb_archive(declared_bytes: int) -> bytes:
    """A small ``.tar.gz`` declaring a ``declared_bytes`` member."""
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            info, data = _manifest_member()
            tar.addfile(info, io.BytesIO(data))
            big = tarfile.TarInfo("zeros.bin")
            big.size = declared_bytes
            big.mtime = 0
            tar.addfile(big, io.BufferedReader(_Zeros(declared_bytes)))
    return raw.getvalue()


def _spec(url: str, **kwargs) -> SourceSpec:
    """An archive spec for ``url`` without going through the grammar."""
    return SourceSpec(kind="archive", raw=url, url=url, **kwargs)


@pytest.fixture
def quarantine(tmp_path) -> Path:
    """A fresh 0700 quarantine directory, as the dispatcher would make."""
    dest = tmp_path / "quarantine"
    dest.mkdir(mode=0o700)
    return dest


def _fetch(spec: SourceSpec, dest: Path, *, require_manifest: bool = True,
           **limit_kwargs):
    limits = FetchLimits(**limit_kwargs) if limit_kwargs else FetchLimits()
    return ArchiveUrlFetcher().fetch(
        spec, dest, limits=limits, require_manifest=require_manifest,
    )


# ── registration and shape ──────────────────────────────────────────


class TestSeam:
    def test_kinds(self):
        assert ArchiveUrlFetcher.kinds == frozenset({"archive"})

    def test_satisfies_the_protocol(self):
        assert isinstance(ArchiveUrlFetcher(), Fetcher)

    def test_importing_the_module_registers_it(self):
        assert isinstance(fetch._FETCHERS.get("archive"), ArchiveUrlFetcher)

    def test_refuses_a_spec_of_another_kind(self, quarantine):
        spec = SourceSpec(kind="git", raw="git+https://host/r.git",
                          url="https://host/r.git")
        with pytest.raises(FetchError, match="handles 'archive' sources"):
            _fetch(spec, quarantine)

    def test_refuses_a_spec_with_no_url(self, quarantine):
        with pytest.raises(FetchError, match="has no URL to fetch"):
            _fetch(SourceSpec(kind="archive", raw="demo"), quarantine)


# ── the two accepted layouts ────────────────────────────────────────


class TestLayouts:
    def test_flat_archive(self, site, quarantine, flat_archive, caplog):
        url = site.serve("/demo.tar.gz", flat_archive)
        result = _fetch(_spec(url), quarantine)

        assert (result.tree / "manifest.yaml").is_file()
        assert (result.tree / "tools.py").read_text() == "VALUE = 1\n"
        assert result.staging == quarantine
        assert result.tree.resolve().is_relative_to(quarantine.resolve())
        assert result.bytes_fetched == len(flat_archive)
        assert result.files_fetched == 2
        assert result.resolved == hashlib.sha256(flat_archive).hexdigest()

    def test_single_top_level_directory_is_the_package(
        self, site, quarantine, nested_archive,
    ):
        url = site.serve("/demo.tgz", nested_archive)
        result = _fetch(_spec(url), quarantine)

        assert result.tree.name == "demo-1.0"
        assert (result.tree / "tools.py").read_text() == "VALUE = 2\n"

    def test_plain_tar_is_accepted(self, site, quarantine, tmp_path):
        body = _tar_gz_to_tar(
            [_manifest_member(), _file_member("tools.py", b"X = 1\n")],
        )
        url = site.serve("/demo.tar", body, content_type="application/x-tar")
        result = _fetch(_spec(url), quarantine)
        assert (result.tree / "manifest.yaml").is_file()

    def test_subdir_is_honoured(self, site, quarantine, tmp_path):
        root = _fresh(tmp_path / "mono")
        (root / "packages").mkdir()
        write_package(root / "packages", name="demo",
                      manifest_yaml=MANIFEST, files={"t.py": "A = 1\n"})
        (root / "README.md").write_text("mono\n")
        body = _archive_bytes(root, tmp_path / "mono.tar.gz")

        url = site.serve("/mono.tar.gz", body)
        result = _fetch(_spec(url, subdir="packages/demo"), quarantine)

        assert result.tree.name == "demo"
        assert (result.tree / "t.py").read_text() == "A = 1\n"

    def test_several_top_level_entries_without_a_manifest(
        self, site, quarantine,
    ):
        body = _tar_gz([
            _file_member("one/a.txt", b"a"),
            _file_member("two/b.txt", b"b"),
        ])
        url = site.serve("/odd.tar.gz", body)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url), quarantine)
        message = str(excinfo.value)
        assert "not a capability package" in message
        assert "one/" in message and "two/" in message

    def test_single_directory_without_a_manifest(self, site, quarantine):
        body = _tar_gz([_file_member("only/a.txt", b"a")])
        url = site.serve("/odd.tar.gz", body)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url), quarantine)
        message = str(excinfo.value)
        assert "single top-level directory 'only'" in message
        assert "a.txt" in message

    def test_subdir_that_does_not_exist(self, site, quarantine, flat_archive):
        url = site.serve("/demo.tar.gz", flat_archive)
        with pytest.raises(FetchError, match="has no directory 'nope'"):
            _fetch(_spec(url, subdir="nope"), quarantine)

    def test_subdir_without_a_manifest(self, site, quarantine):
        body = _tar_gz([
            _manifest_member(),
            _file_member("sub/a.txt", b"a"),
        ])
        url = site.serve("/demo.tar.gz", body)
        with pytest.raises(FetchError, match="holds no manifest.yaml"):
            _fetch(_spec(url, subdir="sub"), quarantine)

    def test_subdir_escaping_the_archive_is_refused(
        self, site, quarantine, flat_archive,
    ):
        url = site.serve("/demo.tar.gz", flat_archive)
        with pytest.raises(FetchError, match="points outside"):
            _fetch(_spec(url, subdir="../../etc"), quarantine)


def _tar_gz_to_tar(members) -> bytes:
    """Build an *uncompressed* tar from explicit members."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for info, data in members:
            tar.addfile(
                info, io.BytesIO(data) if data is not None else None,
            )
    return raw.getvalue()


# ── integrity ───────────────────────────────────────────────────────


class TestDigest:
    def test_a_matching_digest_passes(self, site, quarantine, flat_archive):
        digest = hashlib.sha256(flat_archive).hexdigest()
        url = site.serve("/demo.tar.gz", flat_archive)
        result = _fetch(_spec(url, digest=digest), quarantine)
        assert result.resolved == digest

    def test_a_mismatching_digest_is_refused(
        self, site, quarantine, flat_archive,
    ):
        wrong = "0" * 64
        actual = hashlib.sha256(flat_archive).hexdigest()
        url = site.serve("/demo.tar.gz", flat_archive)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url, digest=wrong), quarantine)
        message = str(excinfo.value)
        assert "failed its sha256 check" in message
        assert f"expected {wrong}" in message
        assert f"got {actual}" in message

    def test_a_mismatch_extracts_nothing(
        self, site, quarantine, flat_archive,
    ):
        url = site.serve("/demo.tar.gz", flat_archive)
        with pytest.raises(FetchError):
            _fetch(_spec(url, digest="1" * 64), quarantine)
        assert list((quarantine / "_tree").iterdir()) == []

    def test_an_unpinned_download_warns_with_its_digest(
        self, site, quarantine, flat_archive, caplog,
    ):
        url = site.serve("/demo.tar.gz", flat_archive)
        digest = hashlib.sha256(flat_archive).hexdigest()
        with caplog.at_level(logging.WARNING,
                             logger="carpenter.packages.fetch_archive"):
            result = _fetch(_spec(url), quarantine)

        warnings = [
            record for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert warnings, "an unpinned download must warn"
        text = "\n".join(record.getMessage() for record in warnings)
        assert "UNPINNED" in text
        assert digest in text
        assert f"#sha256={digest}" in text
        assert result.resolved == digest

    def test_a_pinned_download_does_not_warn(
        self, site, quarantine, flat_archive, caplog,
    ):
        url = site.serve("/demo.tar.gz", flat_archive)
        digest = hashlib.sha256(flat_archive).hexdigest()
        with caplog.at_level(logging.WARNING,
                             logger="carpenter.packages.fetch_archive"):
            _fetch(_spec(url, digest=digest), quarantine)
        assert not [
            record for record in caplog.records
            if record.levelno == logging.WARNING
        ]


# ── caps ────────────────────────────────────────────────────────────


class TestByteCap:
    def test_a_declared_content_length_over_the_cap_is_refused_up_front(
        self, site, quarantine, flat_archive,
    ):
        url = site.serve("/demo.tar.gz", flat_archive)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url), quarantine, max_bytes=16)
        message = str(excinfo.value)
        assert "Content-Length" in message
        assert "max_bytes limit of 16" in message

    def test_the_stream_is_aborted_mid_body(self, site, quarantine):
        """A body with no declared length is cut off, not drained."""
        chunk = b"\0" * (64 * 1024)
        total_chunks = 512          # 32 MiB if it ever all arrived
        written = {"chunks": 0}
        finished = threading.Event()

        def flood(request) -> None:
            request.send_response(200)
            request.send_header("Content-Type", "application/gzip")
            request.end_headers()
            try:
                for _ in range(total_chunks):
                    request.wfile.write(chunk)
                    written["chunks"] += 1
            except OSError:
                pass            # the client hung up, which is the point
            finally:
                finished.set()

        site.route("/flood.tar.gz", flood)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(site.url("/flood.tar.gz")), quarantine,
                   max_bytes=64 * 1024)

        message = str(excinfo.value)
        assert "max_bytes limit of 65536" in message
        assert "aborted after" in message
        assert finished.wait(timeout=10), "the server handler never returned"
        assert written["chunks"] < total_chunks, (
            "the whole body was transferred; the cap did not abort the stream"
        )

    def test_declared_member_sizes_over_the_cap_are_refused(
        self, site, quarantine,
    ):
        body = _tar_gz([
            _manifest_member(),
            _file_member("a.bin", b"\0" * 4096),
            _file_member("b.bin", b"\0" * 4096),
        ])
        url = site.serve("/demo.tar.gz", body)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url), quarantine,
                   max_bytes=4096, max_file_bytes=1 << 20)
        message = str(excinfo.value)
        assert "bytes of file content" in message
        assert "max_bytes limit of 4096" in message


class TestFileCountCap:
    def test_too_many_files(self, site, quarantine, tmp_path):
        root = _fresh(tmp_path / "many")
        write_package(
            root, name="demo", manifest_yaml=MANIFEST,
            files={f"f{index}.py": f"N = {index}\n" for index in range(30)},
        )
        body = _archive_bytes(root / "demo", tmp_path / "many.tar.gz")
        url = site.serve("/demo.tar.gz", body)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url), quarantine, max_files=5)
        message = str(excinfo.value)
        # Lazily enforced: the walk stops at the cap, so the message
        # bounds the count instead of reporting the whole index.
        assert "declares more than 5 entries" in message
        assert "max_files limit of 5" in message


class TestPerFileCap:
    def test_a_declared_size_bomb_is_refused_before_extraction(
        self, site, quarantine,
    ):
        declared = 16 * 1024 * 1024
        body = _bomb_archive(declared)
        # The proof that this is a bomb: the archive is tiny.
        assert len(body) < 64 * 1024

        url = site.serve("/bomb.tar.gz", body)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url), quarantine,
                   max_bytes=64 * 1024 * 1024, max_file_bytes=1024)

        message = str(excinfo.value)
        assert "declares member 'zeros.bin'" in message
        assert f"as {declared} bytes" in message
        assert "max_file_bytes limit of 1024" in message
        assert "refusing to expand it" in message
        # Nothing was written: the index, not the disk, caught it.
        assert list((quarantine / "_tree").iterdir()) == []

    def test_what_landed_is_measured_too(self, tmp_path):
        """The post-extraction walk is the backstop for a lying index."""
        root = _fresh(tmp_path / "landed")
        (root / "big.bin").write_bytes(b"\0" * 2048)
        with pytest.raises(FetchError) as excinfo:
            fetch_archive._measure_extracted(
                root, FetchLimits(max_file_bytes=1024),
                "https://host/p.tar.gz",
            )
        assert "expanded 'big.bin' to 2048 bytes" in str(excinfo.value)
        assert "max_file_bytes limit of 1024" in str(excinfo.value)

    def test_the_landed_total_is_measured_too(self, tmp_path):
        root = _fresh(tmp_path / "landed")
        for index in range(4):
            (root / f"f{index}.bin").write_bytes(b"\0" * 1024)
        with pytest.raises(FetchError, match="max_bytes"):
            fetch_archive._measure_extracted(
                root, FetchLimits(max_bytes=2048), "https://host/p.tar.gz",
            )

    def test_the_landed_count_is_measured_too(self, tmp_path):
        root = _fresh(tmp_path / "landed")
        for index in range(4):
            (root / f"f{index}.bin").write_bytes(b"x")
        with pytest.raises(FetchError, match="max_files"):
            fetch_archive._measure_extracted(
                root, FetchLimits(max_files=2), "https://host/p.tar.gz",
            )


class TestTimeout:
    def test_a_slow_server_hits_the_wall_clock_budget(self, site, quarantine):
        def slow(request) -> None:
            request.send_response(200)
            request.send_header("Content-Type", "application/gzip")
            request.send_header("Content-Length", "1024")
            request.end_headers()
            request.wfile.write(b"\0" * 8)
            time.sleep(1.0)

        site.route("/slow.tar.gz", slow)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(site.url("/slow.tar.gz")), quarantine,
                   timeout_s=0.3)
        message = str(excinfo.value)
        assert "timed out" in message or "wall-clock budget" in message
        assert "0.3" in message

    def test_an_exhausted_budget_is_reported_with_the_limit(self, quarantine):
        with pytest.raises(FetchError) as excinfo:
            fetch_archive._remaining(
                time.monotonic() - 1.0, FetchLimits(timeout_s=5.0),
                time.monotonic() - 7.0,
            )
        message = str(excinfo.value)
        assert "timeout_s=5.0" in message
        assert "elapsed=" in message


# ── redirects ───────────────────────────────────────────────────────


class TestRedirects:
    def test_an_allowed_chain_is_followed(
        self, site, quarantine, flat_archive,
    ):
        site.serve("/final.tar.gz", flat_archive)
        site.redirect("/second.tar.gz", site.url("/final.tar.gz"))
        start = site.redirect("/first.tar.gz", "/second.tar.gz")

        result = _fetch(_spec(start), quarantine)
        assert (result.tree / "manifest.yaml").is_file()
        assert [path for path, _ in site.seen] == [
            "/first.tar.gz", "/second.tar.gz", "/final.tar.gz",
        ]

    @pytest.mark.parametrize(
        "target",
        [
            "http://169.254.169.254/latest/meta-data/",   # cloud metadata
            "http://[::1]:8080/pkg.tar.gz",               # loopback
            "http://10.1.2.3/pkg.tar.gz",                 # private range
        ],
    )
    def test_a_redirect_to_a_refused_address_is_refused(
        self, site, quarantine, target,
    ):
        start = site.redirect("/first.tar.gz", target)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(start), quarantine)
        message = str(excinfo.value)
        assert "refusing to continue the redirect chain" in message
        assert isinstance(excinfo.value.__cause__, Exception)
        # Only the first hop was ever requested.
        assert [path for path, _ in site.seen] == ["/first.tar.gz"]

    def test_the_hop_limit_is_enforced(self, site, quarantine):
        # Each hop points at the next, more than MAX_REDIRECTS deep.
        depth = fetch_archive.MAX_REDIRECTS + 3
        for index in range(depth):
            site.redirect(f"/hop{index}", f"/hop{index + 1}")
        with pytest.raises(FetchError, match="Too many redirects"):
            _fetch(_spec(site.url("/hop0")), quarantine)
        assert len(site.seen) <= fetch_archive.MAX_REDIRECTS + 1


# ── credentials ─────────────────────────────────────────────────────


@pytest.fixture
def configured_token_source(monkeypatch, site):
    """A ``package_sources`` entry for ``site`` naming a credential var."""
    def _configure(path: str = "/demo.tar.gz", **overrides) -> SourceSpec:
        entry = {
            "name": "forge",
            "url": site.url(path),
            "credential_ref": TOKEN_VAR,
        }
        entry.update(overrides)
        monkeypatch.setitem(config.CONFIG, "package_sources", [entry])
        return parse_source(
            "demo", name="demo", config={"package_sources": [entry]},
        )
    return _configure


class TestCredentials:
    def test_the_bearer_token_reaches_the_server(
        self, site, quarantine, flat_archive, configured_token_source,
        monkeypatch,
    ):
        monkeypatch.setenv(TOKEN_VAR, SECRET)
        site.serve("/demo.tar.gz", flat_archive)
        spec = configured_token_source()
        assert spec.alias == "forge"

        result = _fetch(spec, quarantine)
        assert (result.tree / "manifest.yaml").is_file()
        assert site.headers_for("/demo.tar.gz")["authorization"] == (
            f"Bearer {SECRET}"
        )

    def test_an_unset_variable_is_refused_by_name(
        self, site, quarantine, flat_archive, configured_token_source,
        no_token,
    ):
        site.serve("/demo.tar.gz", flat_archive)
        with pytest.raises(FetchError) as excinfo:
            _fetch(configured_token_source(), quarantine)
        message = str(excinfo.value)
        assert TOKEN_VAR in message
        assert "unset or empty" in message
        assert not site.seen, "no request should be made without the token"

    def test_an_empty_variable_is_refused_by_name(
        self, site, quarantine, flat_archive, configured_token_source,
        monkeypatch,
    ):
        monkeypatch.setenv(TOKEN_VAR, "   ")
        site.serve("/demo.tar.gz", flat_archive)
        with pytest.raises(FetchError, match=TOKEN_VAR):
            _fetch(configured_token_source(), quarantine)

    def test_no_credential_is_sent_for_a_hand_written_url(
        self, site, quarantine, flat_archive, monkeypatch,
    ):
        monkeypatch.setenv(TOKEN_VAR, SECRET)
        url = site.serve("/demo.tar.gz", flat_archive)
        _fetch(_spec(url), quarantine)
        assert "authorization" not in site.headers_for("/demo.tar.gz")

    def test_a_same_origin_redirect_keeps_the_header(
        self, site, quarantine, flat_archive, configured_token_source,
        monkeypatch,
    ):
        monkeypatch.setenv(TOKEN_VAR, SECRET)
        site.serve("/final.tar.gz", flat_archive)
        site.redirect("/demo.tar.gz", "/final.tar.gz")

        _fetch(configured_token_source(), quarantine)
        assert site.headers_for("/final.tar.gz")["authorization"] == (
            f"Bearer {SECRET}"
        )

    def test_a_cross_origin_redirect_drops_the_header(
        self, site, other_site, quarantine, flat_archive,
        configured_token_source, monkeypatch,
    ):
        monkeypatch.setenv(TOKEN_VAR, SECRET)
        other_site.serve("/asset.tar.gz", flat_archive)
        site.redirect("/demo.tar.gz", other_site.url("/asset.tar.gz"))

        result = _fetch(configured_token_source(), quarantine)
        assert (result.tree / "manifest.yaml").is_file()

        # The token went to the origin it was minted for, and only there.
        assert site.headers_for("/demo.tar.gz")["authorization"] == (
            f"Bearer {SECRET}"
        )
        assert "authorization" not in other_site.headers_for("/asset.tar.gz")

    def test_a_stale_alias_falls_back_to_anonymous(
        self, site, quarantine, flat_archive, monkeypatch, caplog,
    ):
        url = site.serve("/demo.tar.gz", flat_archive)
        monkeypatch.setitem(config.CONFIG, "package_sources", [])
        spec = _spec(url, alias="gone")
        with caplog.at_level(logging.WARNING,
                             logger="carpenter.packages.fetch_archive"):
            _fetch(spec, quarantine)
        assert "no longer in package_sources" in caplog.text
        assert "authorization" not in site.headers_for("/demo.tar.gz")

    def test_a_broken_registry_is_reported(
        self, site, quarantine, flat_archive, monkeypatch,
    ):
        url = site.serve("/demo.tar.gz", flat_archive)
        monkeypatch.setitem(config.CONFIG, "package_sources", "not-a-list")
        with pytest.raises(FetchError, match="package_sources registry"):
            _fetch(_spec(url, alias="forge"), quarantine)


class TestTheSecretNeverLeaks:
    """The token must not reach an exception message or any log line."""

    @pytest.mark.parametrize("status", [401, 403, 404, 500])
    def test_http_failures_do_not_echo_the_token(
        self, site, quarantine, configured_token_source, monkeypatch,
        caplog, status,
    ):
        monkeypatch.setenv(TOKEN_VAR, SECRET)
        site.status("/demo.tar.gz", status)
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(FetchError) as excinfo:
                _fetch(configured_token_source(), quarantine)
        assert SECRET not in str(excinfo.value)
        assert SECRET not in repr(excinfo.value)
        assert SECRET not in caplog.text

    def test_a_digest_mismatch_does_not_echo_the_token(
        self, site, quarantine, flat_archive, configured_token_source,
        monkeypatch, caplog,
    ):
        monkeypatch.setenv(TOKEN_VAR, SECRET)
        site.serve("/demo.tar.gz", flat_archive)
        spec = configured_token_source()
        spec = SourceSpec(
            kind=spec.kind, raw=spec.raw, url=spec.url,
            subdir=spec.subdir, digest="2" * 64, alias=spec.alias,
        )
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(FetchError) as excinfo:
                _fetch(spec, quarantine)
        assert SECRET not in str(excinfo.value)
        assert SECRET not in caplog.text

    def test_a_successful_fetch_does_not_log_the_token(
        self, site, quarantine, flat_archive, configured_token_source,
        monkeypatch, caplog,
    ):
        monkeypatch.setenv(TOKEN_VAR, SECRET)
        site.serve("/demo.tar.gz", flat_archive)
        with caplog.at_level(logging.DEBUG):
            result = _fetch(configured_token_source(), quarantine)
        assert SECRET not in caplog.text
        assert SECRET not in repr(result)


# ── HTTP and transport failures ─────────────────────────────────────


class TestHttpFailures:
    def test_404_says_what_to_check(self, site, quarantine, no_token):
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(site.url("/missing.tar.gz")), quarantine)
        message = str(excinfo.value)
        assert "404" in message
        assert "release asset" in message

    def test_401_without_a_credential_points_at_credential_ref(
        self, site, quarantine, no_token,
    ):
        url = site.status("/private.tar.gz", 401)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url), quarantine)
        message = str(excinfo.value)
        assert "HTTP 401" in message
        assert "credential_ref" in message

    def test_403_with_a_credential_points_at_the_variable(
        self, site, quarantine, configured_token_source, monkeypatch,
    ):
        monkeypatch.setenv(TOKEN_VAR, SECRET)
        site.status("/demo.tar.gz", 403)
        with pytest.raises(FetchError) as excinfo:
            _fetch(configured_token_source(), quarantine)
        message = str(excinfo.value)
        assert "HTTP 403" in message
        assert TOKEN_VAR in message

    def test_429_is_a_rate_limit(self, site, quarantine):
        url = site.status("/demo.tar.gz", 429)
        with pytest.raises(FetchError, match="rate-limiting"):
            _fetch(_spec(url), quarantine)

    def test_500_blames_the_server(self, site, quarantine):
        url = site.status("/demo.tar.gz", 500)
        with pytest.raises(FetchError, match="the server's fault"):
            _fetch(_spec(url), quarantine)

    def test_a_truncated_body_is_detected(
        self, site, quarantine, flat_archive,
    ):
        url = site.serve(
            "/demo.tar.gz", flat_archive,
            content_length=len(flat_archive) * 2 + 100,
        )
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url), quarantine)
        assert "truncated" in str(excinfo.value)

    def test_an_empty_body_is_refused(self, site, quarantine):
        url = site.serve("/demo.tar.gz", b"")
        with pytest.raises(FetchError, match="empty body"):
            _fetch(_spec(url), quarantine)


class TestTransportFailureMessages:
    """Each transport failure gets its own actionable message."""

    LIMITS = FetchLimits()
    URL = "https://forge.example/pkg.tar.gz"

    def _message(self, exc: Exception) -> str:
        return str(
            fetch_archive._transport_failure(exc, self.URL, self.LIMITS),
        )

    def test_tls_failure(self):
        cause = ssl.SSLCertVerificationError("certificate verify failed")
        exc = httpx.ConnectError("certificate verify failed")
        exc.__cause__ = cause
        message = self._message(exc)
        assert "TLS verification failed" in message
        assert "CA certificate" in message

    def test_dns_failure(self):
        exc = httpx.ConnectError("[Errno -2] Name or service not known")
        exc.__cause__ = socket.gaierror(-2, "Name or service not known")
        message = self._message(exc)
        assert "could not resolve the host" in message
        assert "DNS" in message

    def test_plain_connection_failure(self):
        exc = httpx.ConnectError("[Errno 111] Connection refused")
        exc.__cause__ = ConnectionRefusedError(111, "Connection refused")
        message = self._message(exc)
        assert "could not connect to the host" in message

    def test_timeout(self):
        message = self._message(httpx.ReadTimeout("timed out"))
        assert "timed out fetching package archive" in message
        assert "timeout_s=" in message

    def test_truncated_body(self):
        message = self._message(
            httpx.RemoteProtocolError("peer closed connection"),
        )
        assert "was truncated" in message

    def test_read_error(self):
        message = self._message(httpx.ReadError("boom"))
        assert "failed mid-body" in message

    def test_anything_else(self):
        message = self._message(httpx.ProxyError("no proxy"))
        assert "could not fetch package archive" in message
        assert "ProxyError" in message

    def test_the_url_is_redacted(self):
        secret_url = "https://user:tok@forge.example/pkg.tar.gz"
        message = str(fetch_archive._transport_failure(
            httpx.ReadError("x"), secret_url, self.LIMITS,
        ))
        assert "tok" not in message
        assert "***@forge.example" in message


# ── unsupported and hostile archives ────────────────────────────────


class TestUnsupportedShapes:
    def test_a_zip_url_is_refused_before_any_request(self, site, quarantine):
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(site.url("/demo.zip")), quarantine)
        message = str(excinfo.value)
        assert "zip archives are not supported" in message
        assert ".tar.gz" in message
        assert not site.seen

    def test_zip_bytes_behind_a_tar_name_are_refused(self, site, quarantine):
        body = b"PK\x03\x04" + b"\0" * 64
        url = site.serve("/demo.tar.gz", body)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url), quarantine)
        assert "is a zip archive, whatever its file name says" in str(
            excinfo.value,
        )

    def test_a_body_that_is_not_a_tar_is_refused(self, site, quarantine):
        url = site.serve("/demo.tar.gz", b"this is not a tar archive\n" * 4)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url), quarantine)
        assert "could not be read as a tar archive" in str(excinfo.value)


class TestSafeExtractDoesTheRefusing:
    """Member safety is the shared extractor's job; prove it is used."""

    def _refused(self, site, quarantine, body: bytes) -> FetchError:
        url = site.serve("/evil.tar.gz", body)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url), quarantine)
        error = excinfo.value
        assert "refused by the archive safety check" in str(error)
        assert isinstance(error.__cause__, ArchiveVerificationError)
        return error

    def test_a_symlink_member_is_refused(self, site, quarantine):
        link = tarfile.TarInfo("escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../../../etc/passwd"
        link.mtime = 0
        body = _tar_gz([_manifest_member(), (link, None)])
        error = self._refused(site, quarantine, body)
        assert "unsafe archive member" in str(error)
        assert "escape" in str(error)

    def test_a_hardlink_member_is_refused(self, site, quarantine):
        link = tarfile.TarInfo("hard")
        link.type = tarfile.LNKTYPE
        link.linkname = "manifest.yaml"
        link.mtime = 0
        body = _tar_gz([_manifest_member(), (link, None)])
        self._refused(site, quarantine, body)

    def test_a_device_node_member_is_refused(self, site, quarantine):
        device = tarfile.TarInfo("dev/null")
        device.type = tarfile.CHRTYPE
        device.devmajor = 1
        device.devminor = 3
        device.mtime = 0
        body = _tar_gz([_manifest_member(), (device, None)])
        self._refused(site, quarantine, body)

    def test_an_absolute_member_path_is_refused(self, site, quarantine):
        info = tarfile.TarInfo("/etc/cron.d/evil")
        info.size = 4
        info.mtime = 0
        body = _tar_gz([_manifest_member(), (info, b"evil")])
        error = self._refused(site, quarantine, body)
        assert "absolute path" in str(error)

    def test_a_path_escape_is_refused(self, site, quarantine):
        info = tarfile.TarInfo("../outside.txt")
        info.size = 1
        info.mtime = 0
        body = _tar_gz([_manifest_member(), (info, b"x")])
        error = self._refused(site, quarantine, body)
        assert "path escape" in str(error)


# ── through the dispatcher ──────────────────────────────────────────


@pytest.fixture
def isolated_fetchers():
    saved = dict(fetch._FETCHERS)
    yield
    fetch._FETCHERS.clear()
    fetch._FETCHERS.update(saved)


class TestThroughTheDispatcher:
    def test_end_to_end(
        self, site, tmp_path, flat_archive, isolated_fetchers, monkeypatch,
    ):
        """``fetch_source`` + the real fetcher + the result validator."""
        monkeypatch.setitem(config.CONFIG, "base_dir", str(tmp_path / "base"))
        register_fetcher(ArchiveUrlFetcher())
        url = site.serve("/demo.tar.gz", flat_archive)
        spec = parse_source(
            f"{url}#sha256={hashlib.sha256(flat_archive).hexdigest()}",
        )
        assert spec.kind == "archive"

        result = fetch_source(spec)
        try:
            assert (result.tree / "manifest.yaml").is_file()
            assert result.staging is not None
            assert result.staging.name.startswith("_fetch-")
            assert result.resolved == hashlib.sha256(
                flat_archive,
            ).hexdigest()
        finally:
            cleanup(result)
        assert result.staging is not None
        assert not result.staging.exists()

    def test_the_dispatcher_removes_the_quarantine_on_failure(
        self, site, tmp_path, isolated_fetchers, monkeypatch,
    ):
        monkeypatch.setitem(config.CONFIG, "base_dir", str(tmp_path / "base"))
        register_fetcher(ArchiveUrlFetcher())
        url = site.serve("/demo.tar.gz", b"not a tar")
        with pytest.raises(FetchError):
            fetch_source(parse_source(url))
        staging_root = tmp_path / "base" / "cache" / "package-staging"
        assert list(staging_root.iterdir()) == []

    def test_a_non_allowlisted_host_never_reaches_the_fetcher(
        self, tmp_path, isolated_fetchers, monkeypatch,
    ):
        """The dispatcher owns the gate on the initial URL."""
        monkeypatch.setitem(config.CONFIG, "base_dir", str(tmp_path / "base"))
        monkeypatch.setitem(config.CONFIG, "web_egress_allowlist", [])
        register_fetcher(ArchiveUrlFetcher())
        with pytest.raises(FetchError, match="web_egress_allowlist"):
            fetch_source(parse_source("http://127.0.0.1:9/demo.tar.gz"))


# ── origin comparison ──────────────────────────────────────────────


class TestOrigin:
    @pytest.mark.parametrize(
        "left,right,same",
        [
            ("https://h/a", "https://h/b", True),
            ("https://h:443/a", "https://h/b", True),
            ("http://h:80/a", "http://h/b", True),
            ("https://h/a", "http://h/a", False),
            ("https://h/a", "https://h:8443/a", False),
            ("https://h/a", "https://other/a", False),
            ("https://H/a", "https://h/a", True),
        ],
    )
    def test_origins(self, left, right, same):
        assert (
            fetch_archive._origin(left) == fetch_archive._origin(right)
        ) is same


# ── the ambient proxy environment (F8) ──────────────────────────────


def _dead_port() -> int:
    """A port nothing is listening on."""
    sock = socket.socket()
    sock.bind((LOOPBACK, 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestProxyEnvironmentIsIgnored:
    """What the egress guard checked has to be what gets connected to."""

    def test_a_set_proxy_does_not_change_where_the_fetch_goes(
        self, site, quarantine, flat_archive, monkeypatch,
    ):
        dead = f"http://{LOOPBACK}:{_dead_port()}"
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                     "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.setenv(name, dead)
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)

        url = site.serve("/demo.tar.gz", flat_archive)
        result = _fetch(_spec(url), quarantine)

        assert (result.tree / "manifest.yaml").is_file()
        # The bytes came from the host the guard checked, not the proxy.
        assert site.headers_for("/demo.tar.gz")


# ── the index is walked lazily (F2) ─────────────────────────────────


def _counting_tarfile_open(monkeypatch) -> list[str]:
    """Record every member the extractor's tar reader actually visits."""
    visited: list[str] = []
    real_open = tarfile.open

    def counting_open(*args, **kwargs):
        tar = real_open(*args, **kwargs)
        real_next = tar.next

        def next_counting():
            info = real_next()
            if info is not None:
                visited.append(info.name)
            return info

        tar.next = next_counting
        return tar

    monkeypatch.setattr(fetch_archive.tarfile, "open", counting_open)
    return visited


class TestMemberCountIsEnforcedLazily:
    """F2: the member count must bite before the index is materialised.

    The shape is the reviewer's: zero-byte members, so no member
    breaches ``max_file_bytes`` and the declared content total is 0 —
    only the *count* is over.  A real bomb is ~30M members at the
    download cap; this proves the mechanism (the walk stops at the cap)
    on a few hundred, which is the only way to test it without
    allocating what it was designed to allocate.
    """

    def test_the_count_is_refused_without_reading_every_member(
        self, site, quarantine, monkeypatch,
    ):
        visited = _counting_tarfile_open(monkeypatch)
        body = _tar_gz(
            [_manifest_member()]
            + [_file_member(f"f{index}.txt", b"") for index in range(400)],
        )
        url = site.serve("/bomb.tar.gz", body)

        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url), quarantine, max_files=10)

        message = str(excinfo.value)
        assert "max_files" in message
        assert "10" in message
        # The walk stopped at the cap instead of indexing all 401
        # members: that difference is the whole finding.
        assert len(visited) <= 12, visited
        assert list((quarantine / "_tree").iterdir()) == []

    def test_a_legitimate_archive_still_extracts_every_member(
        self, site, quarantine, monkeypatch,
    ):
        visited = _counting_tarfile_open(monkeypatch)
        body = _tar_gz(
            [_manifest_member()]
            + [_file_member(f"f{index}.txt", b"x") for index in range(20)],
        )
        url = site.serve("/ok.tar.gz", body)

        result = _fetch(_spec(url), quarantine, max_files=50)

        assert result.files_fetched == 21
        assert (result.tree / "f19.txt").read_text() == "x"
        assert len(visited) == 21


# ── the index root a listing fetches (require_manifest=False) ───────


@pytest.fixture
def index_archive(tmp_path) -> bytes:
    """A monorepo tarball: ``packages/`` holding three packages."""
    root = _fresh(tmp_path / "mono-index")
    for name in ("alpha", "beta", "gamma"):
        write_package(
            root / "packages", name=name,
            manifest_yaml=MANIFEST.replace("demo", name),
            files={"tools.py": f"NAME = {name!r}\n"},
        )
    (root / "packages" / "notes").mkdir()
    (root / "packages" / "notes" / "readme.txt").write_text("not a package\n")
    (root / "README.md").write_text("a self-hosted forge monorepo\n")
    return _archive_bytes(root, tmp_path / "mono-index.tar.gz")


@pytest.fixture
def wrapped_index_archive(tmp_path) -> bytes:
    """The same monorepo in one top-level directory, as a forge serves it."""
    root = _fresh(tmp_path / "wrapped")
    inner = root / "packages-1.0"
    for name in ("alpha", "beta"):
        write_package(
            inner / "packages", name=name,
            manifest_yaml=MANIFEST.replace("demo", name),
        )
    (inner / "README.md").write_text("wrapped\n")
    return _archive_bytes(root, tmp_path / "wrapped.tar.gz")


class TestIndexRoot:
    """``require_manifest=False`` buys the index root and nothing else."""

    def test_the_index_root_is_refused_by_default(
        self, site, quarantine, index_archive,
    ):
        url = site.serve("/mono.tar.gz", index_archive)
        with pytest.raises(FetchError, match="holds no manifest.yaml"):
            _fetch(_spec(url, subdir="packages"), quarantine)

    def test_the_index_root_is_returned_when_it_is_not_required(
        self, site, quarantine, index_archive,
    ):
        url = site.serve("/mono.tar.gz", index_archive)
        result = _fetch(
            _spec(url, subdir="packages"), quarantine, require_manifest=False,
        )

        assert result.tree.name == "packages"
        assert not (result.tree / "manifest.yaml").exists()
        assert sorted(p.name for p in result.tree.iterdir()) == [
            "alpha", "beta", "gamma", "notes",
        ]
        assert (result.tree / "beta" / "manifest.yaml").is_file()
        assert result.resolved == hashlib.sha256(index_archive).hexdigest()
        assert result.tree.resolve().is_relative_to(quarantine.resolve())

    def test_a_wrapper_directory_is_still_unwrapped_first(
        self, site, quarantine, wrapped_index_archive,
    ):
        """The subdir applies under the single top-level directory."""
        url = site.serve("/wrapped.tar.gz", wrapped_index_archive)
        result = _fetch(
            _spec(url, subdir="packages"), quarantine, require_manifest=False,
        )
        assert result.tree.parent.name == "packages-1.0"
        assert sorted(p.name for p in result.tree.iterdir()) == [
            "alpha", "beta",
        ]

    def test_a_missing_index_root_is_still_refused(
        self, site, quarantine, index_archive,
    ):
        url = site.serve("/mono.tar.gz", index_archive)
        with pytest.raises(FetchError, match="has no directory 'absent'"):
            _fetch(_spec(url, subdir="absent"), quarantine,
                   require_manifest=False)

    def test_a_subdir_escaping_the_archive_is_still_refused(
        self, site, quarantine, index_archive,
    ):
        url = site.serve("/mono.tar.gz", index_archive)
        with pytest.raises(FetchError, match="points outside"):
            _fetch(_spec(url, subdir="../../etc"), quarantine,
                   require_manifest=False)

    # ── the caps still bite ──────────────────────────────────────────

    def test_the_file_count_cap_still_bites(
        self, site, quarantine, index_archive,
    ):
        url = site.serve("/mono.tar.gz", index_archive)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url, subdir="packages"), quarantine,
                   require_manifest=False, max_files=3)
        assert "max_files limit of 3" in str(excinfo.value)

    def test_the_declared_length_cap_still_bites(
        self, site, quarantine, index_archive,
    ):
        url = site.serve("/mono.tar.gz", index_archive)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url, subdir="packages"), quarantine,
                   require_manifest=False, max_bytes=16)
        assert "max_bytes" in str(excinfo.value)

    def test_the_per_file_cap_still_bites(self, site, quarantine, tmp_path):
        root = _fresh(tmp_path / "fat-index")
        write_package(
            root / "packages", name="alpha",
            manifest_yaml=MANIFEST.replace("demo", "alpha"),
            files={"fat.txt": "b" * 5_000},
        )
        body = _archive_bytes(root, tmp_path / "fat-index.tar.gz")
        url = site.serve("/fat.tar.gz", body)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url, subdir="packages"), quarantine,
                   require_manifest=False, max_file_bytes=1_000)
        assert "max_file_bytes" in str(excinfo.value)

    def test_the_wall_clock_budget_still_bites(self, quarantine):
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec("https://nowhere.invalid/mono.tar.gz",
                         subdir="packages"),
                   quarantine, require_manifest=False, timeout_s=0.0)
        assert "timeout_s" in str(excinfo.value)

    # ── integrity and member hardening still bite ────────────────────

    def test_a_digest_mismatch_is_still_refused(
        self, site, quarantine, index_archive,
    ):
        url = site.serve("/mono.tar.gz", index_archive)
        spec = _spec(url, subdir="packages", digest="0" * 64)
        with pytest.raises(FetchError, match="sha256"):
            _fetch(spec, quarantine, require_manifest=False)
        assert list(quarantine.rglob("manifest.yaml")) == []

    def test_an_unsafe_member_is_still_refused(self, site, quarantine):
        link = tarfile.TarInfo("packages/alpha/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../../../etc/passwd"
        link.mtime = 0
        body = _tar_gz([
            _manifest_member("packages/alpha/"),
            (link, None),
        ])
        url = site.serve("/evil.tar.gz", body)
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(url, subdir="packages"), quarantine,
                   require_manifest=False)
        message = str(excinfo.value)
        assert "refused by the archive safety check" in message
        assert "unsafe archive member" in message

    def test_a_redirect_to_a_refused_address_is_still_refused(
        self, site, quarantine,
    ):
        start = site.redirect(
            "/mono.tar.gz", "http://169.254.169.254/x.tar.gz",
        )
        with pytest.raises(FetchError) as excinfo:
            _fetch(_spec(start, subdir="packages"), quarantine,
                   require_manifest=False)
        assert "refusing to continue the redirect chain" in str(excinfo.value)
        assert [path for path, _ in site.seen] == ["/mono.tar.gz"]

    def test_the_ambient_proxy_is_still_ignored(
        self, site, quarantine, index_archive, monkeypatch,
    ):
        dead = f"http://{LOOPBACK}:{_dead_port()}"
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                     "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.setenv(name, dead)
        url = site.serve("/mono.tar.gz", index_archive)
        result = _fetch(_spec(url, subdir="packages"), quarantine,
                        require_manifest=False)
        assert (result.tree / "alpha" / "manifest.yaml").is_file()
        assert site.headers_for("/mono.tar.gz")
