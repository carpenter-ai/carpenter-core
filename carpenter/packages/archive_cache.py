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
  ``{base_dir}/cache/package-archives/<name>/<source_id>/<version>.tar.gz``
  (and, for archives written before the ``source_id`` level existed,
  ``<name>/<version>.tar.gz``, which is still read).  This cache lives
  under ``base_dir``, **not** on the root filesystem, which may be slow
  flash storage.  A package's whole ``<name>/`` subtree is reclaimed on
  uninstall (:func:`reclaim_package_archives`), so the cache cannot grow
  without bound across installs, re-installs and upgrades of packages
  that are no longer installed.
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
keep temp churn off slow flash storage).
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
    "archive_candidates",
    "archive_tree",
    "cache_dir",
    "reclaim_package_archives",
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
    comes from ``carpenter.config.CONFIG['base_dir']``.  This path is under
    ``base_dir`` (typically a data volume rather than the root filesystem).
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


#: Infix for an archive that was moved aside rather than overwritten:
#: ``<version>.superseded-<root hash prefix>.tar.gz``.  See
#: :func:`store_archive`.
_SUPERSEDED_INFIX = ".superseded-"


def _package_cache_dir(name: str, source_id: str | None = None) -> Path:
    """Return the cache directory for a package, per layout.

    ``<cache>/<name>/<source_id>/`` is the CURRENT layout; the
    ``source_id``-less ``<cache>/<name>/`` is the OLD one, still read.
    Neither is created here.
    """
    root = cache_dir() / name
    return root / source_id if source_id else root


def _archive_path(
    name: str, version: str, source_id: str | None = None,
) -> Path:
    """Cache path for a package version's archive.

    ``<name>/<source_id>/<version>.tar.gz`` when the logical source is
    known, else the original ``<name>/<version>.tar.gz``.

    The ``source_id`` level exists because ``(name, version)`` is NOT an
    identity: a package name is not globally unique, so two remotes
    publishing ``demo@1.0.0`` would otherwise share one cache entry and
    each overwrite the other's pristine tree (brief traps #2/#4).
    ``sources.source_id`` discriminates them.  It is OPTIONAL so every
    pre-existing caller — and every archive written before this layout —
    keeps working: see :func:`archive_candidates`.
    """
    return _package_cache_dir(name, source_id) / f"{version}.tar.gz"


def archive_candidates(
    name: str, version: str, *, source_id: str | None = None,
) -> tuple[Path, ...]:
    """Return every cached archive that might hold ``name@version``.

    In lookup order:

    1. the current layout's archive (``<name>/<source_id>/<version>``),
       when ``source_id`` is given,
    2. the old layout's archive (``<name>/<version>``), so an archive
       written before the ``source_id`` level is still found,
    3. any archive moved aside by :func:`store_archive` because its
       version string was re-published with different bytes, newest
       name-sorted first, in both layouts.

    Order is a hint only: :func:`load_pristine_tree` verifies each
    candidate against the expected root hash and returns the first that
    matches, so a wrong-but-present candidate is skipped rather than
    served.
    """
    dirs: list[Path] = []
    if source_id:
        dirs.append(_package_cache_dir(name, source_id))
    dirs.append(_package_cache_dir(name, None))

    out: list[Path] = []
    for d in dirs:
        primary = d / f"{version}.tar.gz"
        if primary.is_file():
            out.append(primary)
    for d in dirs:
        if not d.is_dir():
            continue
        out.extend(sorted(
            p for p in d.glob(f"{version}{_SUPERSEDED_INFIX}*.tar.gz")
            if p.is_file()
        ))
    # dict.fromkeys de-duplicates while keeping order (source_id and
    # old-layout dirs coincide when source_id is empty).
    return tuple(dict.fromkeys(out))


