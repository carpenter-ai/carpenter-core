"""Per-package mutable state primitive (D24 / Phase 3a).

Each capability package gets its own (key, value_json) keyspace in the
``package_state`` table, isolated from every other package by the
(package_name, key) primary key and the FK ``ON DELETE CASCADE`` to
``installed_packages.name``.

Why a platform table (not per-package SQLite files)
---------------------------------------------------
- Hash-pinned install trees are immutable by design.  Co-located state
  files would either break the hash invariant or require an out-of-tree
  state dir per package, multiplying complexity.
- WAL contention with the main DB would be worse than a single shared
  table managed by the platform.
- Centralising state lets uninstall cleanly wipe (cascade) or archive
  (copy-then-cascade) without each package owning that logic.

Isolation invariant (I9 — least privilege between packages)
-----------------------------------------------------------
A :class:`PackageStateHandle` is bound to a single ``package_name`` at
construction.  The ``package_name`` is intended to be private — its
underscore prefix flags that callers must not introspect or rebind it
after construction.  All methods operate exclusively on
``self._package_name``; there is no API to read/write another
package's keys via a handle.  Packages never receive another package's
handle.  Combined with the SQL primary key (package_name, key) and the
FK cascade, cross-package state access is impossible by construction.

CAS semantics
-------------
``version`` is a monotonically-increasing integer.  Callers that need
optimistic concurrency read via :func:`get_with_version` and write via
:func:`cas` with the expected version; on a concurrent update the CAS
returns False and the caller retries.  This is the contention guard
used by GmailPollTrigger's ``poll_in_progress`` flag.

Module-level functions take ``package_name`` explicitly and are the
low-level API used by the installer (which doesn't have a handle).
Application code (triggers / handlers) receives a
:class:`PackageStateHandle` from the loader and uses its bound methods.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ..db import get_db

logger = logging.getLogger(__name__)


# ── module-level API (package_name as explicit arg) ──────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get(
    package_name: str,
    key: str,
    default: Any = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> Any:
    """Return the JSON-decoded value for ``(package_name, key)``.

    Returns ``default`` if the key does not exist.  Caller is
    responsible for treating the returned value as the package's
    keyspace; nothing in this module enforces type uniformity across
    calls.
    """
    db = conn if conn is not None else get_db()
    try:
        row = db.execute(
            "SELECT value_json FROM package_state "
            "WHERE package_name = ? AND key = ?",
            (package_name, key),
        ).fetchone()
        if row is None:
            return default
        # Row may be a tuple (raw connection) or sqlite3.Row.
        raw = row[0] if not isinstance(row, sqlite3.Row) else row["value_json"]
        return json.loads(raw)
    finally:
        if conn is None:
            db.close()


def get_with_version(
    package_name: str,
    key: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[Any, int] | None:
    """Return ``(value, version)`` for ``(package_name, key)``, or None.

    The version is the value to pass back to :func:`cas` for an
    optimistic update.
    """
    db = conn if conn is not None else get_db()
    try:
        row = db.execute(
            "SELECT value_json, version FROM package_state "
            "WHERE package_name = ? AND key = ?",
            (package_name, key),
        ).fetchone()
        if row is None:
            return None
        if isinstance(row, sqlite3.Row):
            raw = row["value_json"]
            version = row["version"]
        else:
            raw = row[0]
            version = row[1]
        return json.loads(raw), int(version)
    finally:
        if conn is None:
            db.close()


def set(
    package_name: str,
    key: str,
    value: Any,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Unconditionally upsert ``value`` for ``(package_name, key)``.

    Returns the new version number.  Use :func:`cas` when concurrent
    writers must be detected; :func:`set` is the "last write wins"
    path for callers that don't need optimistic concurrency.
    """
    payload = json.dumps(value, sort_keys=True)
    now = _now_iso()
    own_conn = conn is None
    db = conn if conn is not None else get_db()
    try:
        cur = db.execute(
            "SELECT version FROM package_state "
            "WHERE package_name = ? AND key = ?",
            (package_name, key),
        ).fetchone()
        if cur is None:
            db.execute(
                "INSERT INTO package_state "
                "(package_name, key, value_json, version, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (package_name, key, payload, now, now),
            )
            new_version = 1
        else:
            existing = cur[0] if not isinstance(cur, sqlite3.Row) else cur["version"]
            new_version = int(existing) + 1
            db.execute(
                "UPDATE package_state "
                "SET value_json = ?, version = ?, updated_at = ? "
                "WHERE package_name = ? AND key = ?",
                (payload, new_version, now, package_name, key),
            )
        if own_conn:
            db.commit()
        return new_version
    finally:
        if own_conn:
            db.close()


