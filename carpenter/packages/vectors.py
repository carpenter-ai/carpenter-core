"""Per-package vector store primitive (D24 / Phase 2 PR-2).

Each capability package gets its own ``(id, embedding, metadata)``
namespace in the ``package_vectors`` table, isolated from every other
package by the ``(package_name, id)`` primary key and the FK
``ON DELETE CASCADE`` to ``installed_packages.name``.

Why a platform table (not per-package SQLite files)
---------------------------------------------------
Same reasoning as :mod:`carpenter.packages.state` (Phase 3a):
- Hash-pinned install trees are immutable by design; co-located
  vector files would either break the hash invariant or require an
  out-of-tree state dir per package, multiplying complexity.
- WAL contention with the main DB would be worse than a single
  shared table managed by the platform.
- Centralising the store lets uninstall cleanly wipe (cascade)
  without each package owning that logic.
- Per the plan's D6 trade-off: a single shared table is preferred
  over per-namespace SQLite tables (which would force dynamic DDL
  at install time and conflict with the migration system).

Isolation invariant (I9 — least privilege between packages)
-----------------------------------------------------------
A :class:`PackageVectorStore` is bound to a single ``package_name`` at
construction.  The ``_package_name`` is private — its underscore prefix
flags that callers MUST NOT introspect or rebind it after construction.
All methods operate exclusively on ``self._package_name``; there is
no API to read/write another package's namespace via a handle.  No
method on the handle accepts a ``package_name`` argument from the
caller, so even a "crafted id" attack cannot leak across namespaces:
every SQL filter binds ``WHERE package_name = ?`` with the handle's
bound value.  Combined with the SQL primary key and the FK cascade,
cross-package vector access is impossible by construction.

Model identity invariant (I11)
------------------------------
Per D7, a single namespace contains vectors from exactly one
``model_identity`` at a time.  Both :func:`upsert` and :func:`search`
enforce this: if the namespace already holds vectors whose
``model_identity`` differs from the value passed (or, for search, the
current embedding service's identity), the call raises
:class:`EmbeddingModelMismatchError`.  Callers recover by
``clear()``-ing the namespace and re-indexing.

Lifecycle (D9)
--------------
Vectors are derived data — they cascade-delete via the FK on
uninstall and are NOT archived.  This is the deliberate asymmetry
with :func:`carpenter.packages.state.archive_for_uninstall`: state
is irreplaceable (Gmail watermark, etc.) while vectors are
reproducible from source data.

Module-level functions take ``package_name`` explicitly and are the
low-level API used by the installer and tests.  Application code
(triggers / handlers) receives a :class:`PackageVectorStore` from
the loader and uses its bound methods.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..db import get_db
from ..embeddings.codec import (
    _cosine_similarity,
    _deserialize_embedding,
    _serialize_embedding,
)
from ..embeddings.service import EmbeddingModelMismatchError

if TYPE_CHECKING:
    from ..embeddings.service import EmbeddingService

logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_value(row: Any, key: str, idx: int) -> Any:
    """Read a column out of a tuple-or-Row result."""
    if isinstance(row, sqlite3.Row):
        return row[key]
    return row[idx]


def _existing_model_identity(
    conn: sqlite3.Connection, package_name: str,
) -> str | None:
    """Return the model_identity already present in *package_name*'s
    namespace, or ``None`` if the namespace is empty.

    Used by :func:`upsert` and :func:`search` to enforce I11 / D7.
    Reads exactly one row so the check is constant-time regardless of
    namespace size.
    """
    row = conn.execute(
        "SELECT model_identity FROM package_vectors "
        "WHERE package_name = ? LIMIT 1",
        (package_name,),
    ).fetchone()
    if row is None:
        return None
    return _row_value(row, "model_identity", 0)


def _filter_clause(filters: dict | None) -> tuple[str, list[Any]]:
    """Build the ``AND json_extract(metadata_json, '$.k') = ?`` tail.

    Exact-match only (D8).  Returns an empty clause + empty args when
    *filters* is falsy.  Keys are JSON-path escaped lightly by
    forbidding the only characters that would break the path syntax —
    callers are expected to pass simple metadata key names.
    """
    if not filters:
        return "", []
    parts: list[str] = []
    args: list[Any] = []
    for key, value in filters.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"filter key must be a non-empty string (got {key!r})")
        if "'" in key or '"' in key or "$" in key:
            raise ValueError(
                f"filter key contains illegal character: {key!r}",
            )
        parts.append(f"json_extract(metadata_json, '$.{key}') = ?")
        args.append(value)
    return " AND " + " AND ".join(parts), args


# ── module-level API (package_name as explicit arg) ──────────────────


def upsert(
    package_name: str,
    id: str,
    vector: list[float],
    model_identity: str,
    metadata: dict | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Insert or replace a vector under ``(package_name, id)``.

    Enforces I11: if *package_name*'s namespace already contains
    vectors with a different ``model_identity``, raises
    :class:`EmbeddingModelMismatchError`.  Callers can :func:`clear`
    the namespace to reset before re-indexing.
    """
    if not isinstance(id, str) or not id:
        raise ValueError("vector id must be a non-empty string")
    if not isinstance(model_identity, str) or not model_identity:
        raise ValueError("model_identity must be a non-empty string")
    if not vector:
        raise ValueError("vector must be a non-empty list of floats")

    payload = _serialize_embedding(list(vector))
    metadata_blob = json.dumps(metadata or {}, sort_keys=True)
    dim = len(vector)
    now = _now_iso()
    own_conn = conn is None
    db = conn if conn is not None else get_db()
    try:
        existing = _existing_model_identity(db, package_name)
        if existing is not None and existing != model_identity:
            raise EmbeddingModelMismatchError(
                f"Namespace {package_name!r} already contains vectors with "
                f"model_identity={existing!r}; refusing to upsert with "
                f"model_identity={model_identity!r}.  Clear the namespace "
                f"and re-index, or revert the embedding configuration.",
            )
        db.execute(
            "INSERT INTO package_vectors "
            "(package_name, id, embedding, model_identity, vector_dim, "
            " metadata_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(package_name, id) DO UPDATE SET "
            "  embedding = excluded.embedding, "
            "  model_identity = excluded.model_identity, "
            "  vector_dim = excluded.vector_dim, "
            "  metadata_json = excluded.metadata_json, "
            "  updated_at = excluded.updated_at",
            (
                package_name, id, payload, model_identity, dim,
                metadata_blob, now, now,
            ),
        )
        if own_conn:
            db.commit()
    finally:
        if own_conn:
            db.close()


