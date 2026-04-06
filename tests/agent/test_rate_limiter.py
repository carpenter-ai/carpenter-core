"""Tests for the proactive rate limiter."""

import time
from unittest.mock import patch

from carpenter.agent import rate_limiter


class TestAcquire:
    def setup_method(self):
        rate_limiter.reset()

    def test_allows_requests_under_limit(self):
        """Requests under RPM limit are allowed immediately."""
        with patch.dict("carpenter.config.CONFIG", {"rate_limit_rpm": 10, "rate_limit_itpm": 0}):
            for _ in range(10):
                assert rate_limiter.acquire(timeout=1.0) is True

    def test_blocks_at_rpm_limit(self):
        """Requests at RPM limit time out when window is full."""
        with patch.dict("carpenter.config.CONFIG", {"rate_limit_rpm": 3, "rate_limit_itpm": 0}):
            for _ in range(3):
                assert rate_limiter.acquire(timeout=0.01) is True

            # Use minimal timeout since we just need to verify blocking
            result = rate_limiter.acquire(timeout=0.01)
            assert result is False

    def test_disabled_when_zero(self):
        """Rate limiting disabled when both limits are 0."""
        with patch.dict("carpenter.config.CONFIG", {"rate_limit_rpm": 0, "rate_limit_itpm": 0}):
            for _ in range(100):
                assert rate_limiter.acquire(timeout=0.1) is True

    def test_reset_clears_state(self):
        """Reset allows new requests after being full."""
        with patch.dict("carpenter.config.CONFIG", {"rate_limit_rpm": 2, "rate_limit_itpm": 0}):
            assert rate_limiter.acquire(timeout=1.0) is True
            assert rate_limiter.acquire(timeout=1.0) is True

            rate_limiter.reset()

            assert rate_limiter.acquire(timeout=1.0) is True
            assert rate_limiter.acquire(timeout=1.0) is True

    def test_window_slides(self):
        """Old entries expire after 60 seconds."""
        with patch.dict("carpenter.config.CONFIG", {"rate_limit_rpm": 2, "rate_limit_itpm": 0}):
            old_time = time.monotonic() - 61.0
            rate_limiter._request_times.append(old_time)
            rate_limiter._request_times.append(old_time + 0.1)

            assert rate_limiter.acquire(timeout=1.0) is True


class TestITPM:
    """Tests for input tokens per minute tracking."""

    def setup_method(self):
        rate_limiter.reset()

    def test_blocks_at_itpm_limit(self):
        """Blocks when estimated next call would exceed ITPM limit."""
        with patch.dict("carpenter.config.CONFIG", {"rate_limit_rpm": 0, "rate_limit_itpm": 10000}):
            rate_limiter.record(8000)
            # After record(8000), estimate = 0.3*1000 + 0.7*8000 = 5900
            # Window has 8000 tokens, 8000 + 5900 = 13900 > 10000 → blocked
            result = rate_limiter.acquire(timeout=0.01)
            assert result is False

    def test_allows_under_itpm_limit(self):
        """Allows requests when token usage is well under ITPM limit."""
        with patch.dict("carpenter.config.CONFIG", {"rate_limit_rpm": 0, "rate_limit_itpm": 50000}):
            rate_limiter.record(1000)
            assert rate_limiter.acquire(timeout=1.0) is True

    def test_record_updates_estimate(self):
        """record() updates the next-call estimate via EMA."""
        rate_limiter.reset()
        assert rate_limiter._next_estimate == 1000

        rate_limiter.record(5000)
        # 0.3 * 1000 + 0.7 * 5000 = 300 + 3500 = 3800
        assert rate_limiter._next_estimate == 3800

        rate_limiter.record(5000)
        # 0.3 * 3800 + 0.7 * 5000 = 1140 + 3500 = 4640
        assert rate_limiter._next_estimate == 4640

    def test_token_window_slides(self):
        """Old token entries expire after 60 seconds."""
        with patch.dict("carpenter.config.CONFIG", {"rate_limit_rpm": 0, "rate_limit_itpm": 10000}):
            old_time = time.monotonic() - 61.0
            with rate_limiter._lock:
                rate_limiter._token_entries.append((old_time, 9000))

            rate_limiter._next_estimate = 500
            assert rate_limiter.acquire(timeout=1.0) is True

    def test_itpm_and_rpm_both_enforced(self):
        """Both RPM and ITPM must be under limit for acquire to succeed."""
        with patch.dict("carpenter.config.CONFIG", {"rate_limit_rpm": 5, "rate_limit_itpm": 10000}):
            rate_limiter.record(9500)
            rate_limiter._next_estimate = 2000

            result = rate_limiter.acquire(timeout=0.01)
            assert result is False


