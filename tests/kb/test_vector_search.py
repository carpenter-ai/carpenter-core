"""Tests for VectorBackend (Ollama-based embedding search)."""

import math
import os
from unittest.mock import patch

import pytest

from carpenter.db import get_db
from carpenter.kb.search import (
    VectorBackend,
    _cosine_similarity,
    _deserialize_embedding,
    _embed_text,
    _serialize_embedding,
    get_search_backend,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic fake embeddings based on text content.

    Returns unit-ish vectors that share similarity when texts share words.
    """
    vectors = []
    keywords = ["schedule", "cron", "timer", "message", "chat", "email", "python", "code"]
    _dim = 16
    for text in texts:
        low = text.lower()
        vec = [0.0] * _dim
        for i, kw in enumerate(keywords):
            if kw in low:
                vec[i] = 1.0
        for word in low.split():
            if word not in keywords:
                idx = hash(word) % (_dim - len(keywords) - 1) + len(keywords)
                vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        else:
            vec[0] = 1.0
        vectors.append(vec)
    return vectors


@pytest.fixture()
def _kb_store(tmp_path):
    """Set up a KBStore with sample entries and mock config."""
    from carpenter.kb.store import KBStore

    kb_dir = str(tmp_path / "kb")
    os.makedirs(kb_dir, exist_ok=True)

    with patch("carpenter.kb.search.CONFIG", {
        "kb": {
            "embedding_url": "http://fake:11434",
            "embedding_model": "test-model",
            "embedding_dim": 16,
        }
    }), patch("carpenter.kb.search._local_embed", side_effect=_fake_embed):
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
# VectorBackend
# ---------------------------------------------------------------------------

class TestVectorBackend:
    @patch("carpenter.kb.search._ollama_embed", side_effect=_fake_embed)
    @patch("carpenter.kb.search.CONFIG", {
        "kb": {
            "embedding_url": "http://fake:11434",
            "embedding_model": "test-model",
            "embedding_dim": 16,
        }
    })
    def test_embed_and_query_roundtrip(self, mock_embed, _kb_store):
        backend = VectorBackend()
        backend.update_entry("scheduling/cron-jobs", "Cron Jobs", "Time-based scheduling", "Schedule tasks with cron timers.")
        backend.update_entry("messaging/chat", "Chat System", "Chat messaging system", "Send and receive messages via chat.")
        backend.update_entry("code/python-tips", "Python Tips", "Python programming tips", "Useful Python code snippets.")

        results = backend.query("schedule cron timer")
        assert len(results) >= 1
        paths = [r[0] for r in results]
        assert "scheduling/cron-jobs" in paths
        assert paths[0] == "scheduling/cron-jobs"

    @patch("carpenter.kb.search._ollama_embed", side_effect=_fake_embed)
    @patch("carpenter.kb.search.CONFIG", {
        "kb": {
            "embedding_url": "http://fake:11434",
            "embedding_model": "test-model",
            "embedding_dim": 16,
        }
    })
    def test_update_and_remove(self, mock_embed, _kb_store):
        backend = VectorBackend()
        backend.update_entry("test/entry", "Test", "Test entry", "Test body")

        db = get_db()
        try:
            row = db.execute("SELECT * FROM kb_embeddings WHERE path = ?", ("test/entry",)).fetchone()
            assert row is not None
            assert row["model"] == "test-model"
        finally:
            db.close()

        backend.remove_entry("test/entry")
        db = get_db()
        try:
            row = db.execute("SELECT * FROM kb_embeddings WHERE path = ?", ("test/entry",)).fetchone()
            assert row is None
        finally:
            db.close()

    @patch("carpenter.kb.search._ollama_embed", side_effect=_fake_embed)
    @patch("carpenter.kb.search.CONFIG", {
        "kb": {
            "embedding_url": "http://fake:11434",
            "embedding_model": "test-model",
            "embedding_dim": 16,
        }
    })
    def test_path_prefix_filtering(self, mock_embed, _kb_store):
        backend = VectorBackend()
        backend.update_entry("scheduling/cron-jobs", "Cron Jobs", "Time-based scheduling", "")
        backend.update_entry("messaging/chat", "Chat System", "Chat messaging", "")

        results = backend.query("system", path_prefix="messaging/")
        paths = [r[0] for r in results]
        assert all(p.startswith("messaging/") for p in paths)

    @patch("carpenter.kb.search._ollama_embed", side_effect=Exception("Ollama down"))
    @patch("carpenter.kb.search.CONFIG", {
        "kb": {
            "embedding_url": "http://fake:11434",
            "embedding_model": "test-model",
            "embedding_dim": 16,
        }
    })
    def test_graceful_degradation_query(self, mock_embed, _kb_store):
        """When embedding service fails during query, return empty list."""
        backend = VectorBackend()
        results = backend.query("cron")
        assert isinstance(results, list)
        assert results == []

    @patch("carpenter.kb.search._ollama_embed", side_effect=Exception("Ollama down"))
    @patch("carpenter.kb.search.CONFIG", {
        "kb": {
            "embedding_url": "http://fake:11434",
            "embedding_model": "test-model",
            "embedding_dim": 16,
        }
    })
    def test_graceful_degradation_update(self, mock_embed, _kb_store):
        """When embedding service fails during update, skip without crashing."""
        backend = VectorBackend()
        backend.update_entry("test/fail", "Fail", "Will fail", "Embedding fails")

        db = get_db()
        try:
            row = db.execute("SELECT * FROM kb_embeddings WHERE path = ?", ("test/fail",)).fetchone()
            assert row is None
        finally:
            db.close()

    @patch("carpenter.kb.search._ollama_embed", side_effect=_fake_embed)
    @patch("carpenter.kb.search.CONFIG", {
        "kb": {
            "embedding_url": "http://fake:11434",
            "embedding_model": "test-model",
            "embedding_dim": 16,
        }
    })
    def test_reindex_batching(self, mock_embed, _kb_store):
        """Verify reindex uses batched embed calls."""
        backend = VectorBackend()
        backend.reindex()

        assert mock_embed.call_count == 1
        texts_arg = mock_embed.call_args[0][0]
        assert len(texts_arg) == 3

    @patch("carpenter.kb.search._ollama_embed", side_effect=_fake_embed)
    @patch("carpenter.kb.search.CONFIG", {
        "kb": {
            "embedding_url": "http://fake:11434",
            "embedding_model": "test-model",
            "embedding_dim": 16,
        }
    })
    def test_query_empty(self, mock_embed, _kb_store):
        backend = VectorBackend()
        assert backend.query("") == []
        assert backend.query("   ") == []


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class TestFactory:
    def test_vector(self):
        assert isinstance(get_search_backend("vector"), VectorBackend)

    def test_unknown(self):
        with pytest.raises(ValueError, match="Unknown search backend"):
            get_search_backend("unknown")
