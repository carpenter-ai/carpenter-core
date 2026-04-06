"""Tests for ONNX embedding infrastructure and tokenizer."""

from carpenter.kb.search import (
    EmbeddingBackend,
    FTS5Backend,
    OnnxEmbeddingBackend,
    TextSearchBackend,
    _deserialize_embedding,
    _serialize_embedding,
    get_search_backend,
)
from carpenter.kb.tokenizer import (
    _basic_tokenize,
    _get_vocab,
    _wordpiece_tokenize,
    tokenize,
)


# ---------------------------------------------------------------------------
# Tokenizer tests
# ---------------------------------------------------------------------------

class TestBasicTokenize:
    def test_lowercase_and_split(self):
        tokens = _basic_tokenize("Hello World")
        assert tokens == ["hello", "world"]

    def test_punctuation_split(self):
        tokens = _basic_tokenize("it's a test.")
        assert "it" in tokens
        assert "'" in tokens
        assert "s" in tokens
        assert "." in tokens

    def test_accents_stripped(self):
        tokens = _basic_tokenize("cafe\u0301")
        assert tokens == ["cafe"]

    def test_empty_string(self):
        assert _basic_tokenize("") == []

    def test_whitespace_only(self):
        assert _basic_tokenize("   \t\n  ") == []

    def test_unicode(self):
        tokens = _basic_tokenize("Zurich is nice")
        assert "zurich" in tokens


class TestWordPieceTokenize:
    def test_known_word(self):
        vocab = _get_vocab()
        result = _wordpiece_tokenize("hello", vocab)
        assert result == ["hello"]

    def test_subword_split(self):
        vocab = _get_vocab()
        result = _wordpiece_tokenize("embeddings", vocab)
        assert len(result) >= 2
        assert result[0] == "em"
        assert all(t.startswith("##") for t in result[1:])

    def test_unknown_chars(self):
        vocab = _get_vocab()
        # Use Tibetan characters that are NOT in the BERT vocab.
        # Single ASCII letters like 'z' are in vocab and get split
        # into subword pieces, so they won't produce [UNK].
        result = _wordpiece_tokenize("\u0f00\u0f01\u0f02", vocab)
        assert "[UNK]" in result

    def test_too_long(self):
        vocab = _get_vocab()
        result = _wordpiece_tokenize("a" * 300, vocab)
        assert result == ["[UNK]"]


class TestTokenize:
    def test_output_shapes(self):
        input_ids, attn_mask, token_type_ids = tokenize("hello world", max_length=32)
        assert len(input_ids) == 1
        assert len(input_ids[0]) == 32
        assert len(attn_mask) == 1
        assert len(attn_mask[0]) == 32
        assert len(token_type_ids) == 1
        assert len(token_type_ids[0]) == 32
        assert all(isinstance(v, int) for v in input_ids[0])

    def test_cls_sep_tokens(self):
        input_ids, attn_mask, _ = tokenize("test", max_length=16)
        ids = input_ids[0]
        assert ids[0] == 101  # [CLS]
        non_pad = [i for i in ids if i != 0]
        assert non_pad[-1] == 102  # [SEP]

    def test_attention_mask(self):
        _, attn_mask, _ = tokenize("hello", max_length=16)
        mask = attn_mask[0]
        ones = sum(1 for m in mask if m == 1)
        assert ones >= 3  # [CLS], "hello", [SEP]
        assert ones < 16

    def test_padding(self):
        input_ids, attn_mask, _ = tokenize("hi", max_length=32)
        ids = input_ids[0]
        assert ids[-1] == 0
        assert attn_mask[0][-1] == 0

    def test_truncation(self):
        long_text = " ".join(["word"] * 200)
        input_ids, _, _ = tokenize(long_text, max_length=16)
        assert len(input_ids) == 1
        assert len(input_ids[0]) == 16

    def test_known_token_ids(self):
        input_ids, _, _ = tokenize("hello", max_length=8)
        ids = input_ids[0]
        vocab = _get_vocab()
        assert ids[0] == 101  # [CLS]
        assert ids[1] == vocab["hello"]
        assert ids[2] == 102  # [SEP]

    def test_token_type_ids_all_zero(self):
        _, _, token_type_ids = tokenize("test input", max_length=16)
        assert all(v == 0 for v in token_type_ids[0])


# ---------------------------------------------------------------------------
# Backward compatibility: aliases still work
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_onnx_alias_is_embedding(self):
        """OnnxEmbeddingBackend is now an alias for EmbeddingBackend."""
        assert OnnxEmbeddingBackend is EmbeddingBackend

    def test_fts5_alias_is_embedding(self):
        assert FTS5Backend is EmbeddingBackend

    def test_text_search_alias_is_embedding(self):
        assert TextSearchBackend is EmbeddingBackend


# ---------------------------------------------------------------------------
# Serialization with ONNX dimensions
# ---------------------------------------------------------------------------

class TestOnnxSerialization:
    def test_384_dim_roundtrip(self):
        vec = [float(i) / 384 for i in range(384)]
        blob = _serialize_embedding(vec)
        assert len(blob) == 384 * 4
        result = _deserialize_embedding(blob, 384)
        assert len(result) == 384
        for a, b in zip(vec, result):
            assert abs(a - b) < 1e-6
