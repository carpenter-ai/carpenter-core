"""Local embedding provider (all-MiniLM-L6-v2, ONNX -> numpy fallback).

Absorbs the ONNX/numpy embedding pipeline that used to live in
``carpenter/kb/search.py``.  Public behaviour is unchanged:

* ONNX Runtime is preferred when ``onnxruntime`` is installed and the
  model file is present (downloaded on demand).
* If the ONNX path is unavailable, the pure-numpy forward pass is used.
  Both paths produce the same 384-dim unit-normalised vectors.
"""

from __future__ import annotations

import logging
import os
import urllib.request

from ...config import CONFIG

logger = logging.getLogger(__name__)


_ONNX_MODEL_URL = (
    "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2"
    "/resolve/main/onnx/model.onnx"
)
_ONNX_MODEL_NAME = "all-MiniLM-L6-v2.onnx"
_EMBEDDING_DIM = 384
_DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"


def _resolve_onnx_model_path() -> str:
    """Return the path to the ONNX model file.

    Resolution order:
    1. ``embedding.local.model_path`` (new Phase 2 config block)
    2. ``kb.onnx_model_path`` (legacy fallback, kept for one release)
    3. ``{base_dir}/models/all-MiniLM-L6-v2.onnx``
    """
    emb_cfg = CONFIG.get("embedding", {}) or {}
    local_cfg = emb_cfg.get("local", {}) if isinstance(emb_cfg, dict) else {}
    explicit = (local_cfg or {}).get("model_path", "") if isinstance(local_cfg, dict) else ""
    if not explicit:
        kb_cfg = CONFIG.get("kb", {}) or {}
        explicit = kb_cfg.get("onnx_model_path", "") if isinstance(kb_cfg, dict) else ""
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


class LocalEmbeddingProvider:
    """all-MiniLM-L6-v2 via ONNX runtime, with pure-numpy fallback.

    The first call to ``embed`` triggers ONNX session construction (or
    falls back permanently to numpy if onnxruntime is missing).  The
    session is cached on the instance — a singleton ``EmbeddingService``
    keeps exactly one session per process (see D5).
    """

    model_name: str = _DEFAULT_MODEL_NAME
    vector_dim: int = _EMBEDDING_DIM

    def __init__(self) -> None:
        self._onnx_session = None
        # None = not probed yet; True/False once decided.
        self._onnx_available: bool | None = None

    # ------------------------------------------------------------------
    # ONNX session lifecycle
    # ------------------------------------------------------------------

    def _get_onnx_session(self):
        """Return a cached ``onnxruntime.InferenceSession``.

        Raises ``RuntimeError`` if onnxruntime is not installed or the
        model file is missing and cannot be downloaded.
        """
        if self._onnx_session is not None:
            return self._onnx_session

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

        self._onnx_session = ort.InferenceSession(
            model_path, providers=["CPUExecutionProvider"],
        )
        return self._onnx_session

    # ------------------------------------------------------------------
    # Embedding paths
    # ------------------------------------------------------------------

    def _onnx_embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using the local ONNX model.

        Returns a list of 384-dim unit-normalised embedding vectors.
        """
        import numpy as np

        from ...kb.tokenizer import tokenize

        session = self._get_onnx_session()
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

    def _numpy_embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using the pure-numpy forward pass."""
        from ...kb.numpy_inference import embed
        from ...kb.tokenizer import tokenize

        all_ids: list[list[int]] = []
        all_masks: list[list[int]] = []
        all_ttids: list[list[int]] = []
        for text in texts:
            ids_list, mask_list, ttids_list = tokenize(text, max_length=128)
            all_ids.append(ids_list[0])
            all_masks.append(mask_list[0])
            all_ttids.append(ttids_list[0])

        return embed(all_ids, all_masks, all_ttids)

    # ------------------------------------------------------------------
    # Provider protocol
    # ------------------------------------------------------------------

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts* using ONNX if available, else pure numpy."""
        if self._onnx_available is None:
            try:
                self._get_onnx_session()
                self._onnx_available = True
            except RuntimeError:
                self._onnx_available = False
                logger.info(
                    "ONNX runtime unavailable; using pure-numpy inference "
                    "(~0.5s/query on ARM, ~50ms on x86)"
                )

        if self._onnx_available:
            return self._onnx_embed(texts)
        return self._numpy_embed(texts)

    def is_ready(self) -> bool:
        """True once a backend (ONNX or numpy) has been selected."""
        return self._onnx_available is not None
