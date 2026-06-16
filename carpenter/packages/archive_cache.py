"""Local pristine-archive cache + hash-verification layer (reconcile SD).

The three-way reconcile engine (:mod:`carpenter.packages.reconcile`) needs
the *pristine* trees a package version shipped — both the OLD installed
version and the NEW version — to diff against the user's CURRENT on-disk
copy.  This module is the storage/verification layer that produces those
pristine trees as ``path -> bytes`` mappings suitable for feeding
:func:`carpenter.packages.reconcile.classify`.

Design
------
* Each package version's pristine tree is stored as a deterministic
  gzipped tar archive in a local cache under
  ``{base_dir}/cache/package-archives/<name>/<version>.tar.gz``.  This
  cache lives on the HDD/SSD (``base_dir``), **not** the SD card.
* The remote host that supplies an archive (a GitHub release asset,
  etc.) is **untrusted storage**.  The trust anchor is the local root
  hash already recorded in ``installed_packages.hash`` and computed by
  :func:`carpenter.packages.installer.compute_package_hash`.  An archive
  is only trusted if the tree it expands to recomputes to the expected
  root hash; otherwise its contents are never returned.
* Remote fetching is intentionally NOT implemented here.  A platform
  package (e.g. ``carpenter-linux``) supplies a concrete
  :class:`ArchiveFetcher` in a later PR.  This module only defines the
  Protocol so :func:`load_pristine_tree` can accept one.

Safety
------
Tar extraction is guarded against path traversal: members with absolute
paths, ``..`` components escaping the extract root, or non-file/dir types
(devices, fifos, symlinks/hardlinks pointing outside the tree) are
rejected.  Expansion happens in a temporary directory that honours
``TMPDIR`` (the repo sets ``TMPDIR=/dev/shm`` for test/workspace runs to
keep temp churn off the SD card).
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

from .installer import _iter_files, compute_package_hash

logger = logging.getLogger(__name__)

__all__ = [
    "ArchiveCacheError",
    "ArchiveVerificationError",
    "ArchiveFetcher",
    "archive_tree",
    "cache_dir",
    "store_archive",
    "load_pristine_tree",
]


class ArchiveCacheError(Exception):
    """Raised when an archive cache operation fails."""


class ArchiveVerificationError(ArchiveCacheError):
    """Raised when an expanded archive does not match its expected hash.

    A distinct subclass so callers can distinguish "the archive is
    untrusted / corrupt / tampered" from generic I/O failures.  Contents
    that fail verification are NEVER returned to the caller.
    """


@runtime_checkable
class ArchiveFetcher(Protocol):
    """Supplies a downloaded archive for a package version.

    Implementations live in the platform layer (e.g. ``carpenter-linux``
    fetching a GitHub release asset).  ``fetch`` returns a local path to
    a ``.tar.gz`` archive; this module then caches, expands, and verifies
    it against the expected root hash.  The fetcher is untrusted: a
    returned archive whose expanded tree fails hash verification is
    rejected.
    """

    def fetch(self, name: str, version: str) -> Path:
        """Return a local path to a downloaded archive for name@version."""
        ...


# ── deterministic archive creation ──────────────────────────────────


def archive_tree(source_dir: Path, out_path: Path) -> str:
    """Create a deterministic ``.tar.gz`` of ``source_dir`` at ``out_path``.

    The archive carries the package tree faithfully; verification is by
    re-expanding and recomputing :func:`compute_package_hash`, so the
    archive bytes need not themselves be hashed.  Creation is made
    deterministic anyway (sorted entries; normalized mtime/uid/gid/mode;
    no gzip mtime) so the same tree always yields the same bytes — useful
    for content-addressing and reproducible builds.

    The same files the installer's hash ignores (``__pycache__``, ``.pyc``,
    ``.git``, editor cruft) are excluded here too, via
    :func:`carpenter.packages.installer._iter_files`, so the archived tree
    matches what ``compute_package_hash`` measured.

    Args:
        source_dir: Package directory (parent of ``manifest.yaml``).
        out_path: Destination ``.tar.gz`` path.  Parent dirs are created.

    Returns:
        The tree's root hash (``compute_package_hash(source_dir)``).

    Raises:
        ArchiveCacheError: ``source_dir`` is not a directory.
    """
    source_dir = Path(source_dir).resolve()
    if not source_dir.is_dir():
        raise ArchiveCacheError(
            f"archive_tree: {source_dir} is not a directory",
        )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    root_hash = compute_package_hash(source_dir)

    files = _iter_files(source_dir)
    # _iter_files already sorts within each directory; re-sort the flat
    # list by POSIX relative path for a fully deterministic member order.
    rel_paths = sorted(
        p.relative_to(source_dir).as_posix() for p in files
    )

    # Write to a temp file then atomically rename into place so a crash
    # mid-write never leaves a truncated archive in the cache.
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=out_path.name + ".", dir=str(out_path.parent),
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        # mtime=0 on the GzipFile strips the gzip header timestamp so the
        # compressed bytes are reproducible.
        with open(tmp_path, "wb") as raw:
            import gzip

            with gzip.GzipFile(
                filename="", fileobj=raw, mode="wb", mtime=0,
            ) as gz:
                with tarfile.open(fileobj=gz, mode="w") as tar:
                    for rel in rel_paths:
                        abs_path = source_dir / rel
                        info = tarfile.TarInfo(name=rel)
                        data = abs_path.read_bytes()
                        info.size = len(data)
                        # Normalize metadata for reproducibility.
                        info.mtime = 0
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mode = 0o644
                        info.type = tarfile.REGTYPE
                        tar.addfile(info, io.BytesIO(data))
        os.replace(str(tmp_path), str(out_path))
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    return root_hash


# ── cache layout ────────────────────────────────────────────────────


def cache_dir() -> Path:
    """Return the local archive-cache root, creating it if missing.

    Defaults to ``{base_dir}/cache/package-archives/`` where ``base_dir``
    comes from ``carpenter.config.CONFIG['base_dir']``.  This path is on
    the HDD/SSD, not the SD card.
    """
    from .. import config

    base_dir = config.CONFIG.get("base_dir")
    if not base_dir:
        raise ArchiveCacheError(
            "cache_dir: config 'base_dir' is not set",
        )
    root = Path(base_dir) / "cache" / "package-archives"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _archive_path(name: str, version: str) -> Path:
    """Cache path for a package version's archive: ``<name>/<version>.tar.gz``."""
    return cache_dir() / name / f"{version}.tar.gz"


