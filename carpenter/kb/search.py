"""Search backends for the Knowledge Base.

Provides semantic search via sentence embeddings.  The primary backend
(``EmbeddingBackend``) uses the all-MiniLM-L6-v2 model — delegating to
``carpenter.embeddings.EmbeddingService``, which picks ONNX Runtime when
available and falls back to a pure-numpy forward pass that works
everywhere (including 32-bit ARM / Android).

For users who prefer an external embedding service, ``VectorBackend``
(Ollama) is retained as an alternative.

Phase 2 PR-1: the embedding pipeline itself now lives in
``carpenter.embeddings``.  This module keeps KB-specific composition
(``_embed_text``), KB storage (``kb_embeddings``/``kb_text_content``),
and thin shims (``_local_embed``/``_ollama_embed``) so existing tests
that patch ``carpenter.kb.search._local_embed`` /
``carpenter.kb.search._ollama_embed`` continue to work verbatim.
"""

import logging
from typing import Protocol

from ..config import CONFIG
from ..db import db_connection, db_transaction
from ..embeddings.codec import (
    _cosine_similarity,
    _deserialize_embedding,
    _serialize_embedding,
)
from ..embeddings.providers.local import (
    _EMBEDDING_DIM,
    _ONNX_MODEL_NAME,
    _ONNX_MODEL_URL,
    _download_onnx_model,
    _resolve_onnx_model_path,
)
from ..embeddings.providers.ollama import OllamaEmbeddingProvider
from ..embeddings.service import get_embedding_service

logger = logging.getLogger(__name__)


class SearchBackend(Protocol):
    """Protocol for KB search backends."""

    def reindex(self) -> None:
        """Full reindex from kb_entries table."""
        ...

    def update_entry(self, path: str, title: str, description: str, body: str) -> None:
        """Incremental update for a single entry."""
        ...

    def remove_entry(self, path: str) -> None:
        """Remove entry from index."""
        ...

    def query(
        self, query_text: str, max_results: int = 5, path_prefix: str | None = None,
    ) -> list[tuple[str, float]]:
        """Return (path, score) pairs ranked by relevance."""
        ...


# ---------------------------------------------------------------------------
# KB-specific helpers (stay here; not part of the embedding service)
# ---------------------------------------------------------------------------

def _embed_text(title: str, description: str, body: str) -> str:
    """Build embedding input text from entry fields.

    Title is repeated for emphasis weighting.
    """
    parts = [title]
    if description:
        parts.append(description)
    if body:
        parts.append(body[:2000])
    return ". ".join(parts)


def _extract_keywords(text: str) -> list[str]:
    """Extract search keywords from user query text.

    Strips non-alphanumeric characters, drops URL-like tokens and
    single-character words.  Returns a list of cleaned keywords.
    """
    words = text.split()
    keywords: list[str] = []
    for word in words:
        if "://" in word or word.startswith("http"):
            continue
        clean = "".join(c for c in word if c.isalnum() or c in "-_")
        if clean and len(clean) > 1:
            keywords.append(clean)
    return keywords


def _sanitize_fts_query(text: str) -> str:
    """Backward-compatible wrapper: extract keywords and join with OR.

    Retained for any external callers that imported this helper.
    """
    keywords = _extract_keywords(text)
    return " OR ".join(f'"{kw}"' for kw in keywords)


# ---------------------------------------------------------------------------
# Backward-compat shims for the embedding pipeline.
#
# These exist so existing tests that patch
# ``carpenter.kb.search._local_embed`` / ``carpenter.kb.search._ollama_embed``
# keep working unchanged.  New code should call
# ``carpenter.embeddings.service.get_embedding_service`` directly.
# ---------------------------------------------------------------------------

def _local_embed(texts: list[str]) -> list[list[float]]:
    # pragma: no cover -- compat shim; use carpenter.embeddings.service in new code.
    return get_embedding_service().embed(texts)


# Cached Ollama provider so we don't reconstruct it on every call.
_ollama_provider: OllamaEmbeddingProvider | None = None


def _get_ollama_provider() -> OllamaEmbeddingProvider:
    global _ollama_provider
    if _ollama_provider is None:
        _ollama_provider = OllamaEmbeddingProvider()
    return _ollama_provider


def _ollama_embed(texts: list[str]) -> list[list[float]]:
    # pragma: no cover -- compat shim; use carpenter.embeddings.service in new code.
    return _get_ollama_provider().embed(texts)


# ---------------------------------------------------------------------------
# EmbeddingBackend — the primary search backend
# ---------------------------------------------------------------------------

