"""Capability-package install / uninstall / verify machinery (D24 stage 3a).

The Phase A package framework discovered packages directly from a
source repo (``~/repos/carpenter-packages/packages/<name>/``) at every
server start.  D24 SD3 changes that to **copy-on-install + hash-pinning**:

* ``install_package(source, dest)`` deterministically hashes a source
  package directory, atomically materializes its contents to
  ``dest``, and records the install (name, version, source, hash) in
  the ``installed_packages`` SQL table.
* ``verify_install(name)`` recomputes the hash of the installed copy
  and compares it to the recorded value.  Called at server startup;
  on mismatch, the package is logged and refused-to-load (the server
  continues without it; SD6).
* ``uninstall_package(name)`` removes the installed copy and the DB
  rows.  Refuses if any non-terminal arc was created from a template
  the package shipped (SD9).  Allowlists are NOT touched (SD5 — the
  one-way ratchet).

This module is platform code, but it doesn't import anything from the
running platform (DB, registry, etc.) at top level; it takes its DB
connection as a parameter so it remains easy to test in isolation.

The companion stage 3b PR will hook ``verify_install`` into
:class:`PackageRegistry.discover_and_register` and migrate the
reference ``hello`` package onto the install model.  Until then the
registry retains a back-compat shim that scans the source repo for
packages that don't yet have an install record (see
:mod:`carpenter.packages.registry`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .manifest import (
    EnvCredentialRef,
    PackageManifest,
    ManifestError,
    PlatformCapabilityRef,
    load_manifest,
)
from .security import PackageSecurityError, validate_manifest_security

logger = logging.getLogger(__name__)


# Files / directories ignored when computing the source-tree hash.
# Build artefacts and editor cruft must not affect determinism — two
# operators installing the same logical package should compute the
# same hash regardless of whether their checkout has a stray
# ``__pycache__`` next to a chat-tool module.
_IGNORED_NAMES = frozenset({
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".DS_Store",
    ".git",
    ".gitignore",
})
_IGNORED_SUFFIXES = (".pyc", ".pyo", ".swp", "~")


class InstallError(Exception):
    """Raised when an install / uninstall / verify operation fails."""


@dataclass(frozen=True)
class InstallResult:
    """Outcome of a successful install."""

    name: str
    version: str
    hash: str
    source_path: Path
    dest_path: Path
    installed_at: str
    was_update: bool
    # B-full additions: tell the caller what the install changed so the
    # confirmation dialog / log line can surface it.
    allowlist_added: tuple[tuple[str, str], ...] = ()
    allowlist_removed: tuple[tuple[str, str], ...] = ()
    kb_articles_installed: int = 0
    trigger_subscriptions_registered: int = 0
    # Phase 3a PR-B: count of in-process triggers instantiated from the
    # manifest's ``triggers:`` block.  Zero for packages that don't
    # contribute triggers.
    triggers_installed: int = 0
    # ``kind: env`` credential requests created for env vars the package
    # declared but that were not already set.  Each entry is a dict with
    # ``key`` (full ``{prefix}_{suffix}`` env var name), ``provider``,
    # ``request_id``, and ``url`` (the one-time credential form link).
    # Surfaced so the operator / chat agent knows exactly what to supply
    # out-of-band; install does NOT hard-fail on missing env creds
    # (mirrors the OAuth posture — creds are provided after install).
    env_credential_requests: tuple[dict, ...] = ()
    # Package-capability framework: the TRUSTED platform-side dispatch
    # verbs the operator confirmed (granted PLATFORM-LEVEL TRUST) at
    # install time.  Each entry is a dict with ``verb``, ``kind``,
    # ``module``, ``handler``, and a ``grant`` sub-dict (protocol, host
    # env-var suffix, port, credential_ref).  Empty when the package
    # declares no platform capabilities.  Only granted capabilities are
    # later registered at load time.
    platform_capabilities_granted: tuple[dict, ...] = ()
    # Operator-gated write chat tools (I10 relaxation): whether the
    # operator opted this package's write-capable chat-boundary tools in
    # at install time.  Default False (gated off; chat agent read-only).
    write_chat_tools_allowed: bool = False


@dataclass(frozen=True)
class UninstallResult:
    """Outcome of a successful uninstall."""

    name: str
    removed_path: Path


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of a verify_install call.

    ``ok=True`` ⇒ the on-disk hash matches the recorded one and the
    package is safe to load.  ``ok=False`` ⇒ the loader must skip it
    and log loudly with both hashes.
    """

    name: str
    ok: bool
    expected_hash: str | None
    actual_hash: str | None
    install_path: Path
    message: str


# ── Hashing ──────────────────────────────────────────────────────────


def _iter_files(root: Path) -> list[Path]:
    """Return every regular file under ``root``, sorted for determinism.

    Symlinks are NOT followed — a symlink whose target lives outside
    the package would otherwise let the source decide what gets
    hashed.  Symlinks within the package are hashed as their text
    content (the symlink target string), recorded distinctly from a
    same-named regular file.  This is the safest interpretation: an
    attacker who replaces a regular file with a symlink to ``/etc/passwd``
    still produces a different hash from the legitimate version.
    """
    out: list[Path] = []
    root_resolved = root.resolve()
    for dirpath, dirnames, filenames in os.walk(
        root_resolved, followlinks=False,
    ):
        # In-place filter so os.walk skips the ignored dirs.
        dirnames[:] = sorted(d for d in dirnames if d not in _IGNORED_NAMES)
        for fn in sorted(filenames):
            if fn in _IGNORED_NAMES:
                continue
            if any(fn.endswith(suf) for suf in _IGNORED_SUFFIXES):
                continue
            out.append(Path(dirpath) / fn)
    return out


def compute_package_hash(package_dir: Path) -> str:
    """Compute a deterministic SHA-256 hash over the package directory.

    The hash is stable across operators / hosts / Pythons: it depends
    only on relative paths (POSIX-style) and file bytes, sorted for
    determinism.  Symlinks are hashed by their target text — see
    :func:`_iter_files`.

    Args:
        package_dir: Path to the package directory (parent of
            ``manifest.yaml``).

    Returns:
        Hex-encoded SHA-256 of ``(rel_posix_path, sha256(bytes))``
        tuples concatenated in sorted order, with a length-prefixed
        framing so paths can't collide across boundaries.
    """
    package_dir = package_dir.resolve()
    if not package_dir.is_dir():
        raise InstallError(
            f"compute_package_hash: {package_dir} is not a directory",
        )

    accumulator = hashlib.sha256()
    for path in _iter_files(package_dir):
        rel = path.relative_to(package_dir).as_posix()
        # Hash file bytes (or, for symlinks, the target text).
        file_hash = hashlib.sha256()
        if path.is_symlink():
            target = os.readlink(path)
            file_hash.update(b"link:")
            file_hash.update(target.encode("utf-8", errors="surrogateescape"))
        else:
            with open(path, "rb") as fp:
                while True:
                    chunk = fp.read(65536)
                    if not chunk:
                        break
                    file_hash.update(chunk)
        # Length-prefix every component so concatenation is unambiguous.
        rel_bytes = rel.encode("utf-8")
        digest = file_hash.digest()
        accumulator.update(len(rel_bytes).to_bytes(4, "big"))
        accumulator.update(rel_bytes)
        accumulator.update(len(digest).to_bytes(4, "big"))
        accumulator.update(digest)
    return accumulator.hexdigest()


# ── DB schema helpers ────────────────────────────────────────────────