def store_archive(name: str, version: str, source_dir: Path) -> Path:
    """Archive a package's pristine tree into the cache.

    Writes ``<cache>/<name>/<version>.tar.gz`` and returns its path.
    This is what the install flow would call to capture a pristine
    snapshot — but it is intentionally NOT wired into install in this PR.

    Args:
        name: Package name.
        version: Package version string.
        source_dir: The package directory to snapshot.

    Returns:
        Path to the stored archive.
    """
    dest = _archive_path(name, version)
    archive_tree(Path(source_dir), dest)
    logger.info(
        "Stored pristine archive for %s@%s at %s", name, version, dest,
    )
    return dest


# ── safe extraction ─────────────────────────────────────────────────


def _is_within(base: Path, target: Path) -> bool:
    """True if ``target`` is ``base`` or lives under it (no escape)."""
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract ``tar`` into ``dest``, rejecting any unsafe member.

    Rejected: absolute member paths, paths with ``..`` that escape
    ``dest``, symlinks/hardlinks, and any non-regular-file / non-directory
    member (devices, fifos, etc.).  This is a fail-closed allowlist:
    anything not explicitly a plain file or directory inside ``dest`` is
    refused.
    """
    dest = dest.resolve()
    for member in tar.getmembers():
        member_name = member.name
        # Reject absolute paths outright.
        if member_name.startswith("/") or os.path.isabs(member_name):
            raise ArchiveVerificationError(
                f"unsafe archive member (absolute path): {member_name!r}",
            )
        # Reject special member types — only files and dirs are allowed.
        if not (member.isfile() or member.isdir()):
            raise ArchiveVerificationError(
                f"unsafe archive member (type {member.type!r}): "
                f"{member_name!r}",
            )
        target = (dest / member_name).resolve()
        if not _is_within(dest, target):
            raise ArchiveVerificationError(
                f"unsafe archive member (path escape): {member_name!r}",
            )
    # All members vetted; extract.  (Members were validated above, so the
    # bandit B202 tarfile-extractall concern is addressed by the guard.)
    tar.extractall(dest)  # noqa: S202


def _expand_to_tree(archive_path: Path) -> dict[str, bytes]:
    """Expand an archive into a ``path -> bytes`` mapping (safely).

    Uses a temp dir honouring ``TMPDIR`` (e.g. ``/dev/shm``), extracts
    with :func:`_safe_extract`, then reads each regular file back as
    bytes keyed by its POSIX relative path.  The temp dir is always
    cleaned up.
    """
    archive_path = Path(archive_path)
    if not archive_path.is_file():
        raise ArchiveCacheError(
            f"archive {archive_path} does not exist",
        )
    # tempfile.mkdtemp honours $TMPDIR; the repo sets TMPDIR=/dev/shm to
    # keep temp churn off the SD card.
    tmp_root = Path(tempfile.mkdtemp(prefix="carpenter-archive-"))
    try:
        try:
            with tarfile.open(str(archive_path), mode="r:*") as tar:
                _safe_extract(tar, tmp_root)
        except tarfile.TarError as exc:
            raise ArchiveCacheError(
                f"failed to read archive {archive_path}: {exc}",
            ) from exc
        tree: dict[str, bytes] = {}
        for path in _iter_files(tmp_root):
            rel = path.relative_to(tmp_root).as_posix()
            tree[rel] = path.read_bytes()
        return tree
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _verify_archive_tree(
    archive_path: Path, expected_root_hash: str,
) -> dict[str, bytes]:
    """Expand + verify an archive against ``expected_root_hash``.

    Re-extracts the archive into a temp dir, recomputes
    :func:`compute_package_hash` over the expanded tree, and only returns
    the ``path -> bytes`` mapping if it matches.  On mismatch raises
    :class:`ArchiveVerificationError` and returns nothing.
    """
    # Expand once to a temp dir to compute the hash over the real tree
    # (compute_package_hash walks a directory), then read bytes back.
    tmp_root = Path(tempfile.mkdtemp(prefix="carpenter-archive-verify-"))
    try:
        try:
            with tarfile.open(str(archive_path), mode="r:*") as tar:
                _safe_extract(tar, tmp_root)
        except tarfile.TarError as exc:
            raise ArchiveCacheError(
                f"failed to read archive {archive_path}: {exc}",
            ) from exc
        actual = compute_package_hash(tmp_root)
        if actual != expected_root_hash:
            raise ArchiveVerificationError(
                f"archive {archive_path} hash mismatch: expected "
                f"{expected_root_hash[:12]}..., got {actual[:12]}...",
            )
        tree: dict[str, bytes] = {}
        for path in _iter_files(tmp_root):
            rel = path.relative_to(tmp_root).as_posix()
            tree[rel] = path.read_bytes()
        return tree
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def load_pristine_tree(
    name: str,
    version: str,
    expected_root_hash: str,
    *,
    fetcher: "ArchiveFetcher | None" = None,
) -> dict[str, bytes]:
    """Return a package version's pristine tree as ``path -> bytes``.

    Resolution order:

    1. **Local cache hit** — if ``<cache>/<name>/<version>.tar.gz`` exists,
       expand it and verify it recomputes to ``expected_root_hash``.
    2. **Fetch** — if missing and a ``fetcher`` is supplied, call
       ``fetcher.fetch(name, version)`` to obtain a downloaded archive,
       place it in the cache, then expand + verify.
    3. **Fail** — if neither yields a hash-matching tree, raise.

    The returned mapping is suitable for feeding
    :func:`carpenter.packages.reconcile.classify`.

    Contents that fail hash verification are NEVER returned.

    Args:
        name: Package name.
        version: Package version string.
        expected_root_hash: The trusted root hash (from
            ``installed_packages.hash``) the expanded tree must match.
        fetcher: Optional :class:`ArchiveFetcher` used only on a cache
            miss.

    Returns:
        The pristine tree as ``{posix_rel_path: bytes}``.

    Raises:
        ArchiveVerificationError: an available archive failed hash
            verification.
        ArchiveCacheError: no archive is available (cache miss + no
            fetcher, or the fetcher produced nothing usable).
    """
    cached = _archive_path(name, version)
    if cached.is_file():
        # A cached archive that fails verification is fatal: it means the
        # cache is corrupt or tampered, and we must never serve its bytes.
        return _verify_archive_tree(cached, expected_root_hash)

    if fetcher is None:
        raise ArchiveCacheError(
            f"no cached archive for {name}@{version} at {cached} and no "
            f"fetcher provided",
        )

    fetched = Path(fetcher.fetch(name, version))
    if not fetched.is_file():
        raise ArchiveCacheError(
            f"fetcher returned a non-existent path for {name}@{version}: "
            f"{fetched}",
        )

    # Verify the fetched archive BEFORE trusting it, then cache it only if
    # it passes.  A hash-mismatched fetch raises and is not cached, so a
    # bad remote can never poison the cache.
    tree = _verify_archive_tree(fetched, expected_root_hash)

    cached.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(str(fetched), str(cached))
    except OSError:
        logger.warning(
            "verified archive for %s@%s but failed to cache it at %s",
            name, version, cached,
        )
    return tree