class TestRecord429:
    def setup_method(self):
        rate_limiter.reset()

    def test_record_429_injects_partial_cooldown(self):
        """Default 75% fill leaves headroom — acquire still succeeds."""
        with patch.dict("carpenter.config.CONFIG", {
            "rate_limit_rpm": 40, "rate_limit_itpm": 10000,
            "rate_limit_429_fill_fraction": 0.75,
        }):
            rate_limiter.record_429(10.0)

            # Entries were injected
            from carpenter.agent.rate_limiter import _lock, _get_bucket
            with _lock:
                bucket = _get_bucket(None)
                assert len(bucket.request_times) > 0
                assert len(bucket.token_entries) > 0

            # But 25% headroom remains — acquire succeeds
            assert rate_limiter.acquire(timeout=0.01) is True

    def test_record_429_fills_to_default_fraction(self):
        """With RPM=40, default 75% fill injects 30 entries (75% of 40)."""
        with patch.dict("carpenter.config.CONFIG", {
            "rate_limit_rpm": 40, "rate_limit_itpm": 0,
        }):
            rate_limiter.record_429(10.0)

            from carpenter.agent.rate_limiter import _lock, _get_bucket
            with _lock:
                bucket = _get_bucket(None)
                assert len(bucket.request_times) == 30  # int(40 * 0.75)

    def test_record_429_full_fill_blocks_all(self):
        """Explicit fill_fraction=1.0 fills 100% — acquire fails."""
        with patch.dict("carpenter.config.CONFIG", {
            "rate_limit_rpm": 10, "rate_limit_itpm": 35000,
            "rate_limit_429_fill_fraction": 1.0,
        }):
            rate_limiter.record_429(10.0)
            assert rate_limiter.acquire(timeout=0.01) is False

    def test_record_429_itpm_default_headroom(self):
        """ITPM at default 75% leaves token headroom."""
        with patch.dict("carpenter.config.CONFIG", {
            "rate_limit_rpm": 0, "rate_limit_itpm": 10000,
        }):
            rate_limiter.record_429(10.0)

            from carpenter.agent.rate_limiter import _lock, _get_bucket
            with _lock:
                bucket = _get_bucket(None)
                total_tokens = sum(t for _, t in bucket.token_entries)
                assert total_tokens == 7500  # int(10000 * 0.75)


class TestUpdateFromHeaders:
    """Tests for auto-configuring limits from API response headers."""

    def setup_method(self):
        rate_limiter.reset()

    def test_updates_rpm_limit(self):
        """RPM limit updated from anthropic-ratelimit-requests-limit header."""
        rate_limiter.update_from_headers({
            "anthropic-ratelimit-requests-limit": "50",
        })
        assert rate_limiter._api_rpm_limit == 50
        # Effective limit includes headroom (95%)
        assert rate_limiter._get_rpm_limit() == 47

    def test_updates_itpm_limit(self):
        """ITPM limit updated from anthropic-ratelimit-tokens-limit header."""
        rate_limiter.update_from_headers({
            "anthropic-ratelimit-tokens-limit": "40000",
        })
        assert rate_limiter._api_itpm_limit == 40000
        assert rate_limiter._get_itpm_limit() == 38000

    def test_priority_tokens_used_when_lower(self):
        """Uses priority-input-tokens-limit when lower than tokens-limit."""
        rate_limiter.update_from_headers({
            "anthropic-ratelimit-tokens-limit": "80000",
            "anthropic-priority-input-tokens-limit": "20000",
        })
        # Should use the lower of the two
        assert rate_limiter._api_itpm_limit == 20000

    def test_tokens_limit_used_when_lower(self):
        """Uses tokens-limit when lower than priority-input-tokens-limit."""
        rate_limiter.update_from_headers({
            "anthropic-ratelimit-tokens-limit": "40000",
            "anthropic-priority-input-tokens-limit": "80000",
        })
        assert rate_limiter._api_itpm_limit == 40000

    def test_priority_tokens_alone(self):
        """Priority token limit works without regular tokens-limit."""
        rate_limiter.update_from_headers({
            "anthropic-priority-input-tokens-limit": "25000",
        })
        assert rate_limiter._api_itpm_limit == 25000

    def test_requests_remaining_trims_window(self):
        """requests-remaining corrects an over-counted RPM window."""
        # Set API limit
        rate_limiter.update_from_headers({
            "anthropic-ratelimit-requests-limit": "50",
        })
        # Artificially inflate our window (e.g. from 429 synthetic entries)
        now = time.monotonic()
        for i in range(20):
            rate_limiter._request_times.append(now + i * 0.01)

        # API says 45 remaining out of 50 → only 5 actually used
        rate_limiter.update_from_headers({
            "anthropic-ratelimit-requests-limit": "50",
            "anthropic-ratelimit-requests-remaining": "45",
        })
        # Window should be trimmed: 20 - 5 = 15 entries removed → 5 left
        assert len(rate_limiter._request_times) == 5

    def test_ignores_invalid_headers(self):
        """Invalid header values are silently ignored."""
        rate_limiter.update_from_headers({
            "anthropic-ratelimit-requests-limit": "not-a-number",
            "anthropic-ratelimit-tokens-limit": "",
        })
        assert rate_limiter._api_rpm_limit is None
        assert rate_limiter._api_itpm_limit is None

    def test_fallback_to_config_before_first_response(self):
        """Config values used when no API headers received yet."""
        with patch.dict("carpenter.config.CONFIG", {"rate_limit_rpm": 30, "rate_limit_itpm": 20000}):
            assert rate_limiter._api_rpm_limit is None
            assert rate_limiter._get_rpm_limit() == 30
            assert rate_limiter._get_itpm_limit() == 20000

    def test_api_limits_override_config(self):
        """API-reported limits take precedence over config."""
        with patch.dict("carpenter.config.CONFIG", {"rate_limit_rpm": 30, "rate_limit_itpm": 20000}):
            rate_limiter.update_from_headers({
                "anthropic-ratelimit-requests-limit": "50",
                "anthropic-ratelimit-tokens-limit": "40000",
            })
            # API values with headroom, not config values
            assert rate_limiter._get_rpm_limit() == 47
            assert rate_limiter._get_itpm_limit() == 38000

    def test_reset_clears_api_limits(self):
        """reset() clears API-reported limits back to None."""
        rate_limiter.update_from_headers({
            "anthropic-ratelimit-requests-limit": "50",
            "anthropic-ratelimit-tokens-limit": "40000",
        })
        rate_limiter.reset()
        assert rate_limiter._api_rpm_limit is None
        assert rate_limiter._api_itpm_limit is None


