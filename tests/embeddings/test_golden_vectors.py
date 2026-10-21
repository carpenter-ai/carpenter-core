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
import os

import pytest

from carpenter.embeddings.providers.local import LocalEmbeddingProvider


# Computed against the pure-numpy forward pass (numpy 2.4.4).  ONNX
# runtime ≥1.17 with the published model.onnx from
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


#: Where to find a local copy of the model weights, for a run that
#: wants these vectors checked.  Both backends resolve their weights
#: under ``base_dir``, which is a fresh temp directory for every test,
#: so there is no path the suite itself can put them at.
_WEIGHTS_DIR_ENV = "CARPENTER_TEST_MODEL_DIR"


@pytest.fixture
def _local_provider(monkeypatch) -> LocalEmbeddingProvider:
    """The provider, or a skip when its weights are not on this machine.

    Both backends load ``all-MiniLM-L6-v2`` from
    ``{base_dir}/models/`` and **download it from a public CDN** when it
    is missing.  Every test gets a fresh temp ``base_dir``, so running
    these golden vectors fetched ~90 MB over the internet on every full
    suite run — which only ever worked because the machine happened to
    be online, and is exactly what ``tests/conftest`` now refuses.

    So the weights have to come from the machine: point
    ``CARPENTER_TEST_MODEL_DIR`` at a directory holding
    ``models/all-MiniLM-L6-v2/model.safetensors`` to check these
    vectors.  Without it there is nothing to compute them from, and a
    skip that says so is the honest outcome — a test that downloads a
    model is not a test of this repository.
    """
    from carpenter import config
    from carpenter.kb import numpy_inference

    weights_dir = os.environ.get(_WEIGHTS_DIR_ENV, "")
    if weights_dir:
        monkeypatch.setitem(config.CONFIG, "base_dir", weights_dir)
    model_path = numpy_inference._resolve_model_path()
    if not os.path.isfile(model_path):
        pytest.skip(
            f"no all-MiniLM-L6-v2 weights at {model_path}: these golden "
            f"vectors need the real model, and this suite does not "
            f"download one. Set {_WEIGHTS_DIR_ENV} to a directory "
            f"holding models/all-MiniLM-L6-v2/model.safetensors to run "
            f"them."
        )
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


def test_dim_is_384():
    """The local provider exposes the 384-dim convention.

    Reads class attributes only, so it deliberately does not take the
    fixture above: this one needs no weights and must keep running on a
    machine that has none.
    """
    provider = LocalEmbeddingProvider()
    assert provider.vector_dim == 384
    assert provider.model_name == "all-MiniLM-L6-v2"