def cas(
    package_name: str,
    key: str,
    expected_version: int,
    new_value: Any,
    *,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Compare-and-swap update.

    Updates the row to ``new_value`` (and bumps the version) iff the
    current ``version`` equals ``expected_version``.  Returns True on
    success, False on version mismatch / missing row.

    For new-row CAS (no prior write), pass ``expected_version=0``: this
    is treated as "INSERT only if absent".  Any other expected version
    on a missing row fails.
    """
    payload = json.dumps(new_value, sort_keys=True)
    now = _now_iso()
    own_conn = conn is None
    db = conn if conn is not None else get_db()
    try:
        row = db.execute(
            "SELECT version FROM package_state "
            "WHERE package_name = ? AND key = ?",
            (package_name, key),
        ).fetchone()
        if row is None:
            if expected_version != 0:
                return False
            db.execute(
                "INSERT INTO package_state "
                "(package_name, key, value_json, version, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (package_name, key, payload, now, now),
            )
            if own_conn:
                db.commit()
            return True
        existing = row[0] if not isinstance(row, sqlite3.Row) else row["version"]
        if int(existing) != int(expected_version):
            return False
        new_version = int(existing) + 1
        cur = db.execute(
            "UPDATE package_state "
            "SET value_json = ?, version = ?, updated_at = ? "
            "WHERE package_name = ? AND key = ? AND version = ?",
            (payload, new_version, now, package_name, key, int(expected_version)),
        )
        if cur.rowcount == 0:
            # Lost the race between SELECT and UPDATE.
            return False
        if own_conn:
            db.commit()
        return True
    finally:
        if own_conn:
            db.close()


def delete(
    package_name: str,
    key: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Delete ``(package_name, key)``.

    Returns True if a row was removed, False if nothing matched.
    """
    own_conn = conn is None
    db = conn if conn is not None else get_db()
    try:
        cur = db.execute(
            "DELETE FROM package_state "
            "WHERE package_name = ? AND key = ?",
            (package_name, key),
        )
        if own_conn:
            db.commit()
        return cur.rowcount > 0
    finally:
        if own_conn:
            db.close()


def list_keys(
    package_name: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Return all keys for ``package_name`` (sorted)."""
    own_conn = conn is None
    db = conn if conn is not None else get_db()
    try:
        rows = db.execute(
            "SELECT key FROM package_state "
            "WHERE package_name = ? ORDER BY key",
            (package_name,),
        ).fetchall()
        if not rows:
            return []
        if isinstance(rows[0], sqlite3.Row):
            return [r["key"] for r in rows]
        return [r[0] for r in rows]
    finally:
        if own_conn:
            db.close()


# ── archive / restore helpers ────────────────────────────────────────


def archive_for_uninstall(
    package_name: str,
    *,
    conn: sqlite3.Connection,
) -> int:
    """Copy every ``package_state`` row for ``package_name`` into the
    archive, then delete the originals (which would cascade-delete on
    the next ``installed_packages`` delete anyway, but doing it
    explicitly keeps the lifecycle obvious in logs).

    Caller is expected to hold a transaction (so that archive + cascade
    happen atomically with the install-record delete).  Returns the
    number of rows archived.  Idempotent: existing archive rows for
    ``(package_name, key)`` are REPLACEd.
    """
    if not package_name:
        return 0
    now = _now_iso()
    rows = conn.execute(
        "SELECT key, value_json, version FROM package_state "
        "WHERE package_name = ?",
        (package_name,),
    ).fetchall()
    archived = 0
    for row in rows:
        if isinstance(row, sqlite3.Row):
            key, value_json, version = row["key"], row["value_json"], row["version"]
        else:
            key, value_json, version = row[0], row[1], row[2]
        conn.execute(
            "INSERT OR REPLACE INTO package_state_archive "
            "(package_name, key, value_json, version, archived_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (package_name, key, value_json, int(version), now),
        )
        archived += 1
    # Explicit delete of the live rows so the FK cascade is a no-op.
    conn.execute(
        "DELETE FROM package_state WHERE package_name = ?",
        (package_name,),
    )
    if archived:
        logger.info(
            "Archived %d state row(s) for package %r", archived, package_name,
        )
    return archived


def restore_from_archive(
    package_name: str,
    *,
    conn: sqlite3.Connection,
) -> int:
    """Restore archived state rows for ``package_name`` into the live table.

    Intended to be called from the install path when re-installing a
    package that had its state archived on a prior uninstall.  Archived
    rows are deleted on successful restore so a subsequent archive cycle
    starts clean.  Caller owns the transaction.

    Returns the number of rows restored.
    """
    if not package_name:
        return 0
    now = _now_iso()
    rows = conn.execute(
        "SELECT key, value_json, version FROM package_state_archive "
        "WHERE package_name = ?",
        (package_name,),
    ).fetchall()
    restored = 0
    for row in rows:
        if isinstance(row, sqlite3.Row):
            key, value_json, version = row["key"], row["value_json"], row["version"]
        else:
            key, value_json, version = row[0], row[1], row[2]
        conn.execute(
            "INSERT OR REPLACE INTO package_state "
            "(package_name, key, value_json, version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (package_name, key, value_json, int(version), now, now),
        )
        restored += 1
    conn.execute(
        "DELETE FROM package_state_archive WHERE package_name = ?",
        (package_name,),
    )
    if restored:
        logger.info(
            "Restored %d archived state row(s) for package %r",
            restored, package_name,
        )
    return restored


def list_archived_packages(
    *, conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Return distinct package_names that have archived state rows."""
    own_conn = conn is None
    db = conn if conn is not None else get_db()
    try:
        rows = db.execute(
            "SELECT DISTINCT package_name FROM package_state_archive "
            "ORDER BY package_name"
        ).fetchall()
        if not rows:
            return []
        if isinstance(rows[0], sqlite3.Row):
            return [r["package_name"] for r in rows]
        return [r[0] for r in rows]
    finally:
        if own_conn:
            db.close()


# ── PackageStateHandle ───────────────────────────────────────────────


class PackageStateHandle:
    """A handle bound to a single package's state keyspace.

    Instances are created by the platform (installer / loader) and
    handed to package code (triggers, handlers).  The bound
    ``package_name`` is private — package code MUST NOT introspect or
    rebind it.  All methods route to the module-level functions with
    the bound name, so cross-package access via a handle is
    structurally impossible.

    Usage::

        handle = PackageStateHandle("my-package")
        watermark = handle.get("history_id", default=None)
        handle.set("history_id", 12345)
        ok = handle.cas("poll_in_progress", expected_version=0, new_value=True)
    """

    __slots__ = ("_package_name",)

    def __init__(self, package_name: str) -> None:
        if not isinstance(package_name, str) or not package_name.strip():
            raise ValueError(
                "PackageStateHandle requires a non-empty package_name "
                f"(got {package_name!r})",
            )
        # Bound at construction; methods only ever use this value.
        self._package_name = package_name

    @property
    def package_name(self) -> str:
        """Read-only accessor for the bound package name.

        Provided for logging / debugging only.  Mutation is impossible
        because ``__slots__`` and the property-only access prevent
        assignment from outside ``__init__``.
        """
        return self._package_name

    def get(self, key: str, default: Any = None) -> Any:
        return get(self._package_name, key, default=default)

    def get_with_version(self, key: str) -> tuple[Any, int] | None:
        return get_with_version(self._package_name, key)

    def set(self, key: str, value: Any) -> int:
        return set(self._package_name, key, value)

    def cas(self, key: str, expected_version: int, new_value: Any) -> bool:
        return cas(self._package_name, key, expected_version, new_value)

    def delete(self, key: str) -> bool:
        return delete(self._package_name, key)

    def list_keys(self) -> list[str]:
        return list_keys(self._package_name)

    def __repr__(self) -> str:
        return f"PackageStateHandle(package_name={self._package_name!r})"