_INSTALLED_PACKAGES_DDL = """
CREATE TABLE IF NOT EXISTS installed_packages (
    name TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    hash TEXT NOT NULL,
    source_path TEXT NOT NULL,
    install_path TEXT NOT NULL,
    installed_at TEXT NOT NULL,
    -- B-full (D24): JSON array of {type, value} the manifest declared
    -- at install time.  Used to compute add/remove deltas on update
    -- (the diff is shown in the confirmation dialog) and to display
    -- the package's contributed entries in list_packages.  Allowlist
    -- entries themselves live in ``security_policies``; this column
    -- is provenance bookkeeping for the diff, NOT enforcement state
    -- (SD5: flat-global, no source_package column on the policy table).
    allowlist_proposals_json TEXT,
    -- Package-capability framework: JSON array of the TRUSTED platform-
    -- side dispatch verbs the operator confirmed (granted PLATFORM-LEVEL
    -- TRUST) at install time.  This is ENFORCEMENT state: the load-time
    -- registrar (``loaders.load_platform_capabilities``) registers ONLY
    -- the verbs recorded here, so a declared-but-not-confirmed capability
    -- is never dispatchable.  Each element is {verb, kind, module,
    -- handler, grant{protocol, host_from, port, credential_ref}}.
    platform_capabilities_json TEXT,
    -- Operator-gated write chat tools (security invariant I10 relaxation):
    -- the chat agent is read-only BY DEFAULT, so a package's write-capable
    -- chat-boundary tools (those declaring arc_create / external_effect /
    -- database_write / filesystem_write) are NOT registered unless the
    -- operator explicitly opted in at install time.  This is ENFORCEMENT
    -- state: the registry reads this flag per package and only registers
    -- the write-capable chat tools when it is 1.  Default 0 (gated off).
    -- This is SEPARATE from platform_capabilities_json (egress trust).
    write_chat_tools_allowed INTEGER NOT NULL DEFAULT 0
);
"""

_INSTALLED_PACKAGES_TEMPLATES_DDL = """
CREATE TABLE IF NOT EXISTS installed_packages_templates (
    package_name TEXT NOT NULL,
    template_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY (package_name, template_name),
    FOREIGN KEY (package_name) REFERENCES installed_packages(name)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_installed_packages_templates_template
    ON installed_packages_templates(template_name);
"""


def ensure_installer_tables(conn: sqlite3.Connection) -> None:
    """Create the installer tables if missing.  Called from db_migrations."""
    conn.executescript(
        _INSTALLED_PACKAGES_DDL + _INSTALLED_PACKAGES_TEMPLATES_DDL,
    )
    # B-full migration: existing DBs created before B-full lack the
    # ``allowlist_proposals_json`` column.  ALTER TABLE ADD COLUMN is
    # idempotent only via the OperationalError-on-duplicate path, so we
    # check first.
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(installed_packages)"
    ).fetchall()}
    if "allowlist_proposals_json" not in cols:
        conn.execute(
            "ALTER TABLE installed_packages "
            "ADD COLUMN allowlist_proposals_json TEXT",
        )
    # Package-capability framework migration: older DBs lack the
    # platform_capabilities_json column.
    if "platform_capabilities_json" not in cols:
        conn.execute(
            "ALTER TABLE installed_packages "
            "ADD COLUMN platform_capabilities_json TEXT",
        )
    # Operator-gated write chat tools migration: older DBs lack the
    # write_chat_tools_allowed column.  Defaults to 0 (gated off) so
    # existing installs stay read-only until re-installed with opt-in.
    if "write_chat_tools_allowed" not in cols:
        conn.execute(
            "ALTER TABLE installed_packages "
            "ADD COLUMN write_chat_tools_allowed INTEGER NOT NULL DEFAULT 0",
        )
    conn.commit()


def _capability_to_dict(cap: PlatformCapabilityRef) -> dict:
    """Serialise a granted capability ref for the install record / result."""
    return {
        "verb": cap.verb,
        "kind": cap.kind,
        "module": cap.module,
        "handler": cap.handler,
        "grant": {
            "protocol": cap.grant.protocol,
            "host_from": cap.grant.host_from,
            "port": cap.grant.port,
            "credential_ref": cap.grant.credential_ref,
        },
    }


def _record_install(
    conn: sqlite3.Connection,
    *,
    manifest: PackageManifest,
    source_path: Path,
    install_path: Path,
    pkg_hash: str,
    installed_at: str,
    granted_capabilities: tuple[PlatformCapabilityRef, ...] = (),
    write_chat_tools_allowed: bool = False,
) -> None:
    """Write the install row + templates rows.

    The caller owns the transaction.  When invoked inside
    :func:`carpenter.db.db_transaction`, that wrapper commits on
    success / rolls back on exception, so we must not issue our own
    ``BEGIN``/``COMMIT`` here (nested transactions would either
    silently no-op the outer commit or trigger sqlite errors).  In the
    test path the caller passes a bare connection and the writes flush
    when the test cleans up the connection (or via an explicit commit).
    """
    import json as _json
    proposals_payload = _json.dumps([
        {"type": p.policy_type, "value": p.value}
        for p in manifest.allowlist_proposals
    ])
    capabilities_payload = _json.dumps([
        _capability_to_dict(c) for c in granted_capabilities
    ])
    conn.execute(
        "INSERT OR REPLACE INTO installed_packages "
        "(name, version, hash, source_path, install_path, installed_at, "
        "allowlist_proposals_json, platform_capabilities_json, "
        "write_chat_tools_allowed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            manifest.name,
            manifest.version,
            pkg_hash,
            str(source_path),
            str(install_path),
            installed_at,
            proposals_payload,
            capabilities_payload,
            1 if write_chat_tools_allowed else 0,
        ),
    )
    # Refresh the templates join table for this package.
    conn.execute(
        "DELETE FROM installed_packages_templates WHERE package_name = ?",
        (manifest.name,),
    )
    for tref in manifest.arc_templates:
        conn.execute(
            "INSERT INTO installed_packages_templates "
            "(package_name, template_name, kind) "
            "VALUES (?, ?, ?)",
            (manifest.name, tref.name, "arc_template"),
        )


def _load_prior_proposals(record: dict | None) -> set[tuple[str, str]]:
    """Parse a record's ``allowlist_proposals_json`` into a set of pairs.

    Returns an empty set if the column is NULL, missing, or malformed.
    Used by :func:`compute_proposal_diff` to compare against the new
    manifest's proposals on update; one-way ratchet semantics live in
    the caller (we don't apply removals).
    """
    if record is None:
        return set()
    raw = record.get("allowlist_proposals_json")
    if not raw:
        return set()
    import json as _json
    try:
        items = _json.loads(raw)
    except (ValueError, TypeError):
        return set()
    if not isinstance(items, list):
        return set()
    out: set[tuple[str, str]] = set()
    for it in items:
        if (
            isinstance(it, dict)
            and isinstance(it.get("type"), str)
            and isinstance(it.get("value"), str)
        ):
            out.add((it["type"], it["value"]))
    return out


@dataclass(frozen=True)
class ProposalDiff:
    """Result of diffing a manifest's proposals against a prior install.

    Attributes:
        added: New ``(type, value)`` pairs that will be merged into
            ``security_policies`` if the operator confirms.
        removed: Pairs that the prior manifest declared but the new
            manifest does not.  Per SD5 these are reported in the diff
            for operator transparency but are NOT removed from
            ``security_policies`` — allowlists are a one-way ratchet.
    """

    added: tuple[tuple[str, str], ...]
    removed: tuple[tuple[str, str], ...]


