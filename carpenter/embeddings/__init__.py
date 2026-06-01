"""Embedding service used by both the KB and capability packages.

Phase 2 PR-1 extracts the embedding pipeline that was historically buried
inside ``carpenter/kb/search.py``.  The KB and the (forthcoming)
``PackageVectorStore`` both call ``get_embedding_service().embed(...)``;
no other code reaches into ``kb.search`` for embeddings.
"""

from __future__ import annotations

from .providers import EmbeddingProvider
from .service import (
    EmbeddingModelMismatchError,
    EmbeddingService,
    get_embedding_service,
    reset_embedding_service,
)

__all__ = [
    "EmbeddingProvider",
    "EmbeddingService",
    "EmbeddingModelMismatchError",
    "get_embedding_service",
    "reset_embedding_service",
]
