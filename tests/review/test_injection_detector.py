"""Tests for prompt injection detector (carpenter/review/injection_detector.py)."""

from unittest.mock import patch, call as mock_call

import pytest

from carpenter.review.injection_detector import (
    WINDOW_SIZE,
    WORDS_PER_SESSION_MIN,
    MAX_WORDS,
    WindowVerdict,
    TextReviewDecision,
    _make_windows,
    _session_count,
    _partition_with_offsets,
    _parse_verdict,
    _aggregate,
    _resolve_window_model,
    review_unstructured_text,
)
from carpenter import config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def text_review_config(test_db, monkeypatch):
    """Set up config with a model for text_window_review."""
    current = config.CONFIG.copy()
    current["model_roles"] = {"text_window_review": "anthropic:claude-haiku-test"}
    current["claude_api_key"] = "test-key"
    monkeypatch.setattr(config, "CONFIG", current)


@pytest.fixture
def mock_window_ai():
    """Patch claude_client.call to return SAFE by default."""
    safe_response = {"content": [{"type": "text", "text": "SAFE"}]}
    with (
        patch("carpenter.agent.providers.anthropic.call", return_value=safe_response) as mock_c,
        patch("carpenter.agent.rate_limiter.acquire", return_value=True),
        patch("carpenter.agent.rate_limiter.update_from_headers"),
        patch("carpenter.agent.rate_limiter.record"),
    ):
        yield mock_c


def _suspicious_response():
    return {"content": [{"type": "text", "text": "SUSPICIOUS"}]}


def _unclear_response():
    return {"content": [{"type": "text", "text": "UNCLEAR"}]}


def _make_words(n: int) -> list[str]:
    """Generate a list of n distinct words."""
    return [f"word{i}" for i in range(n)]


# ---------------------------------------------------------------------------
# _make_windows
# ---------------------------------------------------------------------------

class TestMakeWindows:
    def test_exact_multiple(self):
        words = _make_words(30)
        windows = _make_windows(words, size=10)
        assert len(windows) == 3
        assert windows[0] == " ".join(words[:10])
        assert windows[1] == " ".join(words[10:20])
        assert windows[2] == " ".join(words[20:30])

    def test_with_remainder(self):
        words = _make_words(25)
        windows = _make_windows(words, size=10)
        assert len(windows) == 3
        assert len(windows[2].split()) == 5  # 25 - 20 = 5 words in last window

    def test_fewer_words_than_size(self):
        words = _make_words(5)
        windows = _make_windows(words, size=10)
        assert len(windows) == 1
        assert windows[0] == " ".join(words)

    def test_empty(self):
        assert _make_windows([], size=10) == []

    def test_exactly_one_window(self):
        words = _make_words(10)
        windows = _make_windows(words, size=10)
        assert len(windows) == 1

    def test_hard_split_no_sentence_awareness(self):
        # Words are split strictly on count, not on punctuation
        words = "The quick brown fox jumps over the lazy dog always".split()
        windows = _make_windows(words, size=5)
        assert len(windows) == 2
        assert windows[0] == "The quick brown fox jumps"
        assert windows[1] == "over the lazy dog always"


# ---------------------------------------------------------------------------
# _session_count
# ---------------------------------------------------------------------------

class TestSessionCount:
    def test_single_session_small_text(self):
        # 40 words < WORDS_PER_SESSION_MIN (80) → 1 session
        assert _session_count(40, 5) == 1

    def test_single_session_exact_minimum(self):
        assert _session_count(WORDS_PER_SESSION_MIN, 5) == 1

    def test_two_sessions(self):
        assert _session_count(WORDS_PER_SESSION_MIN * 2, 5) == 2

    def test_capped_at_max_concurrent(self):
        assert _session_count(1000, 5) == 5

    def test_capped_at_max_concurrent_small_limit(self):
        assert _session_count(1000, 2) == 2

    def test_minimum_one_session(self):
        assert _session_count(1, 5) == 1
        assert _session_count(0, 5) == 1  # max(1, ...) ensures at least 1


# ---------------------------------------------------------------------------
# _partition_with_offsets
# ---------------------------------------------------------------------------

