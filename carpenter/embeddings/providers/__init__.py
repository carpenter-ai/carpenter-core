"""Embedding provider implementations.

The ``EmbeddingProvider`` protocol describes the minimum contract every
provider must satisfy; concrete implementations live in this package.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .local import LocalEmbeddingProvider
from .ollama import OllamaEmbeddingProvider


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Concrete embedding providers implement this minimal interface.

    Attributes:
        model_name: Stable identifier for the model (e.g. ``"all-MiniLM-L6-v2"``).
        vector_dim: Dimensionality of returned vectors.
    """

    model_name: str
    vector_dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...  # pragma: no cover
    def is_ready(self) -> bool: ...  # pragma: no cover


__all__ = [
    "EmbeddingProvider",
    "LocalEmbeddingProvider",
    "OllamaEmbeddingProvider",
]
