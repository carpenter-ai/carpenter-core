"""Apply step of the three-way package-upgrade reconciliation (reconcile SD).

:mod:`carpenter.packages.reconcile` classifies a package upgrade into a
:class:`~carpenter.packages.reconcile.ReconcilePlan` of per-path
``FileDelta`` records.  A higher layer (a future CLI / chat flow) turns
that plan plus the operator's per-file decisions into a single **resolved
tree** — a ``path -> bytes`` mapping describing the exact bytes the user
wants installed.  This module is the layer that *materializes* that
resolved tree as the installed package.

It deliberately does NOT:

* gather resolutions / drive any UX,
* fetch archives or talk to the network,
* wire itself into any live command, route, or CLI.

It reuses the existing install machinery rather than reimplementing it:

* :func:`carpenter.packages.installer._atomic_copy_into_place` performs the
  transactional staging-dir → ``os.replace`` swap with rollback, so a
  failure mid-apply leaves the prior install untouched (all-or-nothing).
* :func:`carpenter.packages.installer.compute_package_hash` recomputes the
  deterministic root hash of the materialized tree.
* :func:`carpenter.packages.installer._record_install` writes the
  ``installed_packages`` row (same ``INSERT OR REPLACE`` statement install
  uses) — we do NOT hand-write SQL.
* :func:`carpenter.packages.installer._install_kb_articles` refreshes the
  package's KB folder so reconciled KB edits take effect.

Path-safety: resolved-tree keys are vetted with the same fail-closed guard
used by :func:`carpenter.packages.archive_cache._safe_extract` — absolute
paths and ``..`` components that escape the staging root are rejected
*before any byte is written*.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .installer import (
    InstallError,
    _atomic_copy_into_place,
    _install_kb_articles,
    _record_install,
    compute_package_hash,
    get_install_record,
)
from .manifest import load_manifest

logger = logging.getLogger(__name__)

__all__ = [
    "ReconcileApplyError",
    "ApplyResult",
    "apply_reconciled_install",
]


class ReconcileApplyError(InstallError):
    """Raised when applying a reconciled tree fails.

    Subclasses :class:`~carpenter.packages.installer.InstallError` so
    callers that already handle install failures catch this too.
    """


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of a successful :func:`apply_reconciled_install`."""

    name: str
    version: str
    hash: str
    dest_path: Path
    files_written: int
    kb_articles_installed: int


# ── path safety ──────────────────────────────────────────────────────


def _validate_rel_path(rel: str, *, staging_root: Path) -> Path:
    """Validate a resolved-tree key and return its safe absolute target.

    Fail-closed allowlist mirroring
    :func:`carpenter.packages.archive_cache._safe_extract`:

    * empty paths are rejected,
    * absolute paths (``/foo`` or platform-absolute) are rejected,
    * any path whose resolved target escapes ``staging_root`` (via ``..``
      or otherwise) is rejected.

    Raises :class:`ReconcileApplyError` on any violation, BEFORE the
    caller writes anything.
    """
    if not rel or not rel.strip():
        raise ReconcileApplyError(
            f"unsafe resolved-tree path (empty): {rel!r}",
        )
    if rel.startswith("/") or os.path.isabs(rel):
        raise ReconcileApplyError(
            f"unsafe resolved-tree path (absolute): {rel!r}",
        )
    # Also reject backslash-absolute / drive-letter style just in case a
    # Windows-style key sneaks in (os.path.isabs is platform-dependent).
    if rel.startswith("\\") or (len(rel) >= 2 and rel[1] == ":"):
        raise ReconcileApplyError(
            f"unsafe resolved-tree path (absolute): {rel!r}",
        )
    target = (staging_root / rel).resolve()
    staging_resolved = staging_root.resolve()
    try:
        target.relative_to(staging_resolved)
    except ValueError:
        raise ReconcileApplyError(
            f"unsafe resolved-tree path (escapes staging root): {rel!r}",
        ) from None
    return target


def _materialize_tree(
    resolved_tree: Mapping[str, bytes], staging_root: Path,
) -> int:
    """Write every ``(path, bytes)`` entry under ``staging_root``.

    All keys are validated for path-safety *first* (so a single bad key
    aborts before any write), then written.  Parent directories are
    created as needed.  Returns the number of files written.
    """
    # Validate ALL paths up front so an unsafe key never results in a
    # partial materialization.
    targets: list[tuple[Path, bytes]] = []
    for rel, content in resolved_tree.items():
        if not isinstance(content, (bytes, bytearray)):
            raise ReconcileApplyError(
                f"resolved-tree value for {rel!r} is not bytes "
                f"(got {type(content).__name__})",
            )
        target = _validate_rel_path(rel, staging_root=staging_root)
        targets.append((target, bytes(content)))

    written = 0
    for target, content in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        written += 1
    return written


# ── apply ────────────────────────────────────────────────────────────