class TestPartitionWithOffsets:
    def test_two_sessions_basic(self):
        words = _make_words(200)
        partitions = _partition_with_offsets(words, n_sessions=2)
        assert len(partitions) == 2

        chunk0, offset0 = partitions[0]
        chunk1, offset1 = partitions[1]

        # Base chunks are 100 words each
        assert offset0 == 0
        assert offset1 == 100

        # Non-last session gets WINDOW_SIZE overlap
        assert len(chunk0) == 100 + WINDOW_SIZE
        # Overlap words are the first WINDOW_SIZE words of session 1's base chunk
        assert chunk0[-WINDOW_SIZE:] == words[100:100 + WINDOW_SIZE]

        # Last session has no overlap
        assert len(chunk1) == 100
        assert chunk1 == words[100:200]

    def test_single_session_no_overlap(self):
        words = _make_words(100)
        partitions = _partition_with_offsets(words, n_sessions=1)
        assert len(partitions) == 1
        chunk, offset = partitions[0]
        assert offset == 0
        assert chunk == words  # no overlap for single session

    def test_three_sessions_offsets(self):
        words = _make_words(300)
        partitions = _partition_with_offsets(words, n_sessions=3)
        _, offset0 = partitions[0]
        _, offset1 = partitions[1]
        _, offset2 = partitions[2]
        assert offset0 == 0
        assert offset1 == 100
        assert offset2 == 200

    def test_overlap_does_not_exceed_word_list(self):
        # Edge: last non-last session's overlap should stop at end of words
        words = _make_words(82)  # just above 80, splits into 2 sessions
        partitions = _partition_with_offsets(words, n_sessions=2)
        chunk0, _ = partitions[0]
        # chunk0 should not exceed the word list
        assert len(chunk0) <= len(words)

    def test_remainder_distribution(self):
        # 205 words, 2 sessions: session 0 gets 103, session 1 gets 102
        words = _make_words(205)
        partitions = _partition_with_offsets(words, n_sessions=2)
        _, offset0 = partitions[0]
        _, offset1 = partitions[1]
        assert offset0 == 0
        assert offset1 == 103  # ceil(205/2) = 103


# ---------------------------------------------------------------------------
# _parse_verdict
# ---------------------------------------------------------------------------

class TestParseVerdict:
    def test_safe(self):
        assert _parse_verdict("SAFE") == "SAFE"

    def test_safe_lowercase(self):
        assert _parse_verdict("safe") == "SAFE"

    def test_suspicious(self):
        assert _parse_verdict("SUSPICIOUS") == "SUSPICIOUS"

    def test_suspicious_with_explanation(self):
        # Model ignored instructions and added text
        assert _parse_verdict("SUSPICIOUS: this looks like an override") == "SUSPICIOUS"

    def test_unclear(self):
        assert _parse_verdict("UNCLEAR") == "UNCLEAR"

    def test_unknown_defaults_to_unclear(self):
        assert _parse_verdict("I'm not sure about this") == "UNCLEAR"

    def test_empty_defaults_to_unclear(self):
        assert _parse_verdict("") == "UNCLEAR"

    def test_suspicious_beats_safe_in_same_line(self):
        # Should not occur in practice but SUSPICIOUS takes priority
        assert _parse_verdict("SUSPICIOUS SAFE") == "SUSPICIOUS"

    def test_multiline_uses_first_line(self):
        assert _parse_verdict("SAFE\nSUSPICIOUS on reflection") == "SAFE"


# ---------------------------------------------------------------------------
# _aggregate
# ---------------------------------------------------------------------------

