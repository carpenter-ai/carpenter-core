"""Shared fixtures for KB search backend tests."""

import math


# ---------------------------------------------------------------------------
# Shared fake embedding helpers
# ---------------------------------------------------------------------------

_KEYWORDS = [
    "schedule", "cron", "timer", "message", "chat",
    "email", "python", "code",
]


def fake_embed(texts: list[str], dim: int = 16) -> list[list[float]]:
    """Deterministic fake embeddings based on text content.

    Returns unit-ish vectors that share similarity when texts share keywords.
    Works for any embedding dimension (16 for Ollama-style, 384 for ONNX).
    """
    vectors = []
    for text in texts:
        low = text.lower()
        vec = [0.0] * dim
        for i, kw in enumerate(_KEYWORDS):
            if kw in low:
                vec[i] = 1.0
        # Hash unknown words into distinct positions
        for word in low.split():
            if word not in _KEYWORDS:
                idx = hash(word) % (dim - len(_KEYWORDS) - 1) + len(_KEYWORDS)
                vec[idx] += 1.0
        # L2 normalize
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        else:
            vec[0] = 1.0
        vectors.append(vec)
    return vectors


def fake_embed_16(texts: list[str]) -> list[list[float]]:
    """16-dim fake embeddings for VectorBackend tests."""
    return fake_embed(texts, dim=16)


def fake_embed_384(texts: list[str]) -> list[list[float]]:
    """384-dim fake embeddings for EmbeddingBackend tests."""
    return fake_embed(texts, dim=384)


# ---------------------------------------------------------------------------
# Shared CONFIG dicts
# ---------------------------------------------------------------------------

VECTOR_CONFIG = {
    "kb": {
        "embedding_url": "http://fake:11434",
        "embedding_model": "test-model",
        "embedding_dim": 16,
    }
}
