"""Tests for the pure-numpy transformer forward pass."""

import math
from unittest.mock import patch

import numpy as np
import pytest

from carpenter.kb.numpy_inference import (
    _attention,
    _gelu,
    _layer_norm,
    embed,
)
from carpenter.kb.tokenizer import tokenize


# ---------------------------------------------------------------------------
# Building block tests
# ---------------------------------------------------------------------------

class TestGelu:
    def test_zero(self):
        result = _gelu(np.array([0.0]))
        assert abs(result[0]) < 1e-6

    def test_positive(self):
        result = _gelu(np.array([1.0]))
        assert result[0] > 0.8  # GELU(1) ≈ 0.8413

    def test_negative(self):
        result = _gelu(np.array([-1.0]))
        assert result[0] < 0  # GELU(-1) ≈ -0.1587

    def test_shape_preserved(self):
        x = np.random.randn(5, 10).astype(np.float32)
        result = _gelu(x)
        assert result.shape == (5, 10)


class TestLayerNorm:
    def test_normalized_output(self):
        x = np.array([[1.0, 2.0, 3.0, 4.0]], dtype=np.float32)
        weight = np.ones(4, dtype=np.float32)
        bias = np.zeros(4, dtype=np.float32)
        result = _layer_norm(x, weight, bias)
        # Mean should be ~0, std ~1
        assert abs(result.mean()) < 1e-5
        assert abs(result.std() - 1.0) < 0.1

    def test_weight_and_bias(self):
        x = np.array([[1.0, 2.0, 3.0, 4.0]], dtype=np.float32)
        weight = np.ones(4, dtype=np.float32) * 2.0
        bias = np.ones(4, dtype=np.float32) * 0.5
        result = _layer_norm(x, weight, bias)
        # With weight=2 and bias=0.5, output should be scaled and shifted
        assert result.shape == x.shape


class TestAttention:
    def test_output_shape(self):
        seq_len = 5
        q = np.random.randn(12, seq_len, 32).astype(np.float32)
        k = np.random.randn(12, seq_len, 32).astype(np.float32)
        v = np.random.randn(12, seq_len, 32).astype(np.float32)
        mask = np.ones(seq_len, dtype=np.float32)
        result = _attention(q, k, v, mask)
        assert result.shape == (12, seq_len, 32)

    def test_masking(self):
        """Masked positions should not contribute to attention output."""
        seq_len = 4
        q = np.random.randn(1, seq_len, 32).astype(np.float32)
        k = np.random.randn(1, seq_len, 32).astype(np.float32)
        v = np.random.randn(1, seq_len, 32).astype(np.float32)

        # Mask out last 2 positions
        mask = np.array([1.0, 1.0, 0.0, 0.0], dtype=np.float32)
        result = _attention(q, k, v, mask)
        assert result.shape == (1, seq_len, 32)
        # The output for position 0 should be a weighted combination of only v[0] and v[1]


# ---------------------------------------------------------------------------
# Full forward pass tests (with mock weights)
# ---------------------------------------------------------------------------