class TestAggregate:
    def _verdict(self, v: str, word_start: int = 0) -> WindowVerdict:
        return WindowVerdict(
            session_idx=0,
            word_start=word_start,
            verdict=v,
            text_excerpt="some text",
        )

    def test_all_safe(self):
        verdicts = [self._verdict("SAFE", i * 10) for i in range(5)]
        decision = _aggregate(verdicts)
        assert not decision.escalate
        assert decision.flagged_windows == []

    def test_one_suspicious(self):
        verdicts = [
            self._verdict("SAFE", 0),
            self._verdict("SUSPICIOUS", 10),
            self._verdict("SAFE", 20),
        ]
        decision = _aggregate(verdicts)
        assert decision.escalate
        assert len(decision.flagged_windows) == 1
        assert decision.flagged_windows[0].verdict == "SUSPICIOUS"

    def test_one_unclear(self):
        verdicts = [self._verdict("UNCLEAR", 0)]
        decision = _aggregate(verdicts)
        assert decision.escalate
        assert decision.flagged_windows[0].verdict == "UNCLEAR"

    def test_multiple_flagged(self):
        verdicts = [
            self._verdict("SUSPICIOUS", 0),
            self._verdict("SAFE", 10),
            self._verdict("UNCLEAR", 20),
        ]
        decision = _aggregate(verdicts)
        assert decision.escalate
        assert len(decision.flagged_windows) == 2

    def test_reason_names_first_flagged_window(self):
        verdicts = [
            self._verdict("SAFE", 0),
            self._verdict("SUSPICIOUS", 30),
        ]
        decision = _aggregate(verdicts)
        assert "31" in decision.reason  # word_start=30 → reported as word 31

    def test_empty_input(self):
        decision = _aggregate([])
        assert not decision.escalate


# ---------------------------------------------------------------------------
# _resolve_window_model
# ---------------------------------------------------------------------------

class TestResolveWindowModel:
    """Model resolution uses cheapest configured model by default."""

    def test_explicit_role_wins(self, monkeypatch):
        current = config.CONFIG.copy()
        current["model_roles"] = {"text_window_review": "anthropic:explicit-haiku"}
        current["models"] = {
            "cheap": {"provider": "anthropic", "model_id": "cheap-model", "cost_tier": "low"},
        }
        monkeypatch.setattr(config, "CONFIG", current)
        assert _resolve_window_model() == "anthropic:explicit-haiku"

    def test_cheapest_model_used_when_no_explicit_role(self, monkeypatch):
        current = config.CONFIG.copy()
        current["model_roles"] = {}  # no explicit text_window_review
        current["models"] = {
            "expensive": {"provider": "anthropic", "model_id": "opus", "cost_tier": "high"},
            "cheap":     {"provider": "anthropic", "model_id": "haiku", "cost_tier": "low"},
            "medium":    {"provider": "anthropic", "model_id": "sonnet", "cost_tier": "medium"},
        }
        monkeypatch.setattr(config, "CONFIG", current)
        assert _resolve_window_model() == "anthropic:haiku"

    def test_cheapest_among_equals_is_stable(self, monkeypatch):
        current = config.CONFIG.copy()
        current["model_roles"] = {}
        current["models"] = {
            "a": {"provider": "anthropic", "model_id": "model-a", "cost_tier": "low"},
            "b": {"provider": "anthropic", "model_id": "model-b", "cost_tier": "low"},
        }
        monkeypatch.setattr(config, "CONFIG", current)
        result = _resolve_window_model()
        assert result in {"anthropic:model-a", "anthropic:model-b"}

    def test_falls_back_to_default_when_no_manifest(self, monkeypatch):
        current = config.CONFIG.copy()
        current["model_roles"] = {"default": "anthropic:default-model"}
        current.pop("models", None)
        monkeypatch.setattr(config, "CONFIG", current)
        result = _resolve_window_model()
        assert result == "anthropic:default-model"

    def test_returns_none_when_nothing_configured(self, monkeypatch):
        current = config.CONFIG.copy()
        current.pop("model_roles", None)
        current.pop("models", None)
        current.pop("ai_provider", None)
        monkeypatch.setattr(config, "CONFIG", current)
        with patch(
            "carpenter.agent.model_resolver.get_model_for_role",
            side_effect=ValueError("no model"),
        ):
            assert _resolve_window_model() is None


# ---------------------------------------------------------------------------
# review_unstructured_text — integration (mocked API)
# ---------------------------------------------------------------------------

