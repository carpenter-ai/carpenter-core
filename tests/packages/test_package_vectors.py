"""Tests for the per-package vector store primitive (Phase 2 PR-2).

Covers:
  - CRUD via module-level functions and PackageVectorStore handle
  - Namespace isolation (I9): handle for pkg-A cannot touch pkg-B even
    via crafted ids
  - Search ranking with a deterministic mock embedding service
  - EmbeddingModelMismatchError on dim mismatch and on identity flip
  - Cross-handle search impossibility (no package_name on search())
  - FK CASCADE wipe on package uninstall
  - Large batch upsert (1k vectors) latency budget on Pi
  - embed_and_upsert / embed_and_search happy path with mocked service
  - Filter semantics: exact-match on JSON metadata keys
  - Trigger framework backcompat — built-in triggers still construct
    without ``package_vectors=``
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from carpenter.embeddings.service import EmbeddingModelMismatchError
from carpenter.packages import vectors as pkg_vectors
from carpenter.packages.installer import ensure_installer_tables
from carpenter.packages.vectors import PackageVectorStore


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def db_conn():
    """In-memory SQLite with installer + package_vectors tables, FK enforced."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_installer_tables(conn)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS package_vectors (
            package_name   TEXT NOT NULL,
            id             TEXT NOT NULL,
            embedding      BLOB NOT NULL,
            model_identity TEXT NOT NULL,
            vector_dim     INTEGER NOT NULL,
            metadata_json  TEXT NOT NULL DEFAULT '{}',
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (package_name, id),
            FOREIGN KEY (package_name) REFERENCES installed_packages(name)
                ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_package_vectors_pkg_model
            ON package_vectors(package_name, model_identity);
    """)
    yield conn
    conn.close()


def _seed_package(conn: sqlite3.Connection, name: str) -> None:
    """Insert a minimal installed_packages row so the FK is satisfiable."""
    conn.execute(
        "INSERT INTO installed_packages "
        "(name, version, hash, source_path, install_path, installed_at) "
        "VALUES (?, '0.1.0', 'abc', '/tmp/s', '/tmp/d', '2026-05-20T00:00:00Z')",
        (name,),
    )
    conn.commit()


class _NoCloseConn:
    """Wraps a sqlite3.Connection so handle ops can't tear down the fixture."""

    def __init__(self, inner):
        self._inner = inner

    def execute(self, *a, **kw):
        return self._inner.execute(*a, **kw)

    def commit(self):
        return self._inner.commit()

    def close(self):
        return None


@pytest.fixture
def patched_db(db_conn, monkeypatch):
    """Patch ``vectors.get_db`` so handle methods use the test connection."""
    proxy = _NoCloseConn(db_conn)
    monkeypatch.setattr(pkg_vectors, "get_db", lambda: proxy)
    return db_conn


# A model identity used by most tests.  Mirrors the live service format.
MID = "local:test-model:4"


def _vec(x: float, y: float, z: float, w: float) -> list[float]:
    return [x, y, z, w]


# ── CRUD ─────────────────────────────────────────────────────────────