def _archive_root_hash(archive_path: Path) -> str:
    """Return the root hash of the tree ``archive_path`` expands to."""
    tmp_root = Path(tempfile.mkdtemp(prefix="carpenter-archive-hash-"))
    try:
        with tarfile.open(str(archive_path), mode="r:*") as tar:
            _safe_extract(tar, tmp_root)
        return compute_package_hash(tmp_root)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _preserve_superseded(dest: Path, new_root_hash: str) -> None:
    """Move ``dest`` aside if it holds a DIFFERENT tree to the new one.

    ``version`` is a free-form string with no ordering, so a remote can
    re-publish the same version string with different bytes (brief trap
    #16).  Overwriting the cached archive in that case destroys the
    ``shipped_old`` anchor the three-way reconcile needs for the
    version that is installed RIGHT NOW — the upgrade engine's only way
    to tell an operator's edit from an upstream change.  So the old
    archive is renamed to ``<version>.superseded-<hash12>.tar.gz``
    instead of being overwritten, and stays findable through
    :func:`archive_candidates` (which verifies by hash, so the name is
    only a label).

    Best-effort: a failure here is logged and the caller proceeds, the
    same posture install already takes toward the cache as a whole.  An
    identical archive is left alone — rewriting the same bytes is not a
    loss.
    """
    try:
        existing_hash = _archive_root_hash(dest)
    except Exception:  # noqa: BLE001 — unreadable/corrupt: nothing to keep
        logger.warning(
            "Could not read the cached archive at %s to check whether it "
            "holds a different tree; it will be replaced", dest,
            exc_info=True,
        )
        return
    if existing_hash == new_root_hash:
        return
    aside = dest.with_name(
        f"{dest.name[:-len('.tar.gz')]}"
        f"{_SUPERSEDED_INFIX}{existing_hash[:12]}.tar.gz"
    )
    try:
        os.replace(str(dest), str(aside))
    except OSError:
        logger.warning(
            "Could not preserve the superseded archive %s as %s; the "
            "pristine tree for that version will be lost", dest, aside,
            exc_info=True,
        )
        return
    logger.warning(
        "Archive for %s already held a different tree (%s...); preserved "
        "it as %s before storing %s...",
        dest.name, existing_hash[:12], aside.name, new_root_hash[:12],
    )


def store_archive(
    name: str,
    version: str,
    source_dir: Path,
    *,
    source_id: str | None = None,
) -> Path:
    """Archive a package's pristine tree into the cache.

    Writes ``<cache>/<name>/<source_id>/<version>.tar.gz`` (or the old
    ``<cache>/<name>/<version>.tar.gz`` when no ``source_id`` is known)
    and returns its path.  Called by the install flow to capture the
    pristine snapshot an upgrade reconciles against.

    If an archive is already there holding DIFFERENT bytes, it is moved
    aside rather than overwritten (see :func:`_preserve_superseded`).

    Args:
        name: Package name.
        version: Package version string.
        source_dir: The package directory to snapshot.
        source_id: Stable id of the logical source these bytes came
            from (``sources.source_id``).  Omitted — the default — uses
            the old, source-blind layout.

    Returns:
        Path to the stored archive.
    """
    dest = _archive_path(name, version, source_id)
    source_dir = Path(source_dir)
    if dest.is_file():
        _preserve_superseded(dest, compute_package_hash(source_dir))
    archive_tree(source_dir, dest)
    logger.info(
        "Stored pristine archive for %s@%s at %s", name, version, dest,
    )
    return dest


# ── cache reclamation ───────────────────────────────────────────────