def upsert_batch(
    package_name: str,
    items: list[tuple[str, list[float], dict | None]],
    model_identity: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Bulk variant of :func:`upsert`.

    All items share the same *model_identity*.  Returns the number of
    rows written.  Atomic: either every row is written or none are
    (the transaction rolls back on the first failure when this
    function owns the connection).
    """
    if not items:
        return 0
    if not isinstance(model_identity, str) or not model_identity:
        raise ValueError("model_identity must be a non-empty string")

    now = _now_iso()
    own_conn = conn is None
    db = conn if conn is not None else get_db()
    try:
        existing = _existing_model_identity(db, package_name)
        if existing is not None and existing != model_identity:
            raise EmbeddingModelMismatchError(
                f"Namespace {package_name!r} already contains vectors with "
                f"model_identity={existing!r}; refusing to upsert batch "
                f"with model_identity={model_identity!r}.",
            )
        written = 0
        for id_, vector, metadata in items:
            if not isinstance(id_, str) or not id_:
                raise ValueError("vector id must be a non-empty string")
            if not vector:
                raise ValueError(
                    f"vector for id={id_!r} must be a non-empty list",
                )
            payload = _serialize_embedding(list(vector))
            metadata_blob = json.dumps(metadata or {}, sort_keys=True)
            dim = len(vector)
            db.execute(
                "INSERT INTO package_vectors "
                "(package_name, id, embedding, model_identity, vector_dim, "
                " metadata_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(package_name, id) DO UPDATE SET "
                "  embedding = excluded.embedding, "
                "  model_identity = excluded.model_identity, "
                "  vector_dim = excluded.vector_dim, "
                "  metadata_json = excluded.metadata_json, "
                "  updated_at = excluded.updated_at",
                (
                    package_name, id_, payload, model_identity, dim,
                    metadata_blob, now, now,
                ),
            )
            written += 1
        if own_conn:
            db.commit()
        return written
    finally:
        if own_conn:
            db.close()


def get(
    package_name: str,
    id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[list[float], dict] | None:
    """Return ``(vector, metadata)`` for ``(package_name, id)``, or None."""
    own_conn = conn is None
    db = conn if conn is not None else get_db()
    try:
        row = db.execute(
            "SELECT embedding, vector_dim, metadata_json FROM package_vectors "
            "WHERE package_name = ? AND id = ?",
            (package_name, id),
        ).fetchone()
        if row is None:
            return None
        blob = _row_value(row, "embedding", 0)
        dim = int(_row_value(row, "vector_dim", 1))
        metadata_json = _row_value(row, "metadata_json", 2)
        vector = list(_deserialize_embedding(blob, dim))
        metadata = json.loads(metadata_json) if metadata_json else {}
        return vector, metadata
    finally:
        if own_conn:
            db.close()


def delete(
    package_name: str,
    id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Delete ``(package_name, id)``.  Returns True if a row was removed."""
    own_conn = conn is None
    db = conn if conn is not None else get_db()
    try:
        cur = db.execute(
            "DELETE FROM package_vectors "
            "WHERE package_name = ? AND id = ?",
            (package_name, id),
        )
        if own_conn:
            db.commit()
        return cur.rowcount > 0
    finally:
        if own_conn:
            db.close()


def search(
    package_name: str,
    query_vector: list[float],
    top_k: int = 10,
    filters: dict | None = None,
    *,
    model_identity: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[tuple[str, float, dict]]:
    """Cosine-similarity search within *package_name*'s namespace.

    Returns up to *top_k* ``(id, score, metadata)`` tuples sorted by
    descending cosine similarity.  Per D7: if *model_identity* is
    provided and differs from the namespace's existing identity (or
    if the dim doesn't match the query vector's), raises
    :class:`EmbeddingModelMismatchError`.

    *filters* are exact-match metadata key/value pairs (D8 v1).
    Implemented via ``json_extract(metadata_json, '$.key') = ?``.
    Cosine is computed in pure Python — for huge namespaces consider
    upgrading to a numpy-batched matmul (see plan's D6 trade-off).
    """
    if top_k <= 0:
        return []
    if not query_vector:
        raise ValueError("query_vector must be a non-empty list of floats")
    query_dim = len(query_vector)
    own_conn = conn is None
    db = conn if conn is not None else get_db()
    try:
        existing = _existing_model_identity(db, package_name)
        if existing is None:
            return []
        if model_identity is not None and existing != model_identity:
            raise EmbeddingModelMismatchError(
                f"Namespace {package_name!r} contains vectors with "
                f"model_identity={existing!r}; cannot search with "
                f"model_identity={model_identity!r}.",
            )

        filter_sql, filter_args = _filter_clause(filters)
        rows = db.execute(
            f"SELECT id, embedding, vector_dim, metadata_json "
            f"FROM package_vectors "
            f"WHERE package_name = ?{filter_sql}",
            [package_name, *filter_args],
        ).fetchall()

        query_tuple = tuple(query_vector)
        scored: list[tuple[str, float, dict]] = []
        for row in rows:
            id_ = _row_value(row, "id", 0)
            blob = _row_value(row, "embedding", 1)
            dim = int(_row_value(row, "vector_dim", 2))
            metadata_json = _row_value(row, "metadata_json", 3)
            if dim != query_dim:
                raise EmbeddingModelMismatchError(
                    f"Vector dim mismatch in namespace {package_name!r}: "
                    f"stored dim={dim}, query dim={query_dim} (id={id_!r}).",
                )
            stored = _deserialize_embedding(blob, dim)
            score = _cosine_similarity(query_tuple, stored)
            metadata = json.loads(metadata_json) if metadata_json else {}
            scored.append((id_, score, metadata))

        scored.sort(key=lambda triple: triple[1], reverse=True)
        return scored[:top_k]
    finally:
        if own_conn:
            db.close()


def count(
    package_name: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Return the number of vectors in *package_name*'s namespace."""
    own_conn = conn is None
    db = conn if conn is not None else get_db()
    try:
        row = db.execute(
            "SELECT COUNT(*) FROM package_vectors WHERE package_name = ?",
            (package_name,),
        ).fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        if own_conn:
            db.close()


def clear(
    package_name: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Wipe every row in *package_name*'s namespace.

    Returns the number of rows removed.  This is the recovery path
    from :class:`EmbeddingModelMismatchError`: a package owning a
    rebuild-index arc would ``clear()`` then re-upsert under the new
    model.
    """
    own_conn = conn is None
    db = conn if conn is not None else get_db()
    try:
        cur = db.execute(
            "DELETE FROM package_vectors WHERE package_name = ?",
            (package_name,),
        )
        if own_conn:
            db.commit()
        return int(cur.rowcount)
    finally:
        if own_conn:
            db.close()


def list_ids(
    package_name: str,
    limit: int = 1000,
    offset: int = 0,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Return up to *limit* ids in *package_name*'s namespace (sorted)."""
    if limit <= 0:
        return []
    own_conn = conn is None
    db = conn if conn is not None else get_db()
    try:
        rows = db.execute(
            "SELECT id FROM package_vectors "
            "WHERE package_name = ? ORDER BY id LIMIT ? OFFSET ?",
            (package_name, int(limit), int(max(0, offset))),
        ).fetchall()
        if not rows:
            return []
        if isinstance(rows[0], sqlite3.Row):
            return [r["id"] for r in rows]
        return [r[0] for r in rows]
    finally:
        if own_conn:
            db.close()


# ── PackageVectorStore ───────────────────────────────────────────────


class PackageVectorStore:
    """A handle bound to a single package's vector namespace.

    Instances are created by the platform (installer / loader) and
    handed to package code (triggers, handlers).  The bound
    ``package_name`` is private — package code MUST NOT introspect or
    rebind it.  All methods route to the module-level functions with
    the bound name, so cross-package vector access via a handle is
    structurally impossible.

    No method on this class accepts a ``package_name`` argument from
    the caller — that is the I9 isolation invariant.  Crafted ids
    cannot leak across namespaces because every SQL filter binds
    ``WHERE package_name = ?`` with ``self._package_name``.

    Usage::

        handle = PackageVectorStore("my-package")
        handle.embed_and_upsert("msg-123", "subject + snippet text")
        hits = handle.embed_and_search("query text", top_k=5)
    """

    __slots__ = ("_package_name", "_service")

    def __init__(
        self,
        package_name: str,
        service: "EmbeddingService | None" = None,
    ) -> None:
        if not isinstance(package_name, str) or not package_name.strip():
            raise ValueError(
                "PackageVectorStore requires a non-empty package_name "
                f"(got {package_name!r})",
            )
        # Bound at construction; methods only ever use this value.
        self._package_name = package_name
        # The embedding service is resolved lazily so handles can be
        # constructed before ``get_embedding_service`` is callable
        # (e.g. in test contexts that bypass the daemon entrypoint).
        self._service: "EmbeddingService | None" = service

    @property
    def package_name(self) -> str:
        """Read-only accessor for the bound package name (logging only)."""
        return self._package_name

    # ── raw vector API ────────────────────────────────────────────────

    def upsert(
        self,
        id: str,
        vector: list[float],
        model_identity: str,
        metadata: dict | None = None,
    ) -> None:
        upsert(
            self._package_name, id, vector, model_identity, metadata,
        )

    def upsert_batch(
        self,
        items: list[tuple[str, list[float], dict | None]],
        model_identity: str,
    ) -> int:
        return upsert_batch(self._package_name, items, model_identity)

    def get(self, id: str) -> tuple[list[float], dict] | None:
        return get(self._package_name, id)

    def delete(self, id: str) -> bool:
        return delete(self._package_name, id)

    def search(
        self,
        query_vector: list[float],
        top_k: int = 10,
        filters: dict | None = None,
        *,
        model_identity: str | None = None,
    ) -> list[tuple[str, float, dict]]:
        return search(
            self._package_name,
            query_vector,
            top_k=top_k,
            filters=filters,
            model_identity=model_identity,
        )

    def count(self) -> int:
        return count(self._package_name)

    def clear(self) -> int:
        return clear(self._package_name)

    def list_ids(self, limit: int = 1000, offset: int = 0) -> list[str]:
        return list_ids(self._package_name, limit=limit, offset=offset)

    # ── convenience: embed-then-upsert / embed-then-search ────────────

    def _resolve_service(self) -> "EmbeddingService":
        """Lazily obtain the embedding service singleton."""
        if self._service is not None:
            return self._service
        from ..embeddings.service import get_embedding_service
        self._service = get_embedding_service()
        return self._service

    def embed_and_upsert(
        self,
        id: str,
        text: str,
        metadata: dict | None = None,
    ) -> None:
        """Embed *text* with the bound service and upsert under *id*.

        Uses the embedding service's current ``model_identity`` so a
        namespace's invariant is automatically maintained as long as
        the configured model does not change between calls.  If it
        does, :class:`EmbeddingModelMismatchError` surfaces here.
        """
        service = self._resolve_service()
        vectors = service.embed([text])
        if not vectors:
            raise RuntimeError(
                "Embedding service returned no vectors for non-empty input",
            )
        self.upsert(id, vectors[0], service.model_identity, metadata)

    def embed_and_search(
        self,
        query_text: str,
        top_k: int = 10,
        filters: dict | None = None,
    ) -> list[tuple[str, float, dict]]:
        """Embed *query_text* and run :func:`search`."""
        service = self._resolve_service()
        vectors = service.embed([query_text])
        if not vectors:
            return []
        return self.search(
            vectors[0],
            top_k=top_k,
            filters=filters,
            model_identity=service.model_identity,
        )

    def __repr__(self) -> str:
        return f"PackageVectorStore(package_name={self._package_name!r})"


__all__ = [
    "PackageVectorStore",
    "EmbeddingModelMismatchError",
    "upsert",
    "upsert_batch",
    "get",
    "delete",
    "search",
    "count",
    "clear",
    "list_ids",
]