class TestPerModelIsolation:
    """Tests for per-model rate limit buckets."""

    def setup_method(self):
        rate_limiter.reset()

    def test_models_have_independent_rpm(self):
        """Requests on one model don't consume RPM from another."""
        with patch.dict("carpenter.config.CONFIG", {"rate_limit_rpm": 3, "rate_limit_itpm": 0}):
            # Fill haiku's RPM
            for _ in range(3):
                assert rate_limiter.acquire(timeout=1.0, model="haiku") is True

            # Haiku is full
            start = time.monotonic()
            result = rate_limiter.acquire(timeout=1.0, model="haiku")
            assert result is False

            # Sonnet still has capacity
            assert rate_limiter.acquire(timeout=1.0, model="sonnet") is True

    def test_models_have_independent_itpm(self):
        """Token usage on one model doesn't affect another."""
        with patch.dict("carpenter.config.CONFIG", {"rate_limit_rpm": 0, "rate_limit_itpm": 10000}):
            # Record heavy token usage on haiku
            rate_limiter.record(9000, model="haiku")

            # Haiku should be blocked (9000 + estimate > 10000)
            start = time.monotonic()
            result = rate_limiter.acquire(timeout=1.0, model="haiku")
            assert result is False

            # Sonnet is unaffected
            assert rate_limiter.acquire(timeout=1.0, model="sonnet") is True

    def test_headers_update_correct_model(self):
        """update_from_headers with model only affects that model's bucket."""
        rate_limiter.update_from_headers({
            "anthropic-ratelimit-requests-limit": "50",
            "anthropic-ratelimit-tokens-limit": "40000",
        }, model="haiku")

        rate_limiter.update_from_headers({
            "anthropic-ratelimit-requests-limit": "10",
            "anthropic-ratelimit-tokens-limit": "20000",
        }, model="sonnet")

        # Check haiku bucket
        from carpenter.agent.rate_limiter import _lock, _get_bucket
        with _lock:
            haiku = _get_bucket("haiku")
            sonnet = _get_bucket("sonnet")
            assert haiku.api_rpm_limit == 50
            assert haiku.api_itpm_limit == 40000
            assert sonnet.api_rpm_limit == 10
            assert sonnet.api_itpm_limit == 20000

    def test_record_429_only_affects_target_model(self):
        """record_429 fills only the specified model's bucket."""
        with patch.dict("carpenter.config.CONFIG", {"rate_limit_rpm": 40, "rate_limit_itpm": 0}):
            rate_limiter.record_429(10.0, model="sonnet")

            from carpenter.agent.rate_limiter import _lock, _get_bucket
            with _lock:
                sonnet = _get_bucket("sonnet")
                haiku = _get_bucket("haiku")
                assert len(sonnet.request_times) > 0
                assert len(haiku.request_times) == 0

    def test_reset_clears_all_models(self):
        """reset() clears state for all model buckets."""
        rate_limiter.update_from_headers({
            "anthropic-ratelimit-requests-limit": "50",
        }, model="haiku")
        rate_limiter.update_from_headers({
            "anthropic-ratelimit-requests-limit": "10",
        }, model="sonnet")

        rate_limiter.reset()

        from carpenter.agent.rate_limiter import _lock, _buckets
        with _lock:
            assert len(_buckets) == 0