def _reclaim_target(name: str, root: Path) -> Path:
    """Return the cache directory to delete for ``name``, or refuse.

    Fail-closed allowlist in the style of :func:`_safe_extract` and
    :func:`carpenter.packages.reconcile_apply._validate_rel_path` — the
    caller is handing us a ``shutil.rmtree`` target, so nothing is
    accepted that is not provably the package's own cache directory:

    * an empty/whitespace name is refused,
    * an absolute name, or one carrying a path separator, a drive
      letter or a ``.``/``..`` component, is refused — a package name is
      one path component, never a path,
    * the resolved target must be a PROPER immediate subdirectory of the
      resolved cache root: never the root itself, never above it, never
      nested deeper,
    * the target must not itself be a symlink, so a planted link cannot
      redirect the delete out of the cache.  (``shutil.rmtree`` never
      follows a symlinked *member*, so links inside the tree are removed
      as links and their targets are left alone.)

    Raises:
        ArchiveCacheError: on any violation, BEFORE anything is deleted.
    """
    if not name or not name.strip():
        raise ArchiveCacheError(
            f"unsafe package name for cache reclaim (empty): {name!r}",
        )
    if name.startswith("/") or os.path.isabs(name):
        raise ArchiveCacheError(
            f"unsafe package name for cache reclaim (absolute): {name!r}",
        )
    # Reject backslash-absolute / drive-letter style too, since
    # os.path.isabs is platform-dependent.
    if name.startswith("\\") or (len(name) >= 2 and name[1] == ":"):
        raise ArchiveCacheError(
            f"unsafe package name for cache reclaim (absolute): {name!r}",
        )
    if "/" in name or "\\" in name or name in (".", ".."):
        raise ArchiveCacheError(
            f"unsafe package name for cache reclaim (not a single path "
            f"component): {name!r}",
        )

    root_resolved = root.resolve()
    candidate = root / name
    if candidate.is_symlink():
        raise ArchiveCacheError(
            f"refusing to reclaim the cache for {name!r}: "
            f"{candidate} is a symlink",
        )
    target = candidate.resolve()
    if target == root_resolved or not _is_within(root_resolved, target):
        raise ArchiveCacheError(
            f"refusing to reclaim the cache for {name!r}: {target} is "
            f"not a subdirectory of the cache root {root_resolved}",
        )
    if target.parent != root_resolved:
        raise ArchiveCacheError(
            f"refusing to reclaim the cache for {name!r}: {target} is "
            f"not an immediate child of the cache root {root_resolved}",
        )
    return target


def reclaim_package_archives(name: str) -> tuple[Path, ...]:
    """Delete every cached pristine archive belonging to ``name``.

    Removes ``<cache>/<name>/`` whole, so it reclaims every version and
    every ``source_id`` the package ever had, the ``superseded-*`` copies
    :func:`store_archive` moved aside, and the old source-blind
    ``<cache>/<name>/<version>.tar.gz`` layout, leaving no empty
    directories behind.  No other package's cache is touched: the delete
    is scoped to one immediate child of the cache root, vetted by
    :func:`_reclaim_target`.

    Called from :func:`carpenter.packages.installer.uninstall_package`.
    An archive is a *derived* artifact — a re-install re-materialises it
    from the fetched tree — so there is nothing to keep once the install
    record is gone, and keeping it is how the cache grew without bound
    across installs, re-installs and upgrades of packages that are no
    longer installed.

    Idempotent: a missing cache directory (or a missing cache root) is a
    no-op returning ``()``, not an error.

    Args:
        name: Package name.  Must be a single path component.

    Returns:
        The archive paths that were removed, sorted.  Informational: the
        directory itself is gone either way.

    Raises:
        ArchiveCacheError: the name or the resolved path failed the
            safety guard, or the delete failed.  Callers treat this as
            best-effort — see :func:`uninstall_package`.
    """
    root = cache_dir()
    target = _reclaim_target(name, root)
    if not target.is_dir():
        return ()

    # Inventory the archives before the delete, for the caller's report.
    # os.walk does not follow directory symlinks by default, matching
    # what shutil.rmtree will actually traverse.
    removed: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(target):
        for filename in filenames:
            path = Path(dirpath) / filename
            if filename.endswith(".tar.gz") and not path.is_symlink():
                removed.append(path)

    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise ArchiveCacheError(
            f"could not reclaim the archive cache for {name!r} at "
            f"{target}: {exc}",
        ) from exc

    logger.info(
        "Reclaimed %d cached pristine archive(s) for %r from %s",
        len(removed), name, target,
    )
    return tuple(sorted(removed))