def compute_proposal_diff(
    manifest: PackageManifest, prior_record: dict | None,
) -> ProposalDiff:
    """Compute the (added, removed) diff between manifest and prior install.

    For a fresh install the prior set is empty so every proposal is
    "added".  For an update, ``added`` is what we will merge into
    ``security_policies`` and ``removed`` is what the new manifest no
    longer declares (informational only — see SD5).
    """
    new_set = {(p.policy_type, p.value) for p in manifest.allowlist_proposals}
    prior_set = _load_prior_proposals(prior_record)
    added = tuple(sorted(new_set - prior_set))
    removed = tuple(sorted(prior_set - new_set))
    return ProposalDiff(added=added, removed=removed)


def _delete_install_record(conn: sqlite3.Connection, name: str) -> None:
    """Delete install + templates rows.  Caller owns the transaction."""
    conn.execute(
        "DELETE FROM installed_packages_templates WHERE package_name = ?",
        (name,),
    )
    conn.execute(
        "DELETE FROM installed_packages WHERE name = ?", (name,),
    )


def get_install_record(
    conn: sqlite3.Connection, name: str,
) -> dict | None:
    row = conn.execute(
        "SELECT name, version, hash, source_path, install_path, "
        "installed_at, allowlist_proposals_json, platform_capabilities_json, "
        "write_chat_tools_allowed "
        "FROM installed_packages WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return {
        "name": row[0],
        "version": row[1],
        "hash": row[2],
        "source_path": row[3],
        "install_path": row[4],
        "installed_at": row[5],
        "allowlist_proposals_json": row[6],
        "platform_capabilities_json": row[7],
        "write_chat_tools_allowed": bool(row[8]),
    }


def list_install_records(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT name, version, hash, source_path, install_path, "
        "installed_at, allowlist_proposals_json, platform_capabilities_json, "
        "write_chat_tools_allowed "
        "FROM installed_packages ORDER BY name"
    ).fetchall()
    return [
        {
            "name": r[0], "version": r[1], "hash": r[2],
            "source_path": r[3], "install_path": r[4],
            "installed_at": r[5],
            "allowlist_proposals_json": r[6],
            "platform_capabilities_json": r[7],
            "write_chat_tools_allowed": bool(r[8]),
        }
        for r in rows
    ]


def list_granted_capabilities(
    conn: sqlite3.Connection, name: str,
) -> list[dict]:
    """Return the granted platform-capability records for an installed package.

    Reads the ``platform_capabilities_json`` column written at install
    time.  Each element is ``{verb, kind, module, handler, grant{...}}``.
    Returns ``[]`` if the package is not installed, granted no
    capabilities, or the column is NULL / malformed.

    This is the authoritative source of which verbs the loader may
    register — it reflects the operator's confirmation, not the manifest
    declaration.
    """
    record = get_install_record(conn, name)
    if record is None:
        return []
    raw = record.get("platform_capabilities_json")
    if not raw:
        return []
    import json as _json
    try:
        items = _json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict) and it.get("verb")]


def granted_verbs_for_package(
    conn: sqlite3.Connection, name: str,
) -> frozenset[str]:
    """Return the set of granted capability verbs for an installed package."""
    return frozenset(
        c["verb"] for c in list_granted_capabilities(conn, name)
        if isinstance(c.get("verb"), str)
    )


def write_chat_tools_allowed_for_package(
    conn: sqlite3.Connection, name: str,
) -> bool:
    """Return whether the operator opted this package's WRITE chat tools in.

    Authoritative source the registry consults at load time: a package's
    write-capable chat-boundary tools are only registered when this is
    ``True``.  Returns ``False`` for an uninstalled package (fail-closed —
    the chat agent is read-only by default).
    """
    record = get_install_record(conn, name)
    if record is None:
        return False
    return bool(record.get("write_chat_tools_allowed"))


def classify_package_chat_tools(
    manifest: PackageManifest,
) -> tuple[list[dict], list[dict]]:
    """Inspect a manifest's chat-tool modules and split tools by capability.

    Imports each declared chat-tool module (package-aware, the same path
    the registry uses) and collects ``@chat_tool``-decorated callables,
    partitioning them into read-only and write/effectful groups so the
    install preview can show the operator exactly what an opt-in would
    enable.  A tool is "write/effectful" when it declares any capability
    in :data:`carpenter.chat_tool_registry.WRITE_CAPABILITIES`.

    Returns ``(read_only, write_capable)`` where each element is a dict
    ``{"name": str, "capabilities": list[str], "write_capabilities":
    list[str], "requires_user_confirm": bool}``.  Modules that fail to
    import are skipped (logged at debug); the preview is best-effort and
    never blocks the install.  Platform-boundary tools are omitted (they
    are refused at registration regardless of opt-in).
    """
    from ..chat_tool_registry import WRITE_CAPABILITIES
    from .loaders import _import_package_module

    read_only: list[dict] = []
    write_capable: list[dict] = []

    for rel in manifest.chat_tools:
        rel_path = Path(rel)
        dotted = ".".join(rel_path.with_suffix("").parts)
        try:
            module = _import_package_module(
                manifest.name, dotted, manifest.source_path,
            )
        except Exception as exc:  # noqa: BLE001 — preview is best-effort
            logger.debug(
                "classify_package_chat_tools: could not import %r for "
                "package %r: %s", rel, manifest.name, exc,
            )
            continue
        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if not callable(obj):
                continue
            meta = getattr(obj, "_chat_tool_meta", None)
            if meta is None:
                continue
            if meta.get("trust_boundary") == "platform":
                # Refused at registration regardless of opt-in; not a
                # chat-boundary tool to preview.
                continue
            caps = list(meta.get("capabilities", []))
            write_caps = [c for c in caps if c in WRITE_CAPABILITIES]
            entry = {
                "name": meta["name"],
                "capabilities": caps,
                "write_capabilities": write_caps,
                "requires_user_confirm": bool(
                    meta.get("requires_user_confirm", False),
                ),
            }
            if write_caps:
                write_capable.append(entry)
            else:
                read_only.append(entry)

    read_only.sort(key=lambda e: e["name"])
    write_capable.sort(key=lambda e: e["name"])
    return read_only, write_capable


# ── Atomic copy ─────────────────────────────────────────────────────


