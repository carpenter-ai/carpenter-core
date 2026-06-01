"""Process-wide embedding service.

``EmbeddingService`` owns provider selection, batching, and a process-wide
``threading.Lock`` around the underlying ONNX session.  The singleton is
constructed lazily from ``CONFIG['embedding']`` (with one-release fallback
to the legacy ``kb.embedding_*`` keys) and reused by every caller in the
process so the ~1.5–3s ONNX load happens at most once.

Design references: see Phase 2 plan decisions D1, D2, D3, D5, D12.
"""

from __future__ import annotations

import logging
import threading

from ..config import CONFIG
from .providers import (
    EmbeddingProvider,
    LocalEmbeddingProvider,
    OllamaEmbeddingProvider,
)

logger = logging.getLogger(__name__)


class EmbeddingModelMismatchError(RuntimeError):
    """Raised when a caller attempts to mix vectors from different model identities.

    The PackageVectorStore (Phase 2 PR-2) enforces this at upsert/search time
    so cross-dim contamination cannot silently corrupt search results.  The
    exception is exported here in PR-1 so PR-2 doesn't have to reshape the
    public surface.
    """


_DEFAULT_BATCH_SIZE = 10


class EmbeddingService:
    """Sync, batch-aware, process-wide embedding facade."""

    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        provider_kind: str = "local",
    ) -> None:
        self._provider = provider
        self._batch_size = max(1, int(batch_size))
        self._provider_kind = provider_kind
        # Process-wide lock; ``onnxruntime`` is documented thread-safe but
        # has been observed to segfault on the Pi under contention (see D5).
        self._lock = threading.Lock()
        self._warmed_up = False

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def vector_dim(self) -> int:
        return int(self._provider.vector_dim)

    @property
    def model_name(self) -> str:
        return str(self._provider.model_name)

    @property
    def model_identity(self) -> str:
        """Stable namespace fingerprint, e.g. ``local:all-MiniLM-L6-v2:384``.

        ``PackageVectorStore`` (PR-2) uses this to detect cross-model
        contamination of a namespace.  Format intentionally simple so it
        round-trips through JSON config without escaping.
        """
        return f"{self._provider_kind}:{self.model_name}:{self.vector_dim}"

    def is_ready(self) -> bool:
        """True iff the provider has materialised its session (or is HTTP)."""
        return bool(self._provider.is_ready())

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def provider_kind(self) -> str:
        return self._provider_kind

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts*, batching at ``self.batch_size``.

        Thread-safe: the inner provider call is serialised with a
        process-wide lock (D5).  Returns one vector per input text.
        """
        if not texts:
            return []

        results: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            with self._lock:
                results.extend(self._provider.embed(batch))
        return results

    def warm_up(self) -> None:
        """Force the provider's session to load.

        Safe to call repeatedly; subsequent calls are no-ops.  Network
        providers (Ollama) treat this as a no-op so daemon boot does not
        depend on a remote endpoint being up.
        """
        if self._warmed_up:
            return
        if self._provider_kind == "ollama":
            # Avoid coupling daemon boot to a remote service; the first
            # real query will surface any connectivity problems.
            self._warmed_up = True
            return
        try:
            with self._lock:
                self._provider.embed(["warm-up probe"])
            self._warmed_up = True
            logger.info(
                "Embedding service warm-up complete (provider=%s model=%s dim=%d)",
                self._provider_kind, self.model_name, self.vector_dim,
            )
        except Exception:
            logger.warning(
                "Embedding service warm-up failed (provider=%s); "
                "first real query will retry.",
                self._provider_kind,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_singleton: EmbeddingService | None = None
_singleton_lock = threading.Lock()


def _resolve_embedding_config() -> tuple[str, int, dict]:
    """Return ``(provider_kind, batch_size, raw_block)`` from CONFIG."""
    raw = CONFIG.get("embedding", {})
    if not isinstance(raw, dict):
        raw = {}
    provider_kind = str(raw.get("provider", "local") or "local").lower()
    if provider_kind not in {"local", "ollama"}:
        logger.warning(
            "Unknown embedding.provider=%r; falling back to 'local'",
            provider_kind,
        )
        provider_kind = "local"
    batch_size = int(raw.get("batch_size", _DEFAULT_BATCH_SIZE) or _DEFAULT_BATCH_SIZE)
    return provider_kind, batch_size, raw


def _build_service() -> EmbeddingService:
    provider_kind, batch_size, _raw = _resolve_embedding_config()
    if provider_kind == "ollama":
        provider: EmbeddingProvider = OllamaEmbeddingProvider()
    else:
        provider = LocalEmbeddingProvider()
    return EmbeddingService(
        provider, batch_size=batch_size, provider_kind=provider_kind,
    )


def get_embedding_service() -> EmbeddingService:
    """Return the process-wide ``EmbeddingService`` singleton.

    Lazily constructed from ``CONFIG['embedding']`` on first call.  Tests
    that need to reset the singleton should call ``reset_embedding_service``.
    """
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = _build_service()
    return _singleton


def reset_embedding_service() -> None:
    """Drop the cached singleton.

    Intended for tests; production code should not need to call this.
    """
    global _singleton
    with _singleton_lock:
        _singleton = None


__all__ = [
    "EmbeddingService",
    "EmbeddingModelMismatchError",
    "get_embedding_service",
    "reset_embedding_service",
]
