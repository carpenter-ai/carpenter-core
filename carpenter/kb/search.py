"""Search backends for the Knowledge Base.

Provides semantic search via sentence embeddings.  The primary backend
(``EmbeddingBackend``) uses the all-MiniLM-L6-v2 model — trying
onnxruntime first for speed, then falling back to a pure-numpy forward
pass that works everywhere (including 32-bit ARM / Android).

For users who prefer an external embedding service, ``VectorBackend``
(Ollama) is retained as an alternative.
"""

import logging
import math
import os
import sqlite3
import struct
import urllib.request
from typing import Protocol

import httpx

from ..config import CONFIG
from ..db import get_db, db_connection, db_transaction

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
# Shared helpers
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


def _serialize_embedding(vec: list[float]) -> bytes:
    """Pack a float vector into a compact binary blob."""
    return struct.pack(f"{len(vec)}f", *vec)


def _deserialize_embedding(blob: bytes, dim: int) -> tuple[float, ...]:
    """Unpack a binary blob into a float tuple."""
    return struct.unpack(f"{dim}f", blob)


def _cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Compute cosine similarity between two vectors (pure Python)."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


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
# Local embedding (ONNX + numpy fallback)
# ---------------------------------------------------------------------------

_ONNX_MODEL_URL = (
    "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2"
    "/resolve/main/onnx/model.onnx"
)
_ONNX_MODEL_NAME = "all-MiniLM-L6-v2.onnx"
_EMBEDDING_DIM = 384

# Module-level ONNX session cache (one per process).
_onnx_session = None  # type: ignore[assignment]
_onnx_available: bool | None = None  # None = not yet probed


def _resolve_onnx_model_path() -> str:
    """Return the path to the ONNX model file."""
    kb_cfg = CONFIG.get("kb", {})
    explicit = kb_cfg.get("onnx_model_path", "")
    if explicit:
        return explicit
    base_dir = CONFIG.get("base_dir", os.path.expanduser("~/carpenter"))
    return os.path.join(base_dir, "models", _ONNX_MODEL_NAME)


def _download_onnx_model(dest: str) -> None:
    """Download the ONNX model from HuggingFace to *dest*."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    logger.info("Downloading ONNX embedding model to %s ...", dest)
    try:
        urllib.request.urlretrieve(_ONNX_MODEL_URL, dest)
        logger.info("ONNX model downloaded successfully (%s)", dest)
    except Exception:
        if os.path.exists(dest):
            os.remove(dest)
        raise


def _get_onnx_session():
    """Return a cached ``onnxruntime.InferenceSession``.

    Raises ``RuntimeError`` if onnxruntime is not installed or the model
    file is missing and cannot be downloaded.
    """
    global _onnx_session
    if _onnx_session is not None:
        return _onnx_session

    try:
        import onnxruntime as ort  # type: ignore[import-untyped]
    except ImportError:
        raise RuntimeError(
            "onnxruntime is not installed. Install it with: "
            "pip install onnxruntime>=1.17"
        )

    model_path = _resolve_onnx_model_path()
    if not os.path.isfile(model_path):
        try:
            _download_onnx_model(model_path)
        except Exception as exc:
            raise RuntimeError(
                f"ONNX model not found at {model_path} and download failed: {exc}"
            ) from exc

    _onnx_session = ort.InferenceSession(
        model_path, providers=["CPUExecutionProvider"],
    )
    return _onnx_session


def _onnx_embed(texts: list[str]) -> list[list[float]]:
    """Embed texts using the local ONNX model.

    Returns a list of 384-dim unit-normalized embedding vectors.
    """
    import numpy as np

    from .tokenizer import tokenize

    session = _get_onnx_session()
    results: list[list[float]] = []
    for text in texts:
        ids_list, mask_list, ttids_list = tokenize(text, max_length=128)
        input_ids = np.array(ids_list, dtype=np.int64)
        attention_mask = np.array(mask_list, dtype=np.int64)
        token_type_ids = np.array(ttids_list, dtype=np.int64)
        outputs = session.run(
            None,
            {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            },
        )
        token_embeddings = outputs[0]

        mask = attention_mask.astype(np.float32)
        mask_expanded = np.expand_dims(mask, axis=-1)
        summed = np.sum(token_embeddings * mask_expanded, axis=1)
        counts = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
        embedding = (summed / counts)[0]

        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm

        results.append(embedding.tolist())
    return results


def _numpy_embed(texts: list[str]) -> list[list[float]]:
    """Embed texts using the pure-numpy forward pass.

    Returns a list of 384-dim unit-normalized embedding vectors.
    Same model as ONNX, same results, just slower.
    """
    from .numpy_inference import embed
    from .tokenizer import tokenize

    all_ids: list[list[int]] = []
    all_masks: list[list[int]] = []
    all_ttids: list[list[int]] = []
    for text in texts:
        ids_list, mask_list, ttids_list = tokenize(text, max_length=128)
        all_ids.append(ids_list[0])
        all_masks.append(mask_list[0])
        all_ttids.append(ttids_list[0])

    return embed(all_ids, all_masks, all_ttids)


def _local_embed(texts: list[str]) -> list[list[float]]:
    """Embed texts using local model: ONNX if available, else numpy.

    This is the primary embedding function for EmbeddingBackend.
    """
    global _onnx_available
    if _onnx_available is None:
        try:
            _get_onnx_session()
            _onnx_available = True
        except RuntimeError:
            _onnx_available = False
            logger.info(
                "ONNX runtime unavailable; using pure-numpy inference "
                "(~0.5s/query on ARM, ~50ms on x86)"
            )

    if _onnx_available:
        return _onnx_embed(texts)
    return _numpy_embed(texts)


# ---------------------------------------------------------------------------
# EmbeddingBackend — the primary search backend
# ---------------------------------------------------------------------------

class EmbeddingBackend:
    """Semantic search using all-MiniLM-L6-v2 sentence embeddings.

    Tries ONNX Runtime for speed, falls back to pure-numpy inference.
    Same model, same embeddings — works everywhere including 32-bit ARM
    and Android.

    Stores body text in ``kb_text_content`` for reindex without filesystem
    reads, and pre-computed embeddings in ``kb_embeddings``.
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
            logger.warning("Embedding failed for %s; skipping", path)
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
            logger.warning("Embedding query failed; returning empty results")
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

def _ollama_embed(texts: list[str]) -> list[list[float]]:
    """Call Ollama /api/embed endpoint to get embeddings.

    Returns a list of embedding vectors (one per input text).
    Raises on network/API errors.
    """
    kb_cfg = CONFIG.get("kb", {})
    url = kb_cfg.get("embedding_url", "http://192.168.2.243:11434")
    model = kb_cfg.get("embedding_model", "nomic-embed-text")
    resp = httpx.post(
        f"{url}/api/embed",
        json={"model": model, "input": texts},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["embeddings"]


class VectorBackend:
    """Embedding-based semantic search using an external Ollama service.

    For users who prefer a more powerful embedding model (e.g.
    nomic-embed-text, mxbai-embed-large) running on a separate machine.
    Configure via ``kb.embedding_url`` and ``kb.embedding_model``.
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
            logger.warning("Embedding failed for %s; skipping vector update", path)
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
            logger.warning("Embedding query failed; returning empty results")
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

# Keep _embed and _onnx_embed accessible for tests that mock them
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