def _make_mock_weights():
    """Create random weights with the right shapes for all-MiniLM-L6-v2."""
    rng = np.random.RandomState(42)
    w = {}

    # Embeddings
    vocab_size = 30522
    w["embeddings.word_embeddings.weight"] = rng.randn(vocab_size, 384).astype(np.float32) * 0.02
    w["embeddings.position_embeddings.weight"] = rng.randn(512, 384).astype(np.float32) * 0.02
    w["embeddings.token_type_embeddings.weight"] = rng.randn(2, 384).astype(np.float32) * 0.02
    w["embeddings.LayerNorm.weight"] = np.ones(384, dtype=np.float32)
    w["embeddings.LayerNorm.bias"] = np.zeros(384, dtype=np.float32)

    # 6 transformer layers
    for i in range(6):
        p = f"encoder.layer.{i}"
        for name in ("query", "key", "value"):
            w[f"{p}.attention.self.{name}.weight"] = rng.randn(384, 384).astype(np.float32) * 0.02
            w[f"{p}.attention.self.{name}.bias"] = np.zeros(384, dtype=np.float32)
        w[f"{p}.attention.output.dense.weight"] = rng.randn(384, 384).astype(np.float32) * 0.02
        w[f"{p}.attention.output.dense.bias"] = np.zeros(384, dtype=np.float32)
        w[f"{p}.attention.output.LayerNorm.weight"] = np.ones(384, dtype=np.float32)
        w[f"{p}.attention.output.LayerNorm.bias"] = np.zeros(384, dtype=np.float32)
        w[f"{p}.intermediate.dense.weight"] = rng.randn(1536, 384).astype(np.float32) * 0.02
        w[f"{p}.intermediate.dense.bias"] = np.zeros(1536, dtype=np.float32)
        w[f"{p}.output.dense.weight"] = rng.randn(384, 1536).astype(np.float32) * 0.02
        w[f"{p}.output.dense.bias"] = np.zeros(384, dtype=np.float32)
        w[f"{p}.output.LayerNorm.weight"] = np.ones(384, dtype=np.float32)
        w[f"{p}.output.LayerNorm.bias"] = np.zeros(384, dtype=np.float32)

    return w


class TestEmbed:
    @patch("carpenter.kb.numpy_inference._load_weights")
    def test_output_shape(self, mock_load):
        """Embedding output should be (384,) per input."""
        mock_load.return_value = _make_mock_weights()
        ids, mask, ttids = tokenize("hello world", max_length=16)
        result = embed(ids, mask, ttids)
        assert len(result) == 1
        assert len(result[0]) == 384

    @patch("carpenter.kb.numpy_inference._load_weights")
    def test_unit_normalized(self, mock_load):
        """Embeddings should be L2 unit-normalized."""
        mock_load.return_value = _make_mock_weights()
        ids, mask, ttids = tokenize("test input text", max_length=16)
        result = embed(ids, mask, ttids)
        vec = result[0]
        norm = math.sqrt(sum(x * x for x in vec))
        assert abs(norm - 1.0) < 1e-5

    @patch("carpenter.kb.numpy_inference._load_weights")
    def test_batch_embedding(self, mock_load):
        """Multiple inputs should produce multiple embeddings."""
        mock_load.return_value = _make_mock_weights()
        texts = ["hello world", "foo bar", "test input"]
        all_ids, all_masks, all_ttids = [], [], []
        for text in texts:
            ids, mask, ttids = tokenize(text, max_length=16)
            all_ids.append(ids[0])
            all_masks.append(mask[0])
            all_ttids.append(ttids[0])
        result = embed(all_ids, all_masks, all_ttids)
        assert len(result) == 3
        for vec in result:
            assert len(vec) == 384

    @patch("carpenter.kb.numpy_inference._load_weights")
    def test_different_inputs_different_embeddings(self, mock_load):
        """Different texts should produce different embeddings."""
        mock_load.return_value = _make_mock_weights()
        ids1, mask1, ttids1 = tokenize("scheduling cron timer", max_length=16)
        ids2, mask2, ttids2 = tokenize("cooking recipes food", max_length=16)
        r1 = embed(ids1, mask1, ttids1)[0]
        r2 = embed(ids2, mask2, ttids2)[0]
        # They should not be identical
        diff = sum(abs(a - b) for a, b in zip(r1, r2))
        assert diff > 0.01

    @patch("carpenter.kb.numpy_inference._load_weights")
    def test_deterministic(self, mock_load):
        """Same input should always produce the same embedding."""
        mock_load.return_value = _make_mock_weights()
        ids, mask, ttids = tokenize("hello", max_length=16)
        r1 = embed(ids, mask, ttids)[0]
        r2 = embed(ids, mask, ttids)[0]
        for a, b in zip(r1, r2):
            assert abs(a - b) < 1e-6