# ── safe extraction ─────────────────────────────────────────────────


def _is_within(base: Path, target: Path) -> bool:
    """True if ``target`` is ``base`` or lives under it (no escape)."""
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


def _safe_extract(
    tar: tarfile.TarFile, dest: Path, members=None,
) -> None:
    """Extract ``tar`` into ``dest``, rejecting any unsafe member.

    Rejected: absolute member paths, paths with ``..`` that escape
    ``dest``, symlinks/hardlinks, and any non-regular-file / non-directory
    member (devices, fifos, etc.).  This is a fail-closed allowlist:
    anything not explicitly a plain file or directory inside ``dest`` is
    refused.

    Args:
        tar: The open archive.
        dest: Directory to extract into.
        members: The members to extract, when the caller has already
            walked the index under its own resource caps (the package
            fetcher does, so that an archive declaring millions of
            members is refused before the index is materialised).  The
            default walks the archive itself — lazily, never through
            ``getmembers()``, for the same reason.
    """
    dest = dest.resolve()
    vetted: list[tarfile.TarInfo] = []
    for member in (tar if members is None else members):
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
        vetted.append(member)
    # All members vetted; extract.  (Members were validated above, so the
    # bandit B202 tarfile-extractall concern is addressed by the guard.)
    tar.extractall(dest, members=vetted)  # noqa: S202


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
    # keep temp churn off slow flash storage.
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
    source_id: str | None = None,
) -> dict[str, bytes]:
    """Return a package version's pristine tree as ``path -> bytes``.

    Resolution order:

    1. **Local cache hit** — every candidate
       :func:`archive_candidates` offers (current layout, old layout,
       superseded copies) is expanded and verified against
       ``expected_root_hash``; the first match is returned.
    2. **Fetch** — if there is no candidate at all and a ``fetcher`` is
       supplied, call ``fetcher.fetch(name, version)`` to obtain a
       downloaded archive, place it in the cache, then expand + verify.
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
        source_id: Stable id of the logical source (
            ``sources.source_id``) whose copy of this version is
            wanted.  Omitted, only the old source-blind layout is read.

    Returns:
        The pristine tree as ``{posix_rel_path: bytes}``.

    Raises:
        ArchiveVerificationError: archives exist for this version but
            none expands to ``expected_root_hash``.
        ArchiveCacheError: no archive is available (cache miss + no
            fetcher, or the fetcher produced nothing usable).
    """
    candidates = archive_candidates(name, version, source_id=source_id)
    problems: list[str] = []
    for candidate in candidates:
        try:
            return _verify_archive_tree(candidate, expected_root_hash)
        except ArchiveCacheError as exc:
            # Not fatal per candidate: a superseded copy or an
            # old-layout archive from a different source legitimately
            # fails to match.  Fatal only if NOTHING matches, below.
            problems.append(f"{candidate.name}: {exc}")

    if candidates:
        # Archives for this version exist but none is the tree we were
        # promised.  Never serve unverified bytes, and never silently
        # re-fetch over them: the cache is corrupt, tampered, or this
        # version string was re-published with different bytes.
        raise ArchiveVerificationError(
            f"{len(candidates)} cached archive(s) for {name}@{version} "
            f"but none expands to the expected tree hash "
            f"{expected_root_hash[:12]}...: " + "; ".join(problems),
        )

    if fetcher is None:
        raise ArchiveCacheError(
            f"no cached archive for {name}@{version} at "
            f"{_archive_path(name, version, source_id)} and no "
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

    cached = _archive_path(name, version, source_id)
    cached.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(str(fetched), str(cached))
    except OSError:
        logger.warning(
            "verified archive for %s@%s but failed to cache it at %s",
            name, version, cached,
        )
    return tree