def _atomic_copy_into_place(
    source_path: Path, dest_path: Path,
) -> bool:
    """Atomically materialize ``source_path`` at ``dest_path``.

    Returns True if the operation replaced an existing install
    (update) or False if it was a fresh install.

    Strategy:

    1. Copy the source tree into a sibling staging dir
       ``<dest_path>.staging-<pid>``.  This isolates concurrent
       installs and lets us ``os.replace`` cleanly.
    2. ``os.fsync`` the staging dir so the bytes are durable.
    3. If an existing install dir is present, rename it to a sibling
       ``.old-<pid>`` dir so that a crash mid-swap leaves both old
       and new visible (the .old can then be cleaned up by the next
       successful install).  Otherwise just rename staging into place.
    4. Remove the old dir.  A crash here leaves an orphan ``.old-*``
       dir that startup verification will GC.

    On any failure the staging dir is removed and the original
    ``dest_path`` is left untouched.
    """
    source_path = source_path.resolve()
    dest_path = dest_path.resolve()
    if not source_path.is_dir():
        raise InstallError(f"source path {source_path} is not a directory")

    parent = dest_path.parent
    parent.mkdir(parents=True, exist_ok=True)

    pid = os.getpid()
    staging = parent / f"{dest_path.name}.staging-{pid}"
    rotated_old = parent / f"{dest_path.name}.old-{pid}"

    # Pre-clean any prior crash leftovers belonging to this PID.
    for stray in (staging, rotated_old):
        if stray.exists():
            shutil.rmtree(stray)

    try:
        # ignore=lambda copyfunction skips ignored cruft so the staged
        # copy is byte-for-byte the same as what we hashed.
        def _ignore(_dir: str, names: list[str]) -> set[str]:
            ignored: set[str] = set()
            for n in names:
                if n in _IGNORED_NAMES:
                    ignored.add(n)
                    continue
                if any(n.endswith(suf) for suf in _IGNORED_SUFFIXES):
                    ignored.add(n)
            return ignored

        shutil.copytree(
            source_path, staging, symlinks=True, ignore=_ignore,
        )

        # Fsync the staging directory so the rename is durable.
        try:
            fd = os.open(str(staging), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # fsync on a directory is a best-effort hint on some
            # platforms; failure should not abort the install.
            logger.debug("fsync on staging dir %s skipped", staging)

        was_update = dest_path.exists()
        if was_update:
            os.replace(str(dest_path), str(rotated_old))
        os.replace(str(staging), str(dest_path))
        if was_update and rotated_old.exists():
            shutil.rmtree(rotated_old)
        return was_update
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if rotated_old.exists() and not dest_path.exists():
            # Roll back: the old install was rotated out but the
            # rename of staging into place failed.  Try to put it back.
            try:
                os.replace(str(rotated_old), str(dest_path))
            except OSError:
                logger.exception(
                    "Failed to roll back install of %s; manual recovery "
                    "needed (rotated_old=%s)", dest_path.name, rotated_old,
                )
        raise


# ── B-full helpers (allowlists / KB / trigger subscriptions) ────────


def _merge_allowlist_proposals(
    conn: sqlite3.Connection,
    added: tuple[tuple[str, str], ...],
) -> None:
    """Insert each (type, value) into ``security_policies`` and the
    in-memory singleton.

    Per SD5 there is no provenance column — once merged, an entry is
    platform policy.  We use ``INSERT OR IGNORE`` so re-installs (which
    re-add the same pair) are idempotent.  The in-memory singleton is
    refreshed via ``get_policies().add`` so a chat tool can see the
    new entry without waiting for ``reload_policies``.

    Soft-imports the policy module so unit tests that don't need the
    full security stack can monkeypatch this function.
    """
    if not added:
        return
    try:
        from ..security.policies import get_policies
    except ImportError:
        logger.warning(
            "_merge_allowlist_proposals: security.policies unavailable; "
            "skipping in-memory merge (DB rows still inserted)",
        )
        get_policies = None  # type: ignore[assignment]

    for ptype, value in added:
        conn.execute(
            "INSERT OR IGNORE INTO security_policies "
            "(policy_type, value) VALUES (?, ?)",
            (ptype, value),
        )
        if get_policies is not None:
            try:
                get_policies().add(ptype, value)
            except (ValueError, KeyError) as exc:
                # Manifest validation already rejects unknown types;
                # this would be a real bug — surface a warning but
                # don't abort the install.
                logger.warning(
                    "Failed to add (%s, %s) to in-memory policies: %s",
                    ptype, value, exc,
                )


def _kb_root_dir() -> Path | None:
    """Resolve the platform's KB filesystem root, or None if unavailable.

    Mirrors :class:`carpenter.kb.store.KBStore`'s init logic: prefer
    ``config.CONFIG['kb']['dir']``, otherwise ``<base_dir>/config/kb``.
    Returns ``None`` if the config import fails (tests that don't need
    KB don't have to set up config).
    """
    try:
        from .. import config
    except ImportError:
        return None
    kb_cfg = config.CONFIG.get("kb", {})
    kb_dir = kb_cfg.get("dir", "")
    if not kb_dir:
        base_dir = config.CONFIG.get("base_dir", "")
        if not base_dir:
            return None
        kb_dir = os.path.join(base_dir, "config", "kb")
    return Path(kb_dir)


def _package_kb_dir(kb_root: Path, package_name: str) -> Path:
    """Folder where a package's KB articles live (one folder per pkg)."""
    return kb_root / "packages" / package_name


def _install_kb_articles(
    manifest: PackageManifest, install_path: Path,
) -> int:
    """Copy declared KB articles into ``<kb_root>/packages/<pkg>/``.

    Folder-per-package isolation: collisions between packages are
    impossible by construction (every package gets its own subfolder
    under ``packages/``).  The package's slug determines the path
    INSIDE the folder, e.g. slug ``email/overview`` → file at
    ``<kb_root>/packages/<pkg>/email/overview.md``.

    Atomic-swap: the package's folder is rebuilt in a sibling staging
    directory, then renamed into place.  Re-installs replace the
    package's folder cleanly; KB articles a previous install shipped
    that the new manifest no longer declares simply disappear with the
    folder swap.

    Returns the number of articles copied.  Errors during copy are
    logged and the partial staging dir is removed; the rest of the
    install proceeds (KB is best-effort, not a security gate).
    """
    if not manifest.kb_articles:
        return 0
    kb_root = _kb_root_dir()
    if kb_root is None:
        logger.info(
            "KB root not configured; skipping kb_articles install for %r",
            manifest.name,
        )
        return 0

    pkg_kb_dir = _package_kb_dir(kb_root, manifest.name)
    parent = pkg_kb_dir.parent
    parent.mkdir(parents=True, exist_ok=True)

    pid = os.getpid()
    staging = parent / f"{pkg_kb_dir.name}.staging-{pid}"
    rotated_old = parent / f"{pkg_kb_dir.name}.old-{pid}"
    if staging.exists():
        shutil.rmtree(staging)
    if rotated_old.exists():
        shutil.rmtree(rotated_old)

    copied = 0
    try:
        staging.mkdir(parents=True)
        for art in manifest.kb_articles:
            src = (install_path / art.path).resolve()
            try:
                src.relative_to(install_path.resolve())
            except ValueError:
                logger.warning(
                    "kb_articles: %r escapes package root; skipped",
                    art.path,
                )
                continue
            if not src.is_file():
                logger.warning(
                    "kb_articles: source %s missing; skipped", src,
                )
                continue
            # KB layout (matching ``KBStore`` conventions): every entry
            # is a markdown file at ``<slug>.md`` (or a folder
            # ``<slug>/_index.md``).  We use the leaf-file form: slug
            # ``email/overview`` → ``email/overview.md``.
            dest = staging / (art.slug + ".md")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            copied += 1

        # Atomic swap.
        swapped = pkg_kb_dir.exists()
        if swapped:
            os.replace(str(pkg_kb_dir), str(rotated_old))
        os.replace(str(staging), str(pkg_kb_dir))
        if swapped and rotated_old.exists():
            shutil.rmtree(rotated_old)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if rotated_old.exists() and not pkg_kb_dir.exists():
            try:
                os.replace(str(rotated_old), str(pkg_kb_dir))
            except OSError:
                logger.exception(
                    "kb_articles: rollback failed for package %r",
                    manifest.name,
                )
        logger.exception(
            "kb_articles: install failed for package %r", manifest.name,
        )
        return 0

    # Re-index from filesystem so the KB DB picks up the new files.
    # KBStore.sync_from_filesystem is idempotent and walks the whole
    # tree — fine to call after every package install.
    try:
        from ..kb.store import KBStore
        KBStore(str(kb_root)).sync_from_filesystem()
    except Exception:  # noqa: BLE001 — best-effort
        logger.exception(
            "kb_articles: sync_from_filesystem after install of %r failed",
            manifest.name,
        )

    return copied


def _uninstall_kb_articles(package_name: str) -> None:
    """Remove the package's KB folder and re-index.  Idempotent."""
    kb_root = _kb_root_dir()
    if kb_root is None:
        return
    pkg_kb_dir = _package_kb_dir(kb_root, package_name)
    if pkg_kb_dir.exists():
        shutil.rmtree(pkg_kb_dir)
        try:
            from ..kb.store import KBStore
            KBStore(str(kb_root)).sync_from_filesystem()
        except Exception:  # noqa: BLE001 — best-effort
            logger.exception(
                "kb_articles: sync_from_filesystem after uninstall of %r failed",
                package_name,
            )


def _subscriptions_record_path(install_path: Path) -> Path:
    """Per-package subscriptions JSON record under the install dir."""
    return install_path / "_subscriptions.json"


def _install_trigger_subscriptions(
    manifest: PackageManifest, install_path: Path,
) -> int:
    """Register trigger subscriptions in-memory and persist the JSON record.

    The in-memory ``Subscription`` objects carry a ``source_package``
    attribute so :func:`subscriptions.unregister_for_package` can drop
    them cleanly on uninstall.  The on-disk JSON record is the source
    of truth a fresh process reads at startup so the registrations
    survive restarts.

    Returns the number of subscriptions registered (in-memory).
    """
    if not manifest.trigger_subscriptions:
        # Still wipe any stale record from a prior install whose new
        # manifest dropped subscriptions, so restart doesn't reload them.
        rec = _subscriptions_record_path(install_path)
        if rec.exists():
            rec.unlink()
        return 0

    payload = [
        {"event": s.event, "handler": s.handler}
        for s in manifest.trigger_subscriptions
    ]
    import json as _json
    rec = _subscriptions_record_path(install_path)
    rec.write_text(_json.dumps(payload, indent=2))

    try:
        from ..core.engine import subscriptions as _subs
    except ImportError:
        logger.warning(
            "subscriptions module unavailable; package %r subscriptions "
            "will be loaded only on next restart that imports it",
            manifest.name,
        )
        return 0

    # Drop any prior registrations for this package so re-installs
    # don't double-register.
    if hasattr(_subs, "unregister_for_package"):
        _subs.unregister_for_package(manifest.name)

    registered = 0
    for i, s in enumerate(manifest.trigger_subscriptions):
        sub = _subs.Subscription(
            name=f"_pkg.{manifest.name}.{i}",
            event_type=s.event,
            event_filter=None,
            action_type="package_dispatch",
            action_config={
                "package": manifest.name,
                "handler": s.handler,
            },
            enabled=True,
            # Tag the subscription with its source package so
            # ``unregister_for_package`` can find it.
            source_package=manifest.name,
        )
        _subs._subscriptions.append(sub)
        registered += 1
    return registered


def _install_triggers(
    manifest: PackageManifest, install_path: Path,
) -> int:
    """Register in-process Trigger instances declared in ``manifest.triggers``.

    For each declared trigger:

    1. Import the ``<install_path>/<module>`` Python file and register
       any :class:`Trigger` subclasses it defines, tagged with
       ``source_package=manifest.name`` (so uninstall can drop them).
    2. Instantiate the trigger with its config dict, passing through
       ``source_package=manifest.name`` and a
       :class:`PackageStateHandle` bound to the same name.
    3. Call ``start()`` on the new instance so it can self-register
       (e.g., create cron rows, open connections).

    Idempotent on re-install: prior triggers + types for the package are
    dropped via :func:`registry.unregister_for_package` before re-loading.

    Returns the number of trigger instances successfully started.
    """
    if not manifest.triggers:
        # Even when the new manifest declares no triggers, scrub any
        # leftovers from a prior install version.
        try:
            from ..core.engine.triggers import registry as _treg
            _treg.unregister_for_package(manifest.name)
        except ImportError:
            pass
        return 0

    try:
        from ..core.engine.triggers import registry as _treg
    except ImportError:
        logger.warning(
            "trigger registry unavailable; package %r triggers will not "
            "be loaded until next restart that imports it",
            manifest.name,
        )
        return 0

    # Drop any prior registrations for this package so re-install is
    # idempotent.
    _treg.unregister_for_package(manifest.name)

    # Construct the package-scoped state handle exactly once and reuse
    # it across this package's triggers.  I9: the handle's bound name
    # is ``manifest.name``; the base ``Trigger.__init__`` cross-checks
    # ``source_package == handle.package_name``.
    try:
        from .state import PackageStateHandle
        state_handle = PackageStateHandle(manifest.name)
    except Exception:
        logger.exception(
            "Could not build PackageStateHandle for %r; triggers will be "
            "loaded without per-package state",
            manifest.name,
        )
        state_handle = None

    # Same pattern for the package-scoped vector store handle (Phase 2 PR-2 / D10).
    # The embedding service is resolved lazily inside the handle so daemon boot
    # does not depend on the service being ready at install time.
    try:
        from .vectors import PackageVectorStore
        vector_handle = PackageVectorStore(manifest.name)
    except Exception:
        logger.exception(
            "Could not build PackageVectorStore for %r; triggers will be "
            "loaded without per-package vector access",
            manifest.name,
        )
        vector_handle = None

    # First pass: import each unique module and register trigger types.
    seen_modules: set[str] = set()
    for tref in manifest.triggers:
        if tref.module in seen_modules:
            continue
        seen_modules.add(tref.module)
        module_path = install_path / tref.module
        try:
            _treg.load_package_trigger_module(
                module_path, source_package=manifest.name,
            )
        except Exception:
            logger.exception(
                "Failed to load trigger module %s for package %r",
                module_path, manifest.name,
            )

    # Second pass: instantiate each declared trigger via load_triggers.
    trigger_configs: list[dict] = []
    for tref in manifest.triggers:
        if not tref.enabled:
            logger.info(
                "Trigger %s (package %r) is disabled; skipping instantiation",
                tref.name, manifest.name,
            )
            continue
        cfg = dict(tref.config)
        cfg["name"] = tref.name
        cfg["type"] = tref.type
        cfg["enabled"] = True
        trigger_configs.append(cfg)

    if not trigger_configs:
        return 0

    instances = _treg.load_package_triggers(
        trigger_configs,
        source_package=manifest.name,
        package_state=state_handle,
        package_vectors=vector_handle,
    )
    # Best-effort start() — if a trigger's start() fails we still
    # register it (load_triggers already added it to _instances) so that
    # uninstall can clean it up symmetrically.
    started = 0
    for inst in instances:
        try:
            inst.start()
            started += 1
        except Exception:
            logger.exception(
                "Trigger %s (package %r) failed to start",
                inst.name, manifest.name,
            )
    if started:
        logger.info(
            "Installed %d trigger instance(s) for package %r",
            started, manifest.name,
        )
    return started


def _uninstall_triggers(package_name: str) -> int:
    """Tear down in-process triggers + types for ``package_name``.

    Called from :func:`uninstall_package` before the install dir is
    removed.  Idempotent.
    """
    if not package_name:
        return 0
    try:
        from ..core.engine.triggers import registry as _treg
    except ImportError:
        return 0
    return _treg.unregister_for_package(package_name)


def _env_key_is_set(key: str) -> bool:
    """Return True if ``key`` already has a value the package can read.

    A package's pre-verified EXECUTOR scripts read credentials via
    ``os.environ.get(key)``, and the daemon mirrors ``.env`` writes into
    ``os.environ`` (see :mod:`carpenter.util.dot_env`).  We therefore
    treat a key as "already set" if it is present *either* in the live
    process environment *or* in the loaded platform config (which layers
    in ``{base_dir}/.env`` for known credential keys).  Checking both is
    deliberately permissive: the goal is to avoid re-prompting the
    operator for a value they have already supplied, by whatever path.
    """
    if os.environ.get(key):
        return True
    try:
        from .. import config
    except ImportError:  # pragma: no cover — config always present in prod
        return False
    cfg = config.CONFIG
    return bool(cfg.get(key) or cfg.get(key.lower()))


def _request_env_credentials(manifest: PackageManifest) -> tuple[dict, ...]:
    """Create one-time credential requests for declared ``kind: env`` creds.

    For each :class:`EnvCredentialRef` in the manifest, and for each of
    its ``required_keys`` suffixes, computes the full env var name
    ``f"{env_key_prefix}_{suffix}"`` and — unless that key is already set
    — creates a credential request via
    :func:`carpenter.api.credentials.create_credential_request`.  Reuses
    the existing one-time-link intake mechanism rather than inventing a
    parallel flow.

    OAuth credential refs are intentionally ignored here — they are
    handled by the separate OAuth-callback flow
    (:mod:`carpenter.api.oauth`).

    Install never hard-fails on missing env creds (mirrors the OAuth
    posture); the operator provides the values out-of-band via the
    returned request URLs, and the write path mirrors them into
    ``os.environ`` so subsequently-spawned EXECUTOR subprocesses pick
    them up without a daemon restart.

    Returns a tuple of request-info dicts (``key``, ``provider``,
    ``request_id``, ``url``).  Returns an empty tuple if the package
    declares no env creds, if all keys are already set, or if the
    credentials API is unavailable (e.g. minimal test builds).
    """
    env_refs = [
        c for c in manifest.credential_requirements
        if isinstance(c, EnvCredentialRef)
    ]
    if not env_refs:
        return ()

    try:
        from ..api.credentials import create_credential_request
    except ImportError:
        logger.warning(
            "credentials API unavailable; cannot create env-credential "
            "requests for package %r (declared %d env cred ref(s))",
            manifest.name, len(env_refs),
        )
        return ()

    requests: list[dict] = []
    for ref in env_refs:
        for suffix in ref.required_keys:
            key = f"{ref.env_key_prefix}_{suffix}"
            if _env_key_is_set(key):
                logger.debug(
                    "env credential %s for package %r already set; "
                    "not re-requesting", key, manifest.name,
                )
                continue
            info = create_credential_request(
                key,
                label=key,
                description=(
                    f"Required by package {manifest.name} "
                    f"({ref.provider})"
                ),
            )
            requests.append({
                "key": key,
                "provider": ref.provider,
                "request_id": info["request_id"],
                "url": info["url"],
            })

    if requests:
        logger.info(
            "Package %r needs %d env credential(s); created credential "
            "request(s) for: %s.  Provide them via the one-time link(s) "
            "(install proceeds without them).",
            manifest.name, len(requests),
            ", ".join(r["key"] for r in requests),
        )
    return tuple(requests)


# ── Platform-capability trust acknowledgment ───────────────────────


def _describe_capability(cap: PlatformCapabilityRef) -> str:
    """One-line operator-facing description of a capability + its scope."""
    g = cap.grant
    host_var = f"{g.credential_ref}_{g.host_from}"
    return (
        f"  • verb {cap.verb!r} (kind={cap.kind}) — TRUSTED handler "
        f"{cap.module}:{cap.handler}\n"
        f"      egress: {g.protocol}://<{host_var}>:{g.port}  "
        f"credential: {g.credential_ref}_*"
    )


def confirm_platform_capabilities(
    manifest: PackageManifest,
    *,
    prompt_stream=None,
    input_fn=None,
) -> tuple[PlatformCapabilityRef, ...]:
    """Obtain INTERACTIVE operator consent to grant platform-level trust.

    If the manifest declares no ``platform_capabilities`` this is a no-op
    returning ``()``.  Otherwise:

    * If not running interactively (no tty on stdin), raise
      :class:`InstallError` with a nonzero-exit-worthy message — a
      non-interactive install MUST NOT auto-grant platform-level trust.
    * Otherwise print a clear notice that installing GRANTS PLATFORM-LEVEL
      TRUST, list each capability + its scope (verb, egress host:port,
      credential), and require an explicit affirmative (``yes``).  Any
      other response declines and returns ``()`` (nothing granted).

    Args:
        manifest: The package manifest.
        prompt_stream: Stream to print the prompt to (default stderr).
        input_fn: Callable returning the operator's typed response
            (default :func:`input`).  Both are injectable for tests.

    Returns:
        The tuple of confirmed capabilities (all-or-nothing: an
        affirmative grants every declared capability; a decline grants
        none).
    """
    caps = manifest.platform_capabilities
    if not caps:
        return ()

    import sys as _sys
    stream = prompt_stream if prompt_stream is not None else _sys.stderr

    # Non-interactive guard: refuse to auto-grant platform-level trust.
    # We treat "interactive" as stdin being a tty.  A test injects
    # ``input_fn`` AND treats that as explicitly interactive.
    interactive = input_fn is not None or (
        hasattr(_sys.stdin, "isatty") and _sys.stdin.isatty()
    )
    if not interactive:
        raise InstallError(
            f"Package {manifest.name!r} declares {len(caps)} platform "
            f"capability(ies) which would grant PLATFORM-LEVEL TRUST "
            f"(trusted parent-side handlers with egress + credentials). "
            f"This requires an interactive confirmation and cannot be "
            f"auto-granted in a non-interactive install. Re-run "
            f"interactively to confirm, or remove the platform_capabilities "
            f"from the manifest.",
        )

    ask = input_fn if input_fn is not None else input
    lines = [
        "",
        "=" * 72,
        f"  SECURITY: package {manifest.name!r} requests PLATFORM-LEVEL TRUST",
        "=" * 72,
        "",
        "  Installing this package will register TRUSTED, platform-side",
        "  dispatch-verb handlers. These run with the platform's egress and",
        "  credentials — a DIFFERENT trust class than the package's sandboxed",
        "  executor scripts. Trusted handler code CANNOT be sandboxed.",
        "",
        "  Capabilities requested:",
    ]
    for cap in caps:
        lines.append(_describe_capability(cap))
    lines += [
        "",
        "  Granting trust means you have reviewed this package's handler",
        "  code and accept that it runs with platform privileges.",
        "",
    ]
    print("\n".join(lines), file=stream)

    try:
        response = ask(
            f"Grant platform-level trust to {manifest.name!r}? Type 'yes' "
            f"to confirm: ",
        )
    except EOFError:
        # stdin closed mid-prompt — treat as decline (fail-closed).
        response = ""

    if isinstance(response, str) and response.strip().lower() == "yes":
        logger.warning(
            "Operator GRANTED platform-level trust to package %r "
            "(%d capability(ies): %s)",
            manifest.name, len(caps),
            ", ".join(c.verb for c in caps),
        )
        return tuple(caps)

    logger.info(
        "Operator DECLINED platform-level trust for package %r; "
        "no capabilities granted",
        manifest.name,
    )
    return ()


# ── Install / uninstall / verify ────────────────────────────────────


def install_package(
    source_path: Path | str,
    dest_path: Path | str,
    *,
    conn: sqlite3.Connection,
    capability_input_fn=None,
    capability_prompt_stream=None,
    allow_write_chat_tools: bool = False,
) -> InstallResult:
    """Validate, hash, and install a capability package.

    Args:
        source_path: Path to the source package directory (the parent
            of ``manifest.yaml``).
        dest_path: Where to materialize the installed copy
            (``~/carpenter/packages/<name>/`` in production).  The
            directory is created if missing; if it exists it is
            atomically replaced.
        conn: SQLite connection for recording the install.
        allow_write_chat_tools: Explicit operator opt-in to enable the
            package's WRITE-capable chat-boundary tools (I10 relaxation).
            Default ``False`` — the chat agent is read-only by default,
            so the package's write chat tools are recorded as gated-off
            and the registry will SKIP registering them.  Set ``True``
            only on explicit operator consent (CLI ``--allow-write-chat-
            tools`` or interactive prompt).  This is SEPARATE from the
            platform-capability egress grant (``capability_input_fn``).

    Returns:
        :class:`InstallResult` with the recorded hash + paths.

    Raises:
        InstallError: source missing/invalid, hash failure, atomic
            swap failure, or DB record failure.
        ManifestError: manifest fails shape validation.
        PackageSecurityError: manifest fails security guards.
    """
    source_path = Path(source_path).resolve()
    dest_path = Path(dest_path).resolve()

    if not source_path.is_dir():
        raise InstallError(f"source {source_path} is not a directory")
    manifest_file = source_path / "manifest.yaml"
    if not manifest_file.is_file():
        raise InstallError(
            f"source {source_path} has no manifest.yaml",
        )

    # Manifest + security validation BEFORE we touch the dest.
    manifest = load_manifest(manifest_file)
    import yaml
    with open(manifest_file, encoding="utf-8") as fp:
        raw = yaml.safe_load(fp)
    if not isinstance(raw, dict):
        raise ManifestError(
            f"manifest at {manifest_file} must be a mapping",
        )
    validate_manifest_security(
        manifest, raw_manifest=raw, manifest_path=manifest_file,
    )

    # Manifest 'name' must match the dest dir name (defense in depth
    # against installing pkg X into Y/'s slot).
    if dest_path.name != manifest.name:
        raise InstallError(
            f"dest_path basename {dest_path.name!r} does not match "
            f"manifest name {manifest.name!r}",
        )

    # Package-capability framework: obtain INTERACTIVE operator consent to
    # grant PLATFORM-LEVEL TRUST *before* we touch the dest.  This raises
    # InstallError on a non-interactive install that would grant trust
    # (never auto-grant); a decline returns () so nothing is recorded as
    # granted (the package still installs, but its capability verbs are
    # never registered).  Done before materialization so a refusal leaves
    # the destination untouched.
    granted_capabilities = confirm_platform_capabilities(
        manifest,
        prompt_stream=capability_prompt_stream,
        input_fn=capability_input_fn,
    )

    pkg_hash = compute_package_hash(source_path)

    # Capture the prior install record (if any) BEFORE we mutate the DB,
    # so the proposal diff in InstallResult reflects "what changed
    # between the previous install and now".  Fresh installs see an
    # empty prior set.
    prior_record = get_install_record(conn, manifest.name)
    diff = compute_proposal_diff(manifest, prior_record)

    was_update = _atomic_copy_into_place(source_path, dest_path)

    # Reload the manifest from the *installed* copy so the recorded
    # source_path on the manifest is the install path.  Not strictly
    # required, but matches the invariant "manifest.source_path == on
    # disk location of the registered package".
    installed_manifest = load_manifest(dest_path / "manifest.yaml")
    if installed_manifest.name != manifest.name:
        raise InstallError(
            f"installed manifest name {installed_manifest.name!r} does "
            f"not match staged manifest {manifest.name!r}",
        )

    # Re-bind the granted capabilities to the *installed* manifest objects
    # so the recorded refs match the on-disk copy (the staged and
    # installed manifests are byte-identical, but this keeps provenance
    # consistent).  Match by verb; only verbs the operator confirmed are
    # carried forward.
    granted_verbs = {c.verb for c in granted_capabilities}
    granted_installed_caps = tuple(
        c for c in installed_manifest.platform_capabilities
        if c.verb in granted_verbs
    )

    installed_at = datetime.now(timezone.utc).isoformat()
    _record_install(
        conn,
        manifest=installed_manifest,
        source_path=source_path,
        install_path=dest_path,
        pkg_hash=pkg_hash,
        installed_at=installed_at,
        granted_capabilities=granted_installed_caps,
        write_chat_tools_allowed=allow_write_chat_tools,
    )

    # B-full: merge the proposed allowlist additions into the platform's
    # SecurityPolicies (SD5 — flat-global, one-way ratchet).  Each
    # ``added`` pair is added directly via ``security.policies.add`` to
    # the in-memory singleton AND inserted into ``security_policies`` so
    # it survives a restart.  Removed pairs are NOT applied (one-way
    # ratchet); they show up in the diff for operator transparency only.
    _merge_allowlist_proposals(conn, diff.added)

    # B-full: install KB articles (per-package folder under <kb_root>).
    kb_articles_n = _install_kb_articles(installed_manifest, dest_path)

    # B-full: register trigger subscriptions (in-memory, tagged with the
    # package name so uninstall can drop them) and persist a
    # ``_subscriptions.json`` record under the install dir for restart
    # bookkeeping.
    subs_n = _install_trigger_subscriptions(installed_manifest, dest_path)

    # Phase 3a PR-B: instantiate any in-process Trigger classes the
    # package's manifest declared (idempotent on re-install: prior
    # registrations are dropped first).
    triggers_n = _install_triggers(installed_manifest, dest_path)

    # ``kind: env`` credentials: for each declared env-credential ref,
    # create a one-time credential request for every required env var
    # that is not already set.  Install does NOT hard-fail on missing
    # env creds (mirrors the OAuth posture); the operator supplies the
    # values out-of-band via the returned request URLs and the write
    # path mirrors them into ``os.environ`` for live delivery to
    # EXECUTOR subprocesses.  OAuth refs are handled by the separate
    # OAuth-callback flow and are skipped here.
    env_cred_requests = _request_env_credentials(installed_manifest)

    logger.info(
        "Installed capability package %r v%s (hash %s, %s)",
        manifest.name, manifest.version, pkg_hash[:12],
        "update" if was_update else "fresh install",
    )
    return InstallResult(
        name=manifest.name,
        version=manifest.version,
        hash=pkg_hash,
        source_path=source_path,
        dest_path=dest_path,
        installed_at=installed_at,
        was_update=was_update,
        allowlist_added=diff.added,
        allowlist_removed=diff.removed,
        kb_articles_installed=kb_articles_n,
        trigger_subscriptions_registered=subs_n,
        triggers_installed=triggers_n,
        env_credential_requests=env_cred_requests,
        platform_capabilities_granted=tuple(
            _capability_to_dict(c) for c in granted_installed_caps
        ),
        write_chat_tools_allowed=allow_write_chat_tools,
    )


def list_blocking_arcs(
    conn: sqlite3.Connection, package_name: str,
) -> list[tuple[int, str, str | None]]:
    """Return non-terminal arcs created from this package's templates.

    Used by :func:`uninstall_package` (and its chat-tool wrapper) to
    block / surface uninstalls that would leave live arcs without
    their template definitions.

    Returns a list of ``(arc_id, template_name, status)`` tuples.

    The ``arcs`` table column for the template name varies between
    schema generations; we try both ``template_name`` and the modern
    ``template_id`` join via ``workflow_templates`` and union the
    results so this works on either layout.
    """
    template_rows = conn.execute(
        "SELECT template_name FROM installed_packages_templates "
        "WHERE package_name = ?",
        (package_name,),
    ).fetchall()
    template_names = [r[0] for r in template_rows]
    if not template_names:
        return []

    # Probe schema once.
    arc_cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(arcs)"
    ).fetchall()}

    placeholders = ",".join(["?"] * len(template_names))
    blocking: list[tuple[int, str, str | None]] = []

    if "template_name" in arc_cols:
        query = (
            f"SELECT id, template_name, status FROM arcs "
            f"WHERE template_name IN ({placeholders}) "
            f"AND (status IS NULL OR status NOT IN "
            f"('completed','failed','cancelled','terminated'))"
        )
        for row in conn.execute(query, template_names).fetchall():
            blocking.append((row[0], row[1], row[2]))

    if "template_id" in arc_cols:
        # workflow_templates may or may not be present in tests with a
        # minimal schema; fail-soft if not.
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "workflow_templates" in tables:
            wt_cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(workflow_templates)"
            ).fetchall()}
            name_col = "name" if "name" in wt_cols else None
            if name_col:
                query = (
                    f"SELECT a.id, t.{name_col}, a.status "
                    f"FROM arcs a "
                    f"JOIN workflow_templates t ON a.template_id = t.id "
                    f"WHERE t.{name_col} IN ({placeholders}) "
                    f"AND (a.status IS NULL OR a.status NOT IN "
                    f"('completed','failed','cancelled','terminated'))"
                )
                for row in conn.execute(query, template_names).fetchall():
                    triple = (row[0], row[1], row[2])
                    if triple not in blocking:
                        blocking.append(triple)

    return blocking


