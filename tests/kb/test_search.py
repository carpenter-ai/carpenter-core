"""Tests for EmbeddingBackend and search helpers."""

import math
import os
from unittest.mock import patch

import pytest

from carpenter.db import get_db
from carpenter.kb.search import (
    EmbeddingBackend,
    FTS5Backend,
    OnnxEmbeddingBackend,
    TextSearchBackend,
    VectorBackend,
    _cosine_similarity,
    _deserialize_embedding,
    _embed_text,
    _extract_keywords,
    _sanitize_fts_query,
    _serialize_embedding,
    get_search_backend,
)
from carpenter.kb.store import KBStore


# ---------------------------------------------------------------------------
# Keyword / FTS compat helpers
# ---------------------------------------------------------------------------

class TestExtractKeywords:
    def test_simple_words(self):
        assert _extract_keywords("hello world") == ["hello", "world"]

    def test_special_characters(self):
        result = _extract_keywords('hello "world" OR NOT')
        assert "hello" in result
        assert "world" in result
        assert "OR" in result

    def test_empty(self):
        assert _extract_keywords("") == []

    def test_only_special_chars(self):
        result = _extract_keywords("!@#$%")
        assert result == []


class TestSanitizeFtsQueryCompat:
    """Backward-compat wrapper still works."""

    def test_simple_words(self):
        assert _sanitize_fts_query("hello world") == '"hello" OR "world"'

    def test_empty(self):
        assert _sanitize_fts_query("") == ""


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

class TestSerializeDeserialize:
    def test_roundtrip(self):
        vec = [0.1, 0.2, 0.3, -0.5, 1.0]
        blob = _serialize_embedding(vec)
        result = _deserialize_embedding(blob, len(vec))
        for a, b in zip(vec, result):
            assert abs(a - b) < 1e-6

    def test_empty_vector(self):
        blob = _serialize_embedding([])
        result = _deserialize_embedding(blob, 0)
        assert result == ()

    def test_384_dim_roundtrip(self):
        vec = [float(i) / 384 for i in range(384)]
        blob = _serialize_embedding(vec)
        assert len(blob) == 384 * 4
        result = _deserialize_embedding(blob, 384)
        assert len(result) == 384
        for a, b in zip(vec, result):
            assert abs(a - b) < 1e-6


class TestCosineSimlarity:
    def test_identical_vectors(self):
        v = (1.0, 0.0, 0.0)
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        a = (1.0, 0.0, 0.0)
        b = (0.0, 1.0, 0.0)
        assert abs(_cosine_similarity(a, b)) < 1e-6

    def test_opposite_vectors(self):
        a = (1.0, 0.0)
        b = (-1.0, 0.0)
        assert abs(_cosine_similarity(a, b) - (-1.0)) < 1e-6

    def test_zero_vector(self):
        a = (0.0, 0.0)
        b = (1.0, 0.0)
        assert _cosine_similarity(a, b) == 0.0


class TestEmbedText:
    def test_all_fields(self):
        result = _embed_text("Title", "Desc", "Body content here")
        assert "Title" in result
        assert "Desc" in result
        assert "Body content here" in result

    def test_body_truncated(self):
        long_body = "x" * 5000
        result = _embed_text("T", "", long_body)
        assert len(result) < 2100

    def test_empty_description_and_body(self):
        result = _embed_text("Title", "", "")
        assert result == "Title"


# ---------------------------------------------------------------------------
# Fake embedder for testing (384-dim)
# ---------------------------------------------------------------------------

_DIM = 384


