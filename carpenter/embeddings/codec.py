"""Codec helpers for embedding vectors.

Centralises blob (de)serialisation and cosine similarity so the KB
backend, ``PackageVectorStore`` (Phase 2 PR-2), and any future caller
share one implementation.  The on-disk format is identical to the one
``kb/search.py`` used historically (``struct.pack('Nf')`` little-endian)
so existing ``kb_embeddings`` rows remain valid.
"""

from __future__ import annotations

import math
import struct


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
