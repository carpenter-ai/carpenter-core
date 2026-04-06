"""Pure-Python WordPiece tokenizer for BERT-compatible models.

Implements the tokenization pipeline used by all-MiniLM-L6-v2:
lowercase, strip accents, split on whitespace/punctuation, greedy
longest-match subword tokenization using a bundled vocab.txt.

No native dependencies -- works on Android via Chaquopy.
"""

import os
import unicodedata

_VOCAB_PATH = os.path.join(os.path.dirname(__file__), "model", "vocab.txt")

# Special token IDs (standard BERT vocab)
_CLS_ID = 101   # [CLS]
_SEP_ID = 102   # [SEP]
_UNK_ID = 100   # [UNK]
_PAD_ID = 0     # [PAD]


def _load_vocab(path: str) -> dict[str, int]:
    """Load vocab.txt into a token -> id mapping."""
    vocab: dict[str, int] = {}
    with open(path, encoding="utf-8") as f:
        for idx, line in enumerate(f):
            token = line.rstrip("\n")
            vocab[token] = idx
    return vocab


# Module-level cache; loaded once on first use.
_vocab: dict[str, int] | None = None


def _get_vocab() -> dict[str, int]:
    global _vocab
    if _vocab is None:
        _vocab = _load_vocab(_VOCAB_PATH)
    return _vocab


def _strip_accents(text: str) -> str:
    """Remove combining diacritical marks (NFD normalization)."""
    output = []
    for ch in unicodedata.normalize("NFD", text):
        if unicodedata.category(ch) == "Mn":
            continue
        output.append(ch)
    return "".join(output)


def _is_punctuation(ch: str) -> bool:
    """Check if a character is punctuation (BERT definition)."""
    cp = ord(ch)
    # ASCII punctuation ranges
    if (33 <= cp <= 47) or (58 <= cp <= 64) or (91 <= cp <= 96) or (123 <= cp <= 126):
        return True
    cat = unicodedata.category(ch)
    return cat.startswith("P")


def _is_whitespace(ch: str) -> bool:
    if ch in (" ", "\t", "\n", "\r"):
        return True
    return unicodedata.category(ch) == "Zs"


def _is_control(ch: str) -> bool:
    if ch in ("\t", "\n", "\r"):
        return False
    return unicodedata.category(ch).startswith("C")


def _basic_tokenize(text: str) -> list[str]:
    """Lowercase, strip accents, split on whitespace and punctuation."""
    text = text.lower()
    text = _strip_accents(text)

    # Clean control characters and normalize whitespace
    cleaned = []
    for ch in text:
        if _is_control(ch) or ord(ch) == 0 or ord(ch) == 0xFFFD:
            continue
        if _is_whitespace(ch):
            cleaned.append(" ")
        else:
            cleaned.append(ch)
    text = "".join(cleaned)

    # Insert spaces around punctuation
    output = []
    for ch in text:
        if _is_punctuation(ch):
            output.append(" ")
            output.append(ch)
            output.append(" ")
        else:
            output.append(ch)

    return "".join(output).split()


def _wordpiece_tokenize(token: str, vocab: dict[str, int], max_chars: int = 200) -> list[str]:
    """Greedy longest-match WordPiece tokenization of a single token."""
    if len(token) > max_chars:
        return ["[UNK]"]

    sub_tokens: list[str] = []
    start = 0
    while start < len(token):
        end = len(token)
        found = False
        while start < end:
            substr = token[start:end]
            if start > 0:
                substr = "##" + substr
            if substr in vocab:
                sub_tokens.append(substr)
                found = True
                break
            end -= 1
        if not found:
            sub_tokens.append("[UNK]")
            break
        start = end
    return sub_tokens


def tokenize(
    text: str,
    max_length: int = 128,
):
    """Tokenize text for BERT-style models.

    Returns (input_ids, attention_mask, token_type_ids) as plain Python
    lists of shape [1][seq_len], suitable for ONNX Runtime inference.

    Tokens are truncated to max_length (including [CLS] and [SEP]).

    Note: numpy is NOT required -- returns nested Python lists so the
    tokenizer works on Android (Chaquopy) without numpy installed.
    """
    vocab = _get_vocab()

    basic_tokens = _basic_tokenize(text)
    wp_tokens: list[str] = []
    for token in basic_tokens:
        wp_tokens.extend(_wordpiece_tokenize(token, vocab))

    # Truncate to max_length - 2 (reserve space for [CLS] and [SEP])
    if len(wp_tokens) > max_length - 2:
        wp_tokens = wp_tokens[: max_length - 2]

    # Convert to IDs
    ids = [_CLS_ID]
    for t in wp_tokens:
        ids.append(vocab.get(t, _UNK_ID))
    ids.append(_SEP_ID)

    seq_len = len(ids)
    attention_mask = [1] * seq_len
    token_type_ids = [0] * seq_len

    # Pad to max_length
    pad_len = max_length - seq_len
    ids.extend([_PAD_ID] * pad_len)
    attention_mask.extend([0] * pad_len)
    token_type_ids.extend([0] * pad_len)

    return (
        [ids],
        [attention_mask],
        [token_type_ids],
    )