class EmbeddingBackend:
    """Semantic search using all-MiniLM-L6-v2 sentence embeddings.

    Delegates the actual embedding to ``carpenter.embeddings.EmbeddingService``
    via the ``_local_embed`` shim above (kept patchable for existing tests).
    Storage is unchanged: body text in ``kb_text_content``, pre-computed
    vectors in ``kb_embeddings``.
    """

    _BATCH_SIZE = 10
    _MODEL_NAME = "all-MiniLM-L6-v2"

    def reindex(self) -> None:
        """Embed all kb_entries and store in kb_embeddings.

        Uses cached body text from kb_text_content when available,
        falls back to title+description only.
        """
        with db_transaction() as db:
            rows = db.execute(
                "SELECT e.path, e.title, e.description, "
                "COALESCE(t.body, '') AS body "
                "FROM kb_entries e "
                "LEFT JOIN kb_text_content t ON e.path = t.path"
            ).fetchall()
            if not rows:
                return
            db.execute("DELETE FROM kb_embeddings")
            for i in range(0, len(rows), self._BATCH_SIZE):
                batch = rows[i : i + self._BATCH_SIZE]
                texts = [
                    _embed_text(r["title"], r["description"], r["body"])
                    for r in batch
                ]
                try:
                    vectors = _local_embed(texts)
                except Exception as _exc:
                    logger.warning(
                        "Embedding failed during reindex (batch %d); "
                        "skipping remaining entries",
                        i // self._BATCH_SIZE,
                        exc_info=True,
                    )
                    break
                for row, vec in zip(batch, vectors):
                    db.execute(
                        "INSERT OR REPLACE INTO kb_embeddings"
                        "(path, embedding, model, updated_at) "
                        "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                        (row["path"], _serialize_embedding(vec), self._MODEL_NAME),
                    )

    def update_entry(self, path: str, title: str, description: str, body: str) -> None:
        """Store body text, embed entry, and upsert into kb_embeddings."""
        with db_transaction() as db:
            # Cache body text for future reindex
            db.execute(
                "INSERT OR REPLACE INTO kb_text_content(path, body) VALUES (?, ?)",
                (path, body),
            )

        text = _embed_text(title, description, body)
        try:
            vectors = _local_embed([text])
        except Exception as _exc:
            # Intentionally swallow: embedding is best-effort; entry still
            # exists in kb_text_content even if vector update fails.
            logger.warning("Embedding failed for %s; skipping", path, exc_info=True)
            return
        with db_transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO kb_embeddings"
                "(path, embedding, model, updated_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (path, _serialize_embedding(vectors[0]), self._MODEL_NAME),
            )

    def remove_entry(self, path: str) -> None:
        """Remove entry from text content cache and embeddings."""
        with db_transaction() as db:
            db.execute("DELETE FROM kb_text_content WHERE path = ?", (path,))
            db.execute("DELETE FROM kb_embeddings WHERE path = ?", (path,))

    def query(
        self, query_text: str, max_results: int = 5, path_prefix: str | None = None,
    ) -> list[tuple[str, float]]:
        """Semantic search: embed query, cosine similarity vs stored embeddings."""
        if not query_text or not query_text.strip():
            return []
        try:
            query_vecs = _local_embed([query_text])
        except Exception as _exc:
            # Intentionally swallow: query failures degrade to no semantic
            # results; caller can still fall back to keyword search.
            logger.warning(
                "Embedding query failed; returning empty results", exc_info=True,
            )
            return []

        query_vec = tuple(query_vecs[0])

        with db_connection() as db:
            if path_prefix:
                rows = db.execute(
                    "SELECT path, embedding FROM kb_embeddings WHERE path LIKE ? || '%'",
                    (path_prefix,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT path, embedding FROM kb_embeddings"
                ).fetchall()

            scored: list[tuple[str, float]] = []
            for row in rows:
                stored_vec = _deserialize_embedding(row["embedding"], _EMBEDDING_DIM)
                sim = _cosine_similarity(query_vec, stored_vec)
                scored.append((row["path"], sim))

            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:max_results]


# ---------------------------------------------------------------------------
# VectorBackend — Ollama-based embedding (optional, for power users)
# ---------------------------------------------------------------------------