def apply_reconciled_install(
    name: str,
    version: str,
    resolved_tree: Mapping[str, bytes],
    *,
    conn: sqlite3.Connection,
    dest_path: Path | str | None = None,
) -> ApplyResult:
    """Atomically install a reconciled package tree as ``name``.

    Steps:

    1. Materialize ``resolved_tree`` (``path -> bytes``) into a fresh temp
       staging dir (honours ``TMPDIR``, e.g. ``/dev/shm``).  Every key is
       path-safety vetted before any write (no absolute paths, no ``..``
       escaping the staging root).
    2. Atomically install the materialized tree into ``dest_path`` via
       :func:`carpenter.packages.installer._atomic_copy_into_place`, so a
       failure leaves the prior install untouched (transactional).
    3. Recompute the tree's root hash with
       :func:`carpenter.packages.installer.compute_package_hash` and update
       the ``installed_packages`` row (new ``version`` + ``hash``) via the
       same ``_record_install`` path install uses.
    4. Refresh the package's KB articles (if the manifest declares any) via
       :func:`carpenter.packages.installer._install_kb_articles`.

    Args:
        name: Package name.  Must match the resolved tree's manifest name.
        version: Version string to record for the reconciled install.
        resolved_tree: ``{posix_rel_path: bytes}`` — the exact bytes to
            install.  Must contain a ``manifest.yaml`` entry.
        conn: SQLite connection for updating the install record.
        dest_path: Where to materialize the install.  If ``None``, resolved
            from the existing ``installed_packages`` record's
            ``install_path`` (the package must already be installed in that
            case).

    Returns:
        :class:`ApplyResult`.

    Raises:
        ReconcileApplyError: unsafe path, missing/mismatched manifest,
            dest resolution failure, or atomic-swap failure.  On any
            failure the prior install + DB row are left intact.
    """
    if not resolved_tree:
        raise ReconcileApplyError(
            f"resolved_tree for {name!r} is empty; refusing to install",
        )

    # Resolve the destination BEFORE touching anything.
    record = get_install_record(conn, name)
    if dest_path is None:
        if record is None:
            raise ReconcileApplyError(
                f"no install record for {name!r} and no dest_path given; "
                f"cannot resolve where to install",
            )
        dest_path = Path(record["install_path"])
    dest_path = Path(dest_path).resolve()

    # Defense in depth (mirrors install_package): the dest basename must
    # match the package name so we never install pkg X into Y's slot.
    if dest_path.name != name:
        raise ReconcileApplyError(
            f"dest_path basename {dest_path.name!r} does not match "
            f"package name {name!r}",
        )

    # mkdtemp honours $TMPDIR (the repo sets TMPDIR=/dev/shm for
    # test/workspace runs) so we keep staging churn off the SD card.
    staging_root = Path(tempfile.mkdtemp(prefix="carpenter-reconcile-apply-"))
    try:
        files_written = _materialize_tree(resolved_tree, staging_root)

        # The materialized tree must be a valid package: it needs a
        # manifest, and that manifest's name must match (same invariant
        # install_package enforces).
        manifest_file = staging_root / "manifest.yaml"
        if not manifest_file.is_file():
            raise ReconcileApplyError(
                f"resolved_tree for {name!r} has no manifest.yaml entry",
            )
        manifest = load_manifest(manifest_file)
        if manifest.name != name:
            raise ReconcileApplyError(
                f"resolved_tree manifest name {manifest.name!r} does not "
                f"match requested package name {name!r}",
            )

        # Hash the staged tree exactly as the installer does.  This is the
        # same bytes _atomic_copy_into_place will materialize (it skips the
        # same ignored cruft, none of which we wrote).
        pkg_hash = compute_package_hash(staging_root)

        # Transactional swap: on failure the prior install is restored by
        # _atomic_copy_into_place's rollback and we never reach the DB
        # update below, so the row is unchanged.
        _atomic_copy_into_place(staging_root, dest_path)
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    else:
        # The swap moved staging into place; the temp root is now gone, but
        # remove it defensively in case the platform left an empty husk.
        shutil.rmtree(staging_root, ignore_errors=True)

    # Re-load the manifest from the installed copy (matches install's
    # invariant: recorded manifest == on-disk manifest) and update the
    # install row with the new version + hash via the same path install
    # uses.  We override version with the caller-supplied value so the
    # recorded version reflects the reconciled upgrade target.
    installed_manifest = load_manifest(dest_path / "manifest.yaml")
    if version:
        installed_manifest = dataclasses.replace(
            installed_manifest, version=version,
        )

    source_path = (
        Path(record["source_path"]) if record is not None else dest_path
    )
    installed_at = datetime.now(timezone.utc).isoformat()
    _record_install(
        conn,
        manifest=installed_manifest,
        source_path=source_path,
        install_path=dest_path,
        pkg_hash=pkg_hash,
        installed_at=installed_at,
    )

    # Refresh KB articles for the reconciled tree the same way install
    # does (best-effort; folder-per-package atomic swap inside).
    kb_n = _install_kb_articles(installed_manifest, dest_path)

    logger.info(
        "Applied reconciled install of %r v%s (hash %s, %d file(s))",
        name, installed_manifest.version, pkg_hash[:12], files_written,
    )
    return ApplyResult(
        name=name,
        version=installed_manifest.version,
        hash=pkg_hash,
        dest_path=dest_path,
        files_written=files_written,
        kb_articles_installed=kb_n,
    )
