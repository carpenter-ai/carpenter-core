"""Golden-vector regression test for the local embedding provider.

Per Phase 2 plan Risks #1: the refactor must not silently change the
bytes written to ``kb_embeddings``.  This test pins the first 8 floats
of the unit-normalised embedding for fixed input strings, computed with
the local provider (ONNX when available, pure-numpy fallback otherwise).

Both backends produce the same vectors by construction (the numpy
forward pass mirrors the ONNX graph); a baseline drift at 4 decimal
places would catch tokenizer ``max_length`` regressions, normalisation
order changes, or pooling-mask bugs.
"""

from __future__ import annotations

import math

import pytest

from carpenter.embeddings.providers.local import LocalEmbeddingProvider


# Computed against the pure-numpy forward pass on 2026-05-20 (Pi 5,
# numpy 2.4.4).  ONNX runtime ≥1.17 with the published model.onnx from
# sentence-transformers/all-MiniLM-L6-v2 must produce the same values
# at this tolerance — float drift between ONNX builds shows up at ~6
# decimals; we pin at 4.
_GOLDEN: list[tuple[str, list[float]]] = [
    (
        "the quick brown fox jumps over the lazy dog",
        [0.035509, 0.061385, 0.052760, 0.070614,
         0.033210, -0.030785, 0.006609, -0.061198],
    ),
    (
        "carpenter embedding service test",
        [-0.079034, -0.038595, 0.021382, -0.039997,
         -0.027085, -0.044641, -0.087212, -0.033395],
    ),
]

_TOL = 1e-4


@pytest.fixture(scope="module")
def _local_provider() -> LocalEmbeddingProvider:
    return LocalEmbeddingProvider()


@pytest.mark.parametrize("text,baseline", _GOLDEN, ids=[g[0][:24] for g in _GOLDEN])
def test_golden_vector_first_8_floats(
    _local_provider: LocalEmbeddingProvider, text: str, baseline: list[float],
):
    """First 8 floats of the unit-normalised embedding match the baseline.

    Catches tokenizer / normalisation regressions during the Phase 2 PR-1
    refactor.  If you hit this in PR review, do not just update the
    baseline — verify the refactor didn't change embedding semantics.
    """
    vectors = _local_provider.embed([text])
    assert len(vectors) == 1
    vec = vectors[0]
    assert len(vec) == 384

    drift = [abs(v - b) for v, b in zip(vec[:8], baseline)]
    assert max(drift) < _TOL, (
        f"Embedding drift for input {text!r}: "
        f"got {[round(x, 6) for x in vec[:8]]}, "
        f"expected ~{baseline}, max drift {max(drift):.6f}"
    )


def test_embedding_is_unit_normalised(_local_provider: LocalEmbeddingProvider):
    """Sanity: vectors come out unit-normalised (||v|| ≈ 1)."""
    vec = _local_provider.embed(["normalised"])[0]
    norm = math.sqrt(sum(x * x for x in vec))
    assert abs(norm - 1.0) < 1e-3


def test_dim_is_384(_local_provider: LocalEmbeddingProvider):
    """The local provider exposes the 384-dim convention."""
    assert _local_provider.vector_dim == 384
    assert _local_provider.model_name == "all-MiniLM-L6-v2"
