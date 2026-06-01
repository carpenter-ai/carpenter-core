"""Ollama-based embedding provider.

Wraps Ollama's ``/api/embed`` endpoint for users who prefer an external
embedding service (e.g. ``nomic-embed-text``, ``mxbai-embed-large``).
"""

from __future__ import annotations

import logging

import httpx

from ...config import CONFIG

logger = logging.getLogger(__name__)


def _resolve_ollama_settings() -> tuple[str, str, int]:
    """Return ``(url, model, dim)`` from config.

    Reads the new ``embedding.ollama.*`` block first, then falls back to
    the legacy ``kb.embedding_*`` keys (kept for one release).
    """
    emb_cfg = CONFIG.get("embedding", {}) or {}
    ollama_cfg = emb_cfg.get("ollama", {}) if isinstance(emb_cfg, dict) else {}
    if not isinstance(ollama_cfg, dict):
        ollama_cfg = {}

    kb_cfg = CONFIG.get("kb", {}) or {}
    if not isinstance(kb_cfg, dict):
        kb_cfg = {}

    url = ollama_cfg.get("url") or kb_cfg.get(
        "embedding_url", "http://192.168.2.243:11434",
    )
    model = ollama_cfg.get("model") or kb_cfg.get(
        "embedding_model", "nomic-embed-text",
    )
    dim = ollama_cfg.get("dim") or kb_cfg.get("embedding_dim", 768)
    return url, model, int(dim)


class OllamaEmbeddingProvider:
    """Embed texts via Ollama's ``/api/embed`` endpoint."""

    def __init__(
        self,
        *,
        url: str | None = None,
        model: str | None = None,
        vector_dim: int | None = None,
    ) -> None:
        cfg_url, cfg_model, cfg_dim = _resolve_ollama_settings()
        self._url = url or cfg_url
        self.model_name = model or cfg_model
        self.vector_dim = int(vector_dim) if vector_dim is not None else cfg_dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Call Ollama's ``/api/embed`` endpoint.

        Raises on network/API errors; the caller (``EmbeddingService`` or
        the KB ``VectorBackend``) is responsible for graceful degradation.
        """
        # Re-read settings every call so test patches of CONFIG take effect.
        url, model, _ = _resolve_ollama_settings()
        resp = httpx.post(
            f"{url}/api/embed",
            json={"model": self.model_name or model, "input": texts},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["embeddings"]

    def is_ready(self) -> bool:
        """Always True — the provider is just an HTTP client."""
        return True