class VectorBackend:
    """Embedding-based semantic search using an external Ollama service.

    For users who prefer a more powerful embedding model (e.g.
    nomic-embed-text, mxbai-embed-large) running on a separate machine.
    Configure via ``kb.embedding_url`` and ``kb.embedding_model`` (or
    the new ``embedding.ollama.*`` block).
    """

    _BATCH_SIZE = 10

    def reindex(self) -> None:
        """Embed all kb_entries and store in kb_embeddings."""
        with db_transaction() as db:
            rows = db.execute(
                "SELECT e.path, e.title, e.description, "
                "COALESCE(t.body, '') AS body "
                "FROM kb_entries e "
                "LEFT JOIN kb_text_content t ON e.path = t.path"
            ).fetchall()
            if not rows:
                return
            kb_cfg = CONFIG.get("kb", {})
            model = kb_cfg.get("embedding_model", "nomic-embed-text")
            db.execute("DELETE FROM kb_embeddings")
            for i in range(0, len(rows), self._BATCH_SIZE):
                batch = rows[i : i + self._BATCH_SIZE]
                texts = [
                    _embed_text(r["title"], r["description"], r["body"])
                    for r in batch
                ]
                try:
                    vectors = _ollama_embed(texts)
                except Exception as _exc:
                    logger.warning(
                        "Embedding service unavailable during reindex (batch %d); "
                        "skipping remaining entries",
                        i // self._BATCH_SIZE,
                        exc_info=True,
                    )
                    break
                for row, vec in zip(batch, vectors):
                    db.execute(
                        "INSERT OR REPLACE INTO kb_embeddings"
                        "(path, embedding, model, updated_at) "
                        "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                        (row["path"], _serialize_embedding(vec), model),
                    )

    def update_entry(self, path: str, title: str, description: str, body: str) -> None:
        """Store body, embed via Ollama, upsert into kb_embeddings."""
        with db_transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO kb_text_content(path, body) VALUES (?, ?)",
                (path, body),
            )

        text = _embed_text(title, description, body)
        try:
            vectors = _ollama_embed([text])
        except Exception as _exc:
            # Intentionally swallow: embedding service may be down; entry
            # body is still cached so a later reindex can fix this.
            logger.warning(
                "Embedding failed for %s; skipping vector update",
                path, exc_info=True,
            )
            return
        kb_cfg = CONFIG.get("kb", {})
        model = kb_cfg.get("embedding_model", "nomic-embed-text")
        with db_transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO kb_embeddings"
                "(path, embedding, model, updated_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (path, _serialize_embedding(vectors[0]), model),
            )

    def remove_entry(self, path: str) -> None:
        """Remove from text content cache and embeddings."""
        with db_transaction() as db:
            db.execute("DELETE FROM kb_text_content WHERE path = ?", (path,))
            db.execute("DELETE FROM kb_embeddings WHERE path = ?", (path,))

    def query(
        self, query_text: str, max_results: int = 5, path_prefix: str | None = None,
    ) -> list[tuple[str, float]]:
        """Semantic search: embed query via Ollama, cosine similarity."""
        if not query_text or not query_text.strip():
            return []
        try:
            query_vecs = _ollama_embed([query_text])
        except Exception as _exc:
            # Intentionally swallow: query failures degrade to no semantic
            # results; caller can still fall back to keyword search.
            logger.warning(
                "Embedding query failed; returning empty results", exc_info=True,
            )
            return []

        kb_cfg = CONFIG.get("kb", {})
        dim = kb_cfg.get("embedding_dim", 768)
        query_vec = tuple(query_vecs[0])

        with db_connection() as db:
            if path_prefix:
                rows = db.execute(
                    "SELECT path, embedding FROM kb_embeddings WHERE path LIKE ? || '%'",
                    (path_prefix,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT path, embedding FROM kb_embeddings"
                ).fetchall()

            scored: list[tuple[str, float]] = []
            for row in rows:
                stored_vec = _deserialize_embedding(row["embedding"], dim)
                sim = _cosine_similarity(query_vec, stored_vec)
                scored.append((row["path"], sim))

            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:max_results]


# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------

# These existed in older code — keep them so nothing breaks on import.
TextSearchBackend = EmbeddingBackend
FTS5Backend = EmbeddingBackend
OnnxEmbeddingBackend = EmbeddingBackend

# Keep _embed accessible for tests that mock it
_embed = _ollama_embed


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_search_backend(backend_name: str = "embedding") -> SearchBackend:
    """Factory for search backends.

    Supported names:
    - ``embedding`` (default) — local all-MiniLM-L6-v2 (ONNX or numpy)
    - ``vector`` — external Ollama embedding service
    - ``fts5``, ``text``, ``onnx``, ``hybrid`` — all map to ``embedding``
      for backward compatibility

    The ``embedding`` backend automatically tries onnxruntime for speed
    and falls back to pure-numpy inference.
    """
    if backend_name in ("embedding", "fts5", "text", "onnx", "hybrid"):
        return EmbeddingBackend()
    if backend_name == "vector":
        return VectorBackend()
    raise ValueError(f"Unknown search backend: {backend_name}")


# ---------------------------------------------------------------------------
# Re-exports for legacy callers
#
# ``test_onnx_search.py`` and others import ``_serialize_embedding`` /
# ``_deserialize_embedding`` directly from ``carpenter.kb.search``;
# keep these available from this module.  The implementations now live
# in ``carpenter.embeddings.codec``.
# ---------------------------------------------------------------------------

__all__ = [
    "SearchBackend",
    "EmbeddingBackend",
    "VectorBackend",
    "TextSearchBackend",
    "FTS5Backend",
    "OnnxEmbeddingBackend",
    "get_search_backend",
    "_embed_text",
    "_extract_keywords",
    "_sanitize_fts_query",
    "_local_embed",
    "_ollama_embed",
    "_serialize_embedding",
    "_deserialize_embedding",
    "_cosine_similarity",
    "_resolve_onnx_model_path",
    "_download_onnx_model",
    "_ONNX_MODEL_URL",
    "_ONNX_MODEL_NAME",
    "_EMBEDDING_DIM",
]