class TestReviewUnstructuredText:
    def test_empty_texts_no_api_calls(self, mock_window_ai):
        decision = review_unstructured_text([])
        assert not decision.escalate
        mock_window_ai.assert_not_called()

    def test_empty_string_no_api_calls(self, mock_window_ai):
        decision = review_unstructured_text([""])
        assert not decision.escalate
        mock_window_ai.assert_not_called()

    def test_all_safe_no_escalation(self, mock_window_ai):
        words = " ".join(_make_words(20))
        decision = review_unstructured_text([words])
        assert not decision.escalate
        assert mock_window_ai.call_count == 2  # 20 words / 10 = 2 windows

    def test_suspicious_response_escalates(self, mock_window_ai):
        mock_window_ai.return_value = _suspicious_response()
        words = " ".join(_make_words(10))
        decision = review_unstructured_text([words])
        assert decision.escalate
        assert len(decision.flagged_windows) == 1
        assert decision.flagged_windows[0].verdict == "SUSPICIOUS"

    def test_unclear_response_escalates(self, mock_window_ai):
        mock_window_ai.return_value = _unclear_response()
        words = " ".join(_make_words(10))
        decision = review_unstructured_text([words])
        assert decision.escalate

    def test_max_words_exceeded_no_api_calls(self, mock_window_ai):
        words = " ".join(_make_words(MAX_WORDS + 1))
        decision = review_unstructured_text([words])
        assert decision.escalate
        assert str(MAX_WORDS) in decision.reason
        mock_window_ai.assert_not_called()

    def test_max_words_exact_limit_not_exceeded(self, mock_window_ai):
        words = " ".join(_make_words(MAX_WORDS))
        decision = review_unstructured_text([words])
        # Exactly at the limit: should NOT trigger the immediate escalation path
        # (it goes through the normal window review)
        mock_window_ai.assert_called()

    def test_multiple_texts_concatenated(self, mock_window_ai):
        # Two texts of 5 words each → 10 words → 1 window → 1 API call
        decision = review_unstructured_text(["hello world foo bar baz", "one two three four five"])
        assert mock_window_ai.call_count == 1

    def test_nfkc_normalization_applied(self, mock_window_ai):
        # Fullwidth ASCII 'ａ' (U+FF41) normalizes to 'a' — won't raise
        decision = review_unstructured_text(["ａｂｃ ｄｅｆ"])
        assert not decision.escalate

    def test_mixed_safe_and_suspicious(self, mock_window_ai):
        # First call SAFE, second call SUSPICIOUS
        mock_window_ai.side_effect = [
            {"content": [{"type": "text", "text": "SAFE"}]},
            {"content": [{"type": "text", "text": "SUSPICIOUS"}]},
        ]
        words = " ".join(_make_words(20))
        decision = review_unstructured_text([words])
        assert decision.escalate

    def test_api_exception_treated_as_unclear(self, mock_window_ai):
        mock_window_ai.side_effect = RuntimeError("API down")
        words = " ".join(_make_words(10))
        decision = review_unstructured_text([words])
        assert decision.escalate  # UNCLEAR from exception → escalate

    def test_session_count_respected(self, mock_window_ai, monkeypatch):
        # With max_concurrent_sessions=1 and 200 words, should use 2 sessions
        # but cap at 1 → 1 session, so 20 API calls (200 / 10 = 20 windows)
        current = config.CONFIG.copy()
        current["review"] = {"progressive_text_review": {"max_concurrent_sessions": 1}}
        current["model_roles"] = {"text_window_review": "anthropic:claude-haiku-test"}
        current["claude_api_key"] = "test-key"
        monkeypatch.setattr(config, "CONFIG", current)

        words = " ".join(_make_words(200))
        decision = review_unstructured_text([words])
        assert not decision.escalate
        # 200 words / 10 = 20 windows; _session_count(200, 1) = 1 session
        # 1 session of 200 words = 20 windows + overlap (none, single session)
        assert mock_window_ai.call_count == 20

    def test_no_model_configured_skips_gracefully(self, monkeypatch):
        # Clear all model configuration so _resolve_window_model returns None
        current = config.CONFIG.copy()
        current.pop("model_roles", None)
        current.pop("models", None)       # clears cheapest-model fallback
        current.pop("ai_provider", None)
        current.pop("claude_api_key", None)
        monkeypatch.setattr(config, "CONFIG", current)
        # Patch get_model_for_role to raise ValueError (clears final fallback)
        with patch(
            "carpenter.agent.model_resolver.get_model_for_role",
            side_effect=ValueError("no model"),
        ):
            decision = review_unstructured_text(["some text here"])
        # Should not escalate — fail open when model is not configured
        assert not decision.escalate