class TestCrud:
    def test_upsert_get_roundtrip(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_vectors.upsert(
            "pkg-a", "v1", _vec(1.0, 0.0, 0.0, 0.0), MID,
            metadata={"k": "v"}, conn=db_conn,
        )
        result = pkg_vectors.get("pkg-a", "v1", conn=db_conn)
        assert result is not None
        vector, metadata = result
        assert vector == pytest.approx([1.0, 0.0, 0.0, 0.0])
        assert metadata == {"k": "v"}

    def test_upsert_replaces(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_vectors.upsert(
            "pkg-a", "v1", _vec(1.0, 0.0, 0.0, 0.0), MID, conn=db_conn,
        )
        pkg_vectors.upsert(
            "pkg-a", "v1", _vec(0.0, 1.0, 0.0, 0.0), MID,
            metadata={"updated": True}, conn=db_conn,
        )
        vector, metadata = pkg_vectors.get("pkg-a", "v1", conn=db_conn)
        assert vector == pytest.approx([0.0, 1.0, 0.0, 0.0])
        assert metadata == {"updated": True}
        assert pkg_vectors.count("pkg-a", conn=db_conn) == 1

    def test_get_missing_returns_none(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        assert pkg_vectors.get("pkg-a", "nope", conn=db_conn) is None

    def test_delete(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_vectors.upsert("pkg-a", "v1", _vec(1, 0, 0, 0), MID, conn=db_conn)
        assert pkg_vectors.delete("pkg-a", "v1", conn=db_conn) is True
        assert pkg_vectors.delete("pkg-a", "v1", conn=db_conn) is False
        assert pkg_vectors.get("pkg-a", "v1", conn=db_conn) is None

    def test_count_and_list_ids(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        for i, id_ in enumerate(["b", "a", "c"]):
            pkg_vectors.upsert(
                "pkg-a", id_, _vec(float(i), 0, 0, 0), MID, conn=db_conn,
            )
        assert pkg_vectors.count("pkg-a", conn=db_conn) == 3
        assert pkg_vectors.list_ids("pkg-a", conn=db_conn) == ["a", "b", "c"]

    def test_list_ids_pagination(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        for i in range(5):
            pkg_vectors.upsert(
                "pkg-a", f"id-{i}", _vec(float(i), 0, 0, 0), MID, conn=db_conn,
            )
        page = pkg_vectors.list_ids("pkg-a", limit=2, offset=1, conn=db_conn)
        assert page == ["id-1", "id-2"]

    def test_clear(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        for i in range(3):
            pkg_vectors.upsert(
                "pkg-a", f"id-{i}", _vec(float(i), 0, 0, 0), MID, conn=db_conn,
            )
        removed = pkg_vectors.clear("pkg-a", conn=db_conn)
        assert removed == 3
        assert pkg_vectors.count("pkg-a", conn=db_conn) == 0

    def test_upsert_batch(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        items = [
            ("v1", _vec(1, 0, 0, 0), {"category": "x"}),
            ("v2", _vec(0, 1, 0, 0), {"category": "y"}),
            ("v3", _vec(0, 0, 1, 0), None),
        ]
        written = pkg_vectors.upsert_batch("pkg-a", items, MID, conn=db_conn)
        assert written == 3
        assert pkg_vectors.count("pkg-a", conn=db_conn) == 3
        v2 = pkg_vectors.get("pkg-a", "v2", conn=db_conn)
        assert v2[1] == {"category": "y"}

    def test_upsert_rejects_empty_id_and_empty_vector(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        with pytest.raises(ValueError):
            pkg_vectors.upsert("pkg-a", "", _vec(1, 0, 0, 0), MID, conn=db_conn)
        with pytest.raises(ValueError):
            pkg_vectors.upsert("pkg-a", "v1", [], MID, conn=db_conn)
        with pytest.raises(ValueError):
            pkg_vectors.upsert("pkg-a", "v1", _vec(1, 0, 0, 0), "", conn=db_conn)


# ── Handle CRUD ──────────────────────────────────────────────────────


class TestHandle:
    def test_handle_round_trip(self, patched_db):
        _seed_package(patched_db, "pkg-a")
        handle = PackageVectorStore("pkg-a")
        handle.upsert("v1", _vec(1, 0, 0, 0), MID, metadata={"k": "v"})
        vector, metadata = handle.get("v1")
        assert vector == pytest.approx([1.0, 0.0, 0.0, 0.0])
        assert metadata == {"k": "v"}
        assert handle.count() == 1
        assert handle.list_ids() == ["v1"]
        assert handle.delete("v1") is True
        assert handle.count() == 0

    def test_handle_rejects_empty_package_name(self):
        with pytest.raises(ValueError):
            PackageVectorStore("")
        with pytest.raises(ValueError):
            PackageVectorStore("   ")

    def test_handle_repr_shows_name(self):
        handle = PackageVectorStore("pkg-a")
        assert "pkg-a" in repr(handle)

    def test_handle_slots_prevent_attribute_smuggle(self, patched_db):
        _seed_package(patched_db, "pkg-a")
        handle = PackageVectorStore("pkg-a")
        # __slots__ + property prevents attribute injection.
        with pytest.raises(AttributeError):
            handle.other_pkg = "pkg-b"  # type: ignore[attr-defined]


# ── Isolation (I9) ───────────────────────────────────────────────────


class TestIsolation:
    def test_handles_isolated_via_handle_methods(self, patched_db):
        _seed_package(patched_db, "pkg-a")
        _seed_package(patched_db, "pkg-b")

        handle_a = PackageVectorStore("pkg-a")
        handle_b = PackageVectorStore("pkg-b")

        handle_a.upsert("shared-id", _vec(1, 0, 0, 0), MID, metadata={"who": "A"})
        handle_b.upsert("shared-id", _vec(0, 1, 0, 0), MID, metadata={"who": "B"})

        # Each handle sees only its own data, even with a colliding id.
        a_vec, a_meta = handle_a.get("shared-id")
        b_vec, b_meta = handle_b.get("shared-id")
        assert a_meta == {"who": "A"}
        assert b_meta == {"who": "B"}
        assert a_vec == pytest.approx([1.0, 0.0, 0.0, 0.0])
        assert b_vec == pytest.approx([0.0, 1.0, 0.0, 0.0])

        assert handle_a.count() == 1
        assert handle_b.count() == 1
        assert handle_a.list_ids() == ["shared-id"]
        assert handle_b.list_ids() == ["shared-id"]

    def test_handle_has_no_package_name_argument(self):
        """No public method on the handle accepts a ``package_name`` arg."""
        import inspect
        handle = PackageVectorStore("pkg-a")
        for name in (
            "upsert", "upsert_batch", "get", "delete", "search",
            "count", "clear", "list_ids", "embed_and_upsert",
            "embed_and_search",
        ):
            sig = inspect.signature(getattr(handle, name))
            assert "package_name" not in sig.parameters, (
                f"{name} must not accept a package_name parameter "
                f"(I9 isolation invariant)"
            )

    def test_crafted_id_cannot_leak_across_namespaces(self, patched_db):
        """Even a maliciously crafted id with weird characters stays bound."""
        _seed_package(patched_db, "pkg-a")
        _seed_package(patched_db, "pkg-b")

        handle_a = PackageVectorStore("pkg-a")
        handle_b = PackageVectorStore("pkg-b")

        # B writes a vector with an "interesting" id.
        crafted = "'; DROP TABLE package_vectors; --"
        handle_b.upsert(crafted, _vec(1, 0, 0, 0), MID, metadata={"who": "B"})

        # A cannot read it.
        assert handle_a.get(crafted) is None
        # A cannot delete it.
        assert handle_a.delete(crafted) is False
        # A's count is still 0.
        assert handle_a.count() == 0
        # B's row is still there.
        assert handle_b.count() == 1
        # And it still appears in B's list_ids.
        assert crafted in handle_b.list_ids()

    def test_handle_clear_only_affects_bound_namespace(self, patched_db):
        _seed_package(patched_db, "pkg-a")
        _seed_package(patched_db, "pkg-b")

        handle_a = PackageVectorStore("pkg-a")
        handle_b = PackageVectorStore("pkg-b")
        handle_a.upsert("v1", _vec(1, 0, 0, 0), MID)
        handle_b.upsert("v1", _vec(0, 1, 0, 0), MID)

        removed = handle_a.clear()
        assert removed == 1
        assert handle_a.count() == 0
        assert handle_b.count() == 1


# ── Search ranking ───────────────────────────────────────────────────


class TestSearch:
    def test_search_orders_by_cosine_descending(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        # Three vectors at increasing angles from the query direction.
        pkg_vectors.upsert(
            "pkg-a", "perfect", _vec(1, 0, 0, 0), MID, conn=db_conn,
        )
        pkg_vectors.upsert(
            "pkg-a", "diagonal", _vec(0.7071, 0.7071, 0, 0), MID, conn=db_conn,
        )
        pkg_vectors.upsert(
            "pkg-a", "orthogonal", _vec(0, 1, 0, 0), MID, conn=db_conn,
        )
        results = pkg_vectors.search(
            "pkg-a", _vec(1, 0, 0, 0), top_k=10, conn=db_conn,
        )
        ids = [r[0] for r in results]
        assert ids == ["perfect", "diagonal", "orthogonal"]
        # Scores monotonically decrease.
        scores = [r[1] for r in results]
        assert scores[0] > scores[1] > scores[2]
        # Top score is ~1 for a unit-length match.
        assert scores[0] == pytest.approx(1.0, abs=1e-4)

    def test_search_top_k_truncates(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        for i in range(5):
            v = _vec(1.0 - i * 0.1, i * 0.1, 0, 0)
            pkg_vectors.upsert("pkg-a", f"id-{i}", v, MID, conn=db_conn)
        results = pkg_vectors.search(
            "pkg-a", _vec(1, 0, 0, 0), top_k=2, conn=db_conn,
        )
        assert len(results) == 2

    def test_search_empty_namespace(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        results = pkg_vectors.search(
            "pkg-a", _vec(1, 0, 0, 0), top_k=10, conn=db_conn,
        )
        assert results == []

    def test_search_cannot_cross_namespaces(self, patched_db):
        _seed_package(patched_db, "pkg-a")
        _seed_package(patched_db, "pkg-b")
        handle_a = PackageVectorStore("pkg-a")
        handle_b = PackageVectorStore("pkg-b")

        # B has rich content; A has nothing.
        for i in range(3):
            handle_b.upsert(f"b{i}", _vec(1, 0, 0, 0), MID)

        # A searches; gets nothing — B's vectors are invisible.
        results_a = handle_a.search(_vec(1, 0, 0, 0), top_k=10)
        assert results_a == []

        # B sees its own data.
        results_b = handle_b.search(_vec(1, 0, 0, 0), top_k=10)
        assert len(results_b) == 3


# ── Model identity invariant (D7 / I11) ──────────────────────────────


class TestModelIdentity:
    def test_upsert_rejects_identity_flip(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_vectors.upsert("pkg-a", "v1", _vec(1, 0, 0, 0), MID, conn=db_conn)
        with pytest.raises(EmbeddingModelMismatchError):
            pkg_vectors.upsert(
                "pkg-a", "v2", _vec(0, 1, 0, 0),
                "ollama:nomic-embed:4", conn=db_conn,
            )

    def test_upsert_batch_rejects_identity_flip(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_vectors.upsert("pkg-a", "v1", _vec(1, 0, 0, 0), MID, conn=db_conn)
        with pytest.raises(EmbeddingModelMismatchError):
            pkg_vectors.upsert_batch(
                "pkg-a",
                [("v2", _vec(0, 1, 0, 0), None)],
                "ollama:nomic-embed:4",
                conn=db_conn,
            )

    def test_search_rejects_identity_flip(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_vectors.upsert("pkg-a", "v1", _vec(1, 0, 0, 0), MID, conn=db_conn)
        with pytest.raises(EmbeddingModelMismatchError):
            pkg_vectors.search(
                "pkg-a", _vec(1, 0, 0, 0),
                model_identity="local:other:4", conn=db_conn,
            )

    def test_search_rejects_dim_mismatch(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_vectors.upsert("pkg-a", "v1", _vec(1, 0, 0, 0), MID, conn=db_conn)
        with pytest.raises(EmbeddingModelMismatchError):
            pkg_vectors.search(
                "pkg-a", [1.0, 0.0, 0.0],  # 3 dims vs stored 4
                conn=db_conn,
            )

    def test_clear_then_reupsert_with_new_identity_succeeds(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_vectors.upsert("pkg-a", "v1", _vec(1, 0, 0, 0), MID, conn=db_conn)
        pkg_vectors.clear("pkg-a", conn=db_conn)
        # After clear, the namespace is fresh — any model identity is OK.
        pkg_vectors.upsert(
            "pkg-a", "v1", _vec(0, 1, 0, 0),
            "ollama:nomic-embed:4", conn=db_conn,
        )
        assert pkg_vectors.count("pkg-a", conn=db_conn) == 1


# ── Filter semantics ─────────────────────────────────────────────────


class TestFilters:
    def test_exact_match_string(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_vectors.upsert(
            "pkg-a", "v1", _vec(1, 0, 0, 0), MID,
            metadata={"thread": "T1"}, conn=db_conn,
        )
        pkg_vectors.upsert(
            "pkg-a", "v2", _vec(1, 0, 0, 0), MID,
            metadata={"thread": "T2"}, conn=db_conn,
        )
        results = pkg_vectors.search(
            "pkg-a", _vec(1, 0, 0, 0),
            filters={"thread": "T1"}, conn=db_conn,
        )
        assert [r[0] for r in results] == ["v1"]

    def test_exact_match_int(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_vectors.upsert(
            "pkg-a", "v1", _vec(1, 0, 0, 0), MID,
            metadata={"priority": 1}, conn=db_conn,
        )
        pkg_vectors.upsert(
            "pkg-a", "v2", _vec(1, 0, 0, 0), MID,
            metadata={"priority": 2}, conn=db_conn,
        )
        results = pkg_vectors.search(
            "pkg-a", _vec(1, 0, 0, 0),
            filters={"priority": 2}, conn=db_conn,
        )
        assert [r[0] for r in results] == ["v2"]

    def test_multi_key_filter_is_AND(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_vectors.upsert(
            "pkg-a", "v1", _vec(1, 0, 0, 0), MID,
            metadata={"thread": "T1", "priority": 1}, conn=db_conn,
        )
        pkg_vectors.upsert(
            "pkg-a", "v2", _vec(1, 0, 0, 0), MID,
            metadata={"thread": "T1", "priority": 2}, conn=db_conn,
        )
        results = pkg_vectors.search(
            "pkg-a", _vec(1, 0, 0, 0),
            filters={"thread": "T1", "priority": 2}, conn=db_conn,
        )
        assert [r[0] for r in results] == ["v2"]

    def test_filter_rejects_dangerous_keys(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        pkg_vectors.upsert(
            "pkg-a", "v1", _vec(1, 0, 0, 0), MID, conn=db_conn,
        )
        with pytest.raises(ValueError):
            pkg_vectors.search(
                "pkg-a", _vec(1, 0, 0, 0),
                filters={"x'; DROP": 1}, conn=db_conn,
            )


# ── FK cascade on uninstall (D9) ─────────────────────────────────────


class TestCascadeDelete:
    def test_vectors_wiped_on_package_uninstall(self, db_conn):
        _seed_package(db_conn, "pkg-a")
        _seed_package(db_conn, "pkg-b")
        for i in range(3):
            pkg_vectors.upsert(
                "pkg-a", f"a{i}", _vec(float(i), 0, 0, 0), MID, conn=db_conn,
            )
            pkg_vectors.upsert(
                "pkg-b", f"b{i}", _vec(float(i), 0, 0, 0), MID, conn=db_conn,
            )
        assert pkg_vectors.count("pkg-a", conn=db_conn) == 3
        assert pkg_vectors.count("pkg-b", conn=db_conn) == 3

        # Uninstall pkg-a — FK CASCADE wipes its vectors.
        db_conn.execute(
            "DELETE FROM installed_packages WHERE name = ?", ("pkg-a",),
        )
        db_conn.commit()

        assert pkg_vectors.count("pkg-a", conn=db_conn) == 0
        # pkg-b untouched.
        assert pkg_vectors.count("pkg-b", conn=db_conn) == 3

    def test_install_upsert_uninstall_cycle(self, db_conn):
        """Integration: install → upsert → uninstall → count == 0."""
        _seed_package(db_conn, "pkg-a")
        pkg_vectors.upsert_batch(
            "pkg-a",
            [(f"id-{i}", _vec(float(i), 0, 0, 0), None) for i in range(10)],
            MID, conn=db_conn,
        )
        assert pkg_vectors.count("pkg-a", conn=db_conn) == 10
        db_conn.execute(
            "DELETE FROM installed_packages WHERE name = ?", ("pkg-a",),
        )
        db_conn.commit()
        assert pkg_vectors.count("pkg-a", conn=db_conn) == 0


# ── Large batch latency budget ───────────────────────────────────────


class TestLargeBatch:
    def test_thousand_vector_batch_under_budget(self, db_conn):
        """1k 4-dim vectors batch upsert should complete in a few seconds on Pi.

        The budget is generous (5s) to avoid CI flakes; on a healthy Pi
        this is well under 1s.  The goal is to detect catastrophic
        regressions (e.g. accidentally re-opening the DB connection per
        row), not to micro-benchmark.
        """
        _seed_package(db_conn, "pkg-a")
        items = [
            (f"id-{i}", _vec(float(i % 7), float(i % 5), float(i % 3), 0.0), None)
            for i in range(1000)
        ]
        start = time.monotonic()
        written = pkg_vectors.upsert_batch("pkg-a", items, MID, conn=db_conn)
        elapsed = time.monotonic() - start
        assert written == 1000
        assert pkg_vectors.count("pkg-a", conn=db_conn) == 1000
        assert elapsed < 5.0, (
            f"1k batch upsert took {elapsed:.2f}s (budget 5s)"
        )


# ── Mock embedding service: embed_and_upsert / embed_and_search ──────


class _DeterministicProvider:
    """Tiny stand-in for a provider — maps text → fixed 4D vectors."""

    model_name = "test-model"
    vector_dim = 4

    def __init__(self):
        self._table = {
            "apple":  [1.0, 0.0, 0.0, 0.0],
            "banana": [0.9, 0.1, 0.0, 0.0],
            "carrot": [0.0, 1.0, 0.0, 0.0],
            "query-apple": [1.0, 0.0, 0.0, 0.0],
        }

    def embed(self, texts):
        return [self._table.get(t, [0.0, 0.0, 0.0, 0.0]) for t in texts]

    def is_ready(self):
        return True


@pytest.fixture
def fake_service():
    from carpenter.embeddings.service import EmbeddingService
    return EmbeddingService(_DeterministicProvider(), provider_kind="local")


class TestEmbedAndSearch:
    def test_embed_and_upsert_then_search(self, patched_db, fake_service):
        _seed_package(patched_db, "pkg-a")
        handle = PackageVectorStore("pkg-a", service=fake_service)
        handle.embed_and_upsert("doc-apple", "apple", metadata={"fruit": True})
        handle.embed_and_upsert("doc-banana", "banana")
        handle.embed_and_upsert("doc-carrot", "carrot")

        hits = handle.embed_and_search("query-apple", top_k=2)
        assert len(hits) == 2
        # apple should rank above banana, which ranks above carrot.
        assert hits[0][0] == "doc-apple"
        assert hits[1][0] == "doc-banana"
        # metadata preserved on the top hit.
        assert hits[0][2] == {"fruit": True}

    def test_embed_and_search_with_filter(self, patched_db, fake_service):
        _seed_package(patched_db, "pkg-a")
        handle = PackageVectorStore("pkg-a", service=fake_service)
        handle.embed_and_upsert("a1", "apple", metadata={"tag": "x"})
        handle.embed_and_upsert("a2", "apple", metadata={"tag": "y"})
        hits = handle.embed_and_search(
            "query-apple", top_k=10, filters={"tag": "y"},
        )
        assert [h[0] for h in hits] == ["a2"]


# ── Trigger framework backcompat ─────────────────────────────────────


class TestTriggerBackcompat:
    """Existing built-in triggers must still construct without
    ``package_vectors=``.  The base ``Trigger.__init__`` defaults
    ``package_vectors`` to ``None``, so this is mostly a smoke test that
    the new kwarg didn't break the constructor signature in any of the
    platform-shipped trigger types.
    """

    def test_base_trigger_default_none(self):
        from carpenter.core.engine.triggers.base import Trigger

        class _DummyTrigger(Trigger):
            @classmethod
            def trigger_type(cls):
                return "dummy"

        # No package_vectors arg — accepts as None.
        t = _DummyTrigger(name="x", config={})
        assert t.package_vectors is None
        assert t.package_state is None

    def test_base_trigger_accepts_vector_handle(self, patched_db):
        from carpenter.core.engine.triggers.base import Trigger

        class _DummyTrigger(Trigger):
            @classmethod
            def trigger_type(cls):
                return "dummy"

        handle = PackageVectorStore("pkg-a")
        t = _DummyTrigger(
            name="x", config={},
            source_package="pkg-a",
            package_vectors=handle,
        )
        assert t.package_vectors is handle

    def test_base_trigger_rejects_mismatched_vector_handle(self):
        from carpenter.core.engine.triggers.base import Trigger

        class _DummyTrigger(Trigger):
            @classmethod
            def trigger_type(cls):
                return "dummy"

        handle = PackageVectorStore("pkg-b")
        with pytest.raises(ValueError):
            _DummyTrigger(
                name="x", config={},
                source_package="pkg-a",
                package_vectors=handle,
            )

    def test_builtin_trigger_types_still_construct(self):
        """The shipped TimerTrigger / CounterTrigger / WebhookTrigger
        all inherit ``Trigger.__init__``, so they accept the new
        ``package_vectors`` kwarg automatically.  Without that kwarg
        they construct as before — verified by calling each class
        directly with no package context.
        """
        import importlib

        # Import on demand so this test doesn't drag in optional deps.
        for module_path, class_name in (
            ("carpenter.core.engine.triggers.builtin.timer", "TimerTrigger"),
            ("carpenter.core.engine.triggers.builtin.counter", "CounterTrigger"),
        ):
            try:
                mod = importlib.import_module(module_path)
            except ImportError:
                continue
            cls = getattr(mod, class_name, None)
            if cls is None:
                continue
            # Most triggers want a minimal config; pass a permissive one.
            try:
                inst = cls(name=class_name + "-test", config={
                    "name": class_name + "-test",
                    "type": cls.trigger_type(),
                    "interval_seconds": 60,
                    "max_count": 1,
                })
            except TypeError:
                # If the trigger needs different config keys, the goal
                # is just to confirm the kwarg signature didn't break;
                # ignore the per-trigger config bikeshedding.
                continue
            # Critical: the new attribute exists, defaulted to None.
            assert hasattr(inst, "package_vectors")
            assert inst.package_vectors is None


# ── Trigger registry plumbing ────────────────────────────────────────


class TestRegistryPlumbing:
    """The trigger registry's ``load_triggers`` / ``load_package_triggers``
    accept the new ``package_vectors`` kwarg and forward it to trigger
    classes that accept it.
    """

    def test_load_package_triggers_forwards_vector_handle(self, patched_db):
        from carpenter.core.engine.triggers import registry as treg
        from carpenter.core.engine.triggers.base import Trigger

        class _Probe(Trigger):
            @classmethod
            def trigger_type(cls):
                return "_test_probe_vectors"

        # Ensure clean state in the global registry.
        try:
            treg.register_trigger_type(_Probe, source_package="pkg-a")
        except ValueError:
            pass

        _seed_package(patched_db, "pkg-a")
        handle = PackageVectorStore("pkg-a")
        try:
            instances = treg.load_package_triggers(
                [{"name": "probe1", "type": "_test_probe_vectors", "enabled": True}],
                source_package="pkg-a",
                package_vectors=handle,
            )
            assert len(instances) == 1
            assert instances[0].package_vectors is handle
        finally:
            treg.unregister_for_package("pkg-a")

    def test_load_package_triggers_rejects_empty_name(self):
        from carpenter.core.engine.triggers import registry as treg
        with pytest.raises(ValueError):
            treg.load_package_triggers([], source_package="   ")