def uninstall_package(
    name: str,
    *,
    conn: sqlite3.Connection,
    force: bool = False,
    archive_state: bool = False,
) -> UninstallResult:
    """Uninstall a previously-installed capability package.

    Args:
        name: Package name.
        conn: SQLite connection.
        force: If True, skip the "blocking arcs" check.  The chat tool
            does NOT pass force=True; this exists so that operator
            tooling can clean up a package whose templates are stuck.
        archive_state: If True, copy the package's ``package_state``
            rows into ``package_state_archive`` BEFORE the FK cascade
            deletes them.  A future re-install can restore state via
            :func:`carpenter.packages.state.restore_from_archive`.
            Default False (wipe state; the FK cascade does the actual
            delete via ``ON DELETE CASCADE``).

    Returns:
        :class:`UninstallResult`.

    Raises:
        InstallError: package not installed, or blocking arcs exist.
    """
    record = get_install_record(conn, name)
    if record is None:
        raise InstallError(f"package {name!r} is not installed")

    if not force:
        blockers = list_blocking_arcs(conn, name)
        if blockers:
            details = ", ".join(
                f"arc {a} ({t}, status={s!r})" for (a, t, s) in blockers[:10]
            )
            extra = f" (and {len(blockers)-10} more)" if len(blockers) > 10 else ""
            raise InstallError(
                f"refusing to uninstall {name!r}: {len(blockers)} "
                f"non-terminal arc(s) reference templates this package "
                f"shipped: {details}{extra}",
            )

    install_path = Path(record["install_path"])

    # B-full: drop in-memory trigger subscriptions BEFORE we delete the
    # install dir (so the JSON record is still around if anything
    # consults it during teardown).  Uninstall does NOT touch
    # ``security_policies`` (SD5: one-way ratchet).
    try:
        from ..core.engine import subscriptions as _subs
        if hasattr(_subs, "unregister_for_package"):
            _subs.unregister_for_package(name)
    except ImportError:
        logger.debug(
            "subscriptions module unavailable; skipping in-memory cleanup "
            "on uninstall of %r", name,
        )

    # Phase 3a PR-B: tear down in-process trigger instances + type
    # registrations the package contributed.  Each instance's ``stop()``
    # hook is invoked (best-effort).  Idempotent.
    _uninstall_triggers(name)

    # Phase 3a (D24): archive or wipe the package's mutable state BEFORE
    # the FK cascade fires.  ``archive_state=True`` copies rows into
    # ``package_state_archive`` (which has no FK and survives the
    # cascade); ``False`` is a no-op, the cascade wipes the live rows.
    if archive_state:
        try:
            from .state import archive_for_uninstall
            archive_for_uninstall(name, conn=conn)
        except sqlite3.OperationalError as exc:
            # Missing table → log and continue (minimal test DBs).
            logger.debug(
                "archive_for_uninstall(%r) skipped: %s", name, exc,
            )
        except ImportError:
            logger.debug(
                "package state module unavailable; skipping archive of %r",
                name,
            )

    if install_path.exists():
        shutil.rmtree(install_path)

    # B-full: remove the package's KB folder + re-index.
    _uninstall_kb_articles(name)

    _delete_install_record(conn, name)

    # D24 stage 3b: drop the package's runtime-registry registrations.
    # The DB rows are gone; this clears the in-memory JUDGE handler
    # map, kind map, and step-handler tracking so the dispatch path
    # stops routing to package code that no longer exists.  Allowlists
    # are NOT touched (SD5: one-way ratchet).
    #
    # Only ``ImportError`` is genuinely defensive here (stripped builds
    # may not have the packages registry module).  Any other exception
    # — particularly from ``unregister_package`` itself — is a real bug
    # we want to surface, not silently swallow (PR #306 followup).
    try:
        from .handler_registry import get_handler_registry
    except ImportError:
        logger.warning(
            "handler_registry unavailable; skipping runtime-registry "
            "cleanup on uninstall of %r",
            name,
        )
    else:
        get_handler_registry().unregister_package(name)

    # Package-capability framework: drop the package's registered trusted
    # dispatch verbs so the dispatch path stops routing to handler code
    # that no longer exists.
    try:
        from .capabilities import get_capability_registry
    except ImportError:
        logger.warning(
            "capabilities registry unavailable; skipping capability "
            "cleanup on uninstall of %r", name,
        )
    else:
        get_capability_registry().unregister_package(name)

    # Drop the package's T1 trusted-capability-handler path classifications.
    try:
        from ..security.platform_paths import (
            unregister_trusted_capability_paths_under,
        )
        unregister_trusted_capability_paths_under(str(install_path))
    except ImportError:
        pass

    logger.info(
        "Uninstalled capability package %r (path %s)", name, install_path,
    )
    return UninstallResult(name=name, removed_path=install_path)