def _fake_local_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic fake embeddings for testing (384-dim)."""
    keywords = [
        "schedule", "cron", "timer", "message", "chat",
        "email", "python", "code", "body", "content",
    ]
    vectors = []
    for text in texts:
        low = text.lower()
        vec = [0.0] * _DIM
        for i, kw in enumerate(keywords):
            if kw in low:
                vec[i] = 1.0
        for word in low.split():
            if word not in keywords:
                idx = hash(word) % (_DIM - len(keywords) - 1) + len(keywords)
                vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        else:
            vec[0] = 1.0
        vectors.append(vec)
    return vectors


@pytest.fixture()
def _embedding_kb_store(tmp_path):
    """Set up a KBStore with sample entries and mocked embedding backend.

    Relies on the autouse ``test_db`` fixture for CONFIG / database setup.
    """
    from carpenter.config import CONFIG

    kb_dir = CONFIG["kb"]["dir"]
    os.makedirs(kb_dir, exist_ok=True)

    with patch("carpenter.kb.search._local_embed", side_effect=_fake_local_embed):
        store = KBStore(kb_dir=kb_dir)
        store.write_entry(
            path="scheduling/cron-jobs",
            content="# Cron Jobs\n\nSchedule tasks with cron timers.",
            description="Time-based scheduling",
        )
        store.write_entry(
            path="messaging/chat",
            content="# Chat System\n\nSend and receive messages via chat.",
            description="Chat messaging system",
        )
        store.write_entry(
            path="code/python-tips",
            content="# Python Tips\n\nUseful Python code snippets.",
            description="Python programming tips",
        )
        yield store


# ---------------------------------------------------------------------------
# EmbeddingBackend tests
# ---------------------------------------------------------------------------

class TestEmbeddingBackend:
    @patch("carpenter.kb.search._local_embed", side_effect=_fake_local_embed)
    def test_update_and_query(self, mock_embed, _embedding_kb_store):
        backend = EmbeddingBackend()
        backend.update_entry(
            "scheduling/cron-jobs", "Cron Jobs",
            "Time-based scheduling", "Schedule tasks with cron timers.",
        )
        backend.update_entry(
            "messaging/chat", "Chat System",
            "Chat messaging system", "Send and receive messages via chat.",
        )
        backend.update_entry(
            "code/python-tips", "Python Tips",
            "Python programming tips", "Useful Python code snippets.",
        )

        results = backend.query("schedule cron timer")
        assert len(results) >= 1
        paths = [r[0] for r in results]
        assert "scheduling/cron-jobs" in paths
        assert paths[0] == "scheduling/cron-jobs"

    @patch("carpenter.kb.search._local_embed", side_effect=_fake_local_embed)
    def test_update_stores_body_text(self, mock_embed, _embedding_kb_store):
        """Body text is cached in kb_text_content for reindex."""
        backend = EmbeddingBackend()
        backend.update_entry("test/entry", "Test", "Desc", "The body content here")

        db = get_db()
        try:
            row = db.execute(
                "SELECT body FROM kb_text_content WHERE path = ?", ("test/entry",)
            ).fetchone()
            assert row is not None
            assert row["body"] == "The body content here"
        finally:
            db.close()

    @patch("carpenter.kb.search._local_embed", side_effect=_fake_local_embed)
    def test_body_content_searchable(self, mock_embed, _embedding_kb_store):
        """Content in body should influence search results."""
        backend = EmbeddingBackend()
        # This entry has 'python' only in the body
        backend.update_entry(
            "misc/entry", "Misc Entry", "General stuff",
            "This entry is about python programming and code.",
        )
        backend.update_entry(
            "other/entry", "Other Entry", "Other stuff",
            "This is about cooking and gardening.",
        )

        results = backend.query("python code")
        paths = [r[0] for r in results]
        assert "misc/entry" in paths
        # The python entry should rank higher
        if len(paths) >= 2:
            assert paths.index("misc/entry") < paths.index("other/entry")

    @patch("carpenter.kb.search._local_embed", side_effect=_fake_local_embed)
    def test_update_and_remove(self, mock_embed, _embedding_kb_store):
        backend = EmbeddingBackend()
        backend.update_entry("test/entry", "Test", "Test entry", "Test body")

        db = get_db()
        try:
            row = db.execute(
                "SELECT * FROM kb_embeddings WHERE path = ?", ("test/entry",)
            ).fetchone()
            assert row is not None
            assert row["model"] == "all-MiniLM-L6-v2"
        finally:
            db.close()

        backend.remove_entry("test/entry")
        db = get_db()
        try:
            row = db.execute(
                "SELECT * FROM kb_embeddings WHERE path = ?", ("test/entry",)
            ).fetchone()
            assert row is None
            # Also removed from text content
            row = db.execute(
                "SELECT * FROM kb_text_content WHERE path = ?", ("test/entry",)
            ).fetchone()
            assert row is None
        finally:
            db.close()

    @patch("carpenter.kb.search._local_embed", side_effect=_fake_local_embed)
    def test_path_prefix_filtering(self, mock_embed, _embedding_kb_store):
        backend = EmbeddingBackend()
        backend.update_entry(
            "scheduling/cron-jobs", "Cron Jobs", "Time-based scheduling", "",
        )
        backend.update_entry(
            "messaging/chat", "Chat System", "Chat messaging", "",
        )

        results = backend.query("system", path_prefix="messaging/")
        paths = [r[0] for r in results]
        assert all(p.startswith("messaging/") for p in paths)

    @patch("carpenter.kb.search._local_embed", side_effect=_fake_local_embed)
    def test_reindex(self, mock_embed, _embedding_kb_store):
        backend = EmbeddingBackend()
        backend.reindex()

        # Should have been called once with a batch of 3 entries
        assert mock_embed.call_count == 1
        texts_arg = mock_embed.call_args[0][0]
        assert len(texts_arg) == 3

    @patch("carpenter.kb.search._local_embed", side_effect=_fake_local_embed)
    def test_query_empty(self, mock_embed, _embedding_kb_store):
        backend = EmbeddingBackend()
        assert backend.query("") == []
        assert backend.query("   ") == []

    @patch("carpenter.kb.search._local_embed", side_effect=Exception("Embed broken"))
    def test_query_returns_empty_on_failure(self, mock_embed, _embedding_kb_store):
        """When embedding fails, query returns empty list (no crash)."""
        backend = EmbeddingBackend()
        results = backend.query("cron")
        assert results == []

    @patch("carpenter.kb.search._local_embed", side_effect=Exception("Embed broken"))
    def test_update_graceful_on_failure(self, mock_embed, _embedding_kb_store):
        """When embedding fails during update, body is still cached."""
        backend = EmbeddingBackend()
        backend.update_entry("test/fail", "Fail", "Will fail", "body text")

        db = get_db()
        try:
            # Embedding should NOT be stored
            row = db.execute(
                "SELECT * FROM kb_embeddings WHERE path = ?", ("test/fail",)
            ).fetchone()
            assert row is None
            # But body text IS stored (for later reindex)
            row = db.execute(
                "SELECT * FROM kb_text_content WHERE path = ?", ("test/fail",)
            ).fetchone()
            assert row is not None
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_fts5_alias(self):
        assert FTS5Backend is EmbeddingBackend

    def test_text_search_alias(self):
        assert TextSearchBackend is EmbeddingBackend

    def test_onnx_alias(self):
        assert OnnxEmbeddingBackend is EmbeddingBackend


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class TestFactory:
    def test_embedding(self):
        assert isinstance(get_search_backend("embedding"), EmbeddingBackend)

    def test_fts5_maps_to_embedding(self):
        assert isinstance(get_search_backend("fts5"), EmbeddingBackend)

    def test_text_maps_to_embedding(self):
        assert isinstance(get_search_backend("text"), EmbeddingBackend)

    def test_onnx_maps_to_embedding(self):
        assert isinstance(get_search_backend("onnx"), EmbeddingBackend)

    def test_hybrid_maps_to_embedding(self):
        assert isinstance(get_search_backend("hybrid"), EmbeddingBackend)

    def test_vector(self):
        assert isinstance(get_search_backend("vector"), VectorBackend)

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown search backend"):
            get_search_backend("nonexistent")
