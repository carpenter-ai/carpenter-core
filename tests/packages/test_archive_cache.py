"""Tests for the pristine-archive cache + hash-verification layer.

Covers round-trip archive/load, hash verification (tamper rejection),
the fetcher cache-miss path (good + bad), path-traversal rejection, and
``cache_dir()`` resolving under ``base_dir``.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from carpenter.packages import archive_cache
from carpenter.packages.archive_cache import (
    ArchiveCacheError,
    ArchiveFetcher,
    ArchiveVerificationError,
    archive_tree,
    cache_dir,
    load_pristine_tree,
    store_archive,
)
from carpenter.packages.installer import compute_package_hash


# ── helpers ──────────────────────────────────────────────────────────


def _make_tree(root: Path) -> dict[str, bytes]:
    """Materialize a small synthetic package tree; return path->bytes."""
    contents: dict[str, bytes] = {
        "manifest.yaml": b"name: demo\nversion: 1.0.0\n",
        "tools/hello.py": b"def hello():\n    return 'hi'\n",
        "data/blob.bin": bytes(range(256)),
        "nested/deep/note.txt": "unicode: \u2603\n".encode("utf-8"),
    }
    for rel, data in contents.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return contents


@pytest.fixture
def cache_base(tmp_path, monkeypatch):
    """Point CONFIG['base_dir'] at an isolated temp dir for the cache."""
    base = tmp_path / "carpenter-base"
    base.mkdir()
    monkeypatch.setattr(
        "carpenter.config.CONFIG", {"base_dir": str(base)},
    )
    return base


# ── cache_dir ────────────────────────────────────────────────────────


def test_cache_dir_under_base_dir_and_created(cache_base):
    cdir = cache_dir()
    assert cdir == cache_base / "cache" / "package-archives"
    assert cdir.is_dir()


def test_cache_dir_requires_base_dir(monkeypatch):
    monkeypatch.setattr("carpenter.config.CONFIG", {})
    with pytest.raises(ArchiveCacheError):
        cache_dir()


# ── round-trip ───────────────────────────────────────────────────────


def test_round_trip_contents_and_hash(cache_base, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    original = _make_tree(src)
    expected_hash = compute_package_hash(src)

    out = tmp_path / "demo.tar.gz"
    returned_hash = archive_tree(src, out)
    assert returned_hash == expected_hash
    assert out.is_file()

    tree = archive_cache._expand_to_tree(out)
    assert tree == original


def test_store_archive_then_load(cache_base, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    original = _make_tree(src)
    root_hash = compute_package_hash(src)

    path = store_archive("demo", "1.0.0", src)
    assert path == cache_dir() / "demo" / "1.0.0.tar.gz"
    assert path.is_file()

    tree = load_pristine_tree("demo", "1.0.0", root_hash)
    assert tree == original


def test_archive_tree_is_deterministic(cache_base, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_tree(src)
    out1 = tmp_path / "a.tar.gz"
    out2 = tmp_path / "b.tar.gz"
    archive_tree(src, out1)
    archive_tree(src, out2)
    assert out1.read_bytes() == out2.read_bytes()


def test_archive_tree_rejects_non_dir(tmp_path):
    with pytest.raises(ArchiveCacheError):
        archive_tree(tmp_path / "nope", tmp_path / "out.tar.gz")


# ── verification ─────────────────────────────────────────────────────


def test_wrong_expected_hash_raises(cache_base, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_tree(src)
    store_archive("demo", "1.0.0", src)

    with pytest.raises(ArchiveVerificationError):
        load_pristine_tree("demo", "1.0.0", "0" * 64)


def test_tampered_cache_file_raises(cache_base, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_tree(src)
    root_hash = compute_package_hash(src)
    store_archive("demo", "1.0.0", src)

    # Tamper: overwrite the cached archive with a different tree.
    other = tmp_path / "other"
    other.mkdir()
    (other / "manifest.yaml").write_bytes(b"name: evil\n")
    cached = cache_dir() / "demo" / "1.0.0.tar.gz"
    archive_tree(other, cached)

    with pytest.raises(ArchiveVerificationError):
        load_pristine_tree("demo", "1.0.0", root_hash)


# ── fetcher path ─────────────────────────────────────────────────────


class _StubFetcher:
    """Returns a pre-prepared archive path; records call count."""

    def __init__(self, archive_path: Path):
        self.archive_path = archive_path
        self.calls = 0

    def fetch(self, name: str, version: str) -> Path:
        self.calls += 1
        return self.archive_path


def test_fetcher_is_runtime_checkable():
    archive = Path("/tmp/whatever.tar.gz")
    assert isinstance(_StubFetcher(archive), ArchiveFetcher)


def test_cache_miss_triggers_fetch_caches_and_verifies(cache_base, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    original = _make_tree(src)
    root_hash = compute_package_hash(src)

    prepared = tmp_path / "downloaded.tar.gz"
    archive_tree(src, prepared)
    fetcher = _StubFetcher(prepared)

    tree = load_pristine_tree(
        "demo", "2.0.0", root_hash, fetcher=fetcher,
    )
    assert tree == original
    assert fetcher.calls == 1

    # The verified archive was cached: a second load needs no fetch.
    cached = cache_dir() / "demo" / "2.0.0.tar.gz"
    assert cached.is_file()
    tree2 = load_pristine_tree("demo", "2.0.0", root_hash, fetcher=fetcher)
    assert tree2 == original
    assert fetcher.calls == 1  # unchanged — served from cache


def test_fetcher_hash_mismatch_raises_and_not_cached(cache_base, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_tree(src)
    real_hash = compute_package_hash(src)

    # Prepare an archive of a DIFFERENT tree, so it won't match real_hash.
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "manifest.yaml").write_bytes(b"name: bad\n")
    prepared = tmp_path / "bad.tar.gz"
    archive_tree(bad, prepared)
    fetcher = _StubFetcher(prepared)

    with pytest.raises(ArchiveVerificationError):
        load_pristine_tree("demo", "3.0.0", real_hash, fetcher=fetcher)

    # A bad fetch must not poison the cache.
    cached = cache_dir() / "demo" / "3.0.0.tar.gz"
    assert not cached.exists()


def test_cache_miss_no_fetcher_raises(cache_base):
    with pytest.raises(ArchiveCacheError):
        load_pristine_tree("demo", "9.9.9", "0" * 64)


def test_fetcher_returns_missing_path_raises(cache_base, tmp_path):
    fetcher = _StubFetcher(tmp_path / "does-not-exist.tar.gz")
    with pytest.raises(ArchiveCacheError):
        load_pristine_tree("demo", "4.0.0", "0" * 64, fetcher=fetcher)


# ── path traversal ───────────────────────────────────────────────────


def _write_malicious_archive(out: Path, member_name: str) -> None:
    """Write a .tar.gz whose single member has ``member_name``."""
    with tarfile.open(str(out), mode="w:gz") as tar:
        data = b"pwned\n"
        info = tarfile.TarInfo(name=member_name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))


def test_path_traversal_dotdot_rejected(cache_base, tmp_path):
    cached = cache_dir() / "demo" / "1.0.0.tar.gz"
    cached.parent.mkdir(parents=True, exist_ok=True)
    _write_malicious_archive(cached, "../escape.txt")
    with pytest.raises(ArchiveVerificationError):
        load_pristine_tree("demo", "1.0.0", "0" * 64)


def test_path_traversal_absolute_rejected(cache_base, tmp_path):
    cached = cache_dir() / "demo" / "1.0.0.tar.gz"
    cached.parent.mkdir(parents=True, exist_ok=True)
    _write_malicious_archive(cached, "/etc/evil.txt")
    with pytest.raises(ArchiveVerificationError):
        load_pristine_tree("demo", "1.0.0", "0" * 64)


def test_symlink_member_rejected(cache_base, tmp_path):
    cached = cache_dir() / "demo" / "1.0.0.tar.gz"
    cached.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(str(cached), mode="w:gz") as tar:
        info = tarfile.TarInfo(name="link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(ArchiveVerificationError):
        load_pristine_tree("demo", "1.0.0", "0" * 64)