def verify_install(
    name: str, *, conn: sqlite3.Connection,
) -> VerifyResult:
    """Recompute the hash of an installed package and compare to the record.

    Called at server startup for every installed package.  On
    mismatch the registry refuses to load the package and logs the
    discrepancy (SD6).

    Returns:
        :class:`VerifyResult`.  Callers branch on ``ok``.
    """
    record = get_install_record(conn, name)
    if record is None:
        return VerifyResult(
            name=name, ok=False, expected_hash=None, actual_hash=None,
            install_path=Path(),
            message=f"no install record found for {name!r}",
        )
    install_path = Path(record["install_path"])
    if not install_path.is_dir():
        return VerifyResult(
            name=name, ok=False, expected_hash=record["hash"],
            actual_hash=None, install_path=install_path,
            message=(
                f"install path {install_path} missing for package {name!r} "
                f"(expected hash {record['hash'][:12]})"
            ),
        )
    try:
        actual = compute_package_hash(install_path)
    except Exception as exc:  # pragma: no cover — defensive
        return VerifyResult(
            name=name, ok=False, expected_hash=record["hash"],
            actual_hash=None, install_path=install_path,
            message=f"hash recompute failed: {exc}",
        )
    if actual != record["hash"]:
        return VerifyResult(
            name=name, ok=False, expected_hash=record["hash"],
            actual_hash=actual, install_path=install_path,
            message=(
                f"hash mismatch for package {name!r}: "
                f"expected {record['hash'][:12]}..., got {actual[:12]}..."
            ),
        )
    return VerifyResult(
        name=name, ok=True, expected_hash=record["hash"],
        actual_hash=actual, install_path=install_path,
        message="ok",
    )
