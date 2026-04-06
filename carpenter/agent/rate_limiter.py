"""Proactive rate limiter for Anthropic API calls.

Uses per-model sliding windows to track both requests per minute (RPM)
and input tokens per minute (ITPM) and blocks before hitting the API
limit. Anthropic enforces rate limits per model tier, so Haiku and
Sonnet each get independent buckets.

Limits are auto-configured from Anthropic response headers:
- anthropic-ratelimit-requests-limit -> RPM limit
- anthropic-ratelimit-tokens-limit -> ITPM limit
- anthropic-ratelimit-requests-remaining -> remaining RPM slots
- anthropic-priority-input-tokens-limit -> priority ITPM limit (used if lower)

Config keys rate_limit_rpm and rate_limit_itpm serve as fallbacks
before the first API response is received for a given model.
"""

import logging
import threading
import time
from collections import deque

from .. import config

logger = logging.getLogger(__name__)

# Headroom fraction to leave below the API-reported limits.
# Configurable via config key "rate_limit_headroom" (default 0.95 = 5% headroom).
_HEADROOM_DEFAULT = 0.95

# Shared lock and shutdown event
_lock = threading.Lock()
_shutdown = threading.Event()

# Default bucket key when callers don't specify a model
_DEFAULT_MODEL = "_default"


class _ModelBucket:
    """Per-model rate limit state."""

    __slots__ = (
        "request_times", "token_entries", "next_estimate",
        "api_rpm_limit", "api_itpm_limit",
    )

    def __init__(self):
        self.request_times: deque[float] = deque()
        self.token_entries: deque[tuple[float, int]] = deque()
        self.next_estimate: int = 1000
        self.api_rpm_limit: int | None = None
        self.api_itpm_limit: int | None = None

    def get_rpm_limit(self) -> int:
        if self.api_rpm_limit is not None:
            headroom = config.CONFIG.get("rate_limit_headroom", _HEADROOM_DEFAULT)
            return int(self.api_rpm_limit * headroom)
        return config.CONFIG.get("rate_limit_rpm", 45)

    def get_itpm_limit(self) -> int:
        if self.api_itpm_limit is not None:
            headroom = config.CONFIG.get("rate_limit_headroom", _HEADROOM_DEFAULT)
            return int(self.api_itpm_limit * headroom)
        return config.CONFIG.get("rate_limit_itpm", 35000)

    def clean_windows(self, now: float):
        """Remove entries older than 60 seconds. Must hold _lock."""
        window_start = now - 60.0
        while self.request_times and self.request_times[0] < window_start:
            self.request_times.popleft()
        while self.token_entries and self.token_entries[0][0] < window_start:
            self.token_entries.popleft()

    def current_itpm(self) -> int:
        """Sum of input tokens in the current 60s window. Must hold _lock."""
        return sum(tokens for _, tokens in self.token_entries)


# Per-model buckets
_buckets: dict[str, _ModelBucket] = {}


def _get_bucket(model: str | None = None) -> _ModelBucket:
    """Get or create the bucket for a model. Must hold _lock."""
    key = model or _DEFAULT_MODEL
    if key not in _buckets:
        _buckets[key] = _ModelBucket()
    return _buckets[key]


# --- Backward-compatible attribute access for tests ---
# Tests access _request_times, _token_entries, _next_estimate, _lock,
# _api_rpm_limit, _api_itpm_limit, _get_rpm_limit(), _get_itpm_limit()
# on the module. These proxy to the default bucket.

@property
def _request_times_proxy():
    return _get_bucket()

# Instead of properties (which don't work at module level), we use
# a simple class to provide attribute-style access to the default bucket.


class _DefaultBucketProxy:
    """Provides module-level attribute access to the default bucket for tests.

    No locking here — callers that access internals (tests) manage their
    own locking via rate_limiter._lock.
    """

    @property
    def _request_times(self):
        return _get_bucket().request_times

    @property
    def _token_entries(self):
        return _get_bucket().token_entries

    @property
    def _next_estimate(self):
        return _get_bucket().next_estimate

    @_next_estimate.setter
    def _next_estimate(self, value):
        _get_bucket().next_estimate = value

    @property
    def _lock(self):
        return _lock

    @property
    def _api_rpm_limit(self):
        return _get_bucket().api_rpm_limit

    @_api_rpm_limit.setter
    def _api_rpm_limit(self, value):
        _get_bucket().api_rpm_limit = value

    @property
    def _api_itpm_limit(self):
        return _get_bucket().api_itpm_limit

    @_api_itpm_limit.setter
    def _api_itpm_limit(self, value):
        _get_bucket().api_itpm_limit = value

    def _get_rpm_limit(self):
        return _get_bucket().get_rpm_limit()

    def _get_itpm_limit(self):
        return _get_bucket().get_itpm_limit()


# Module-level proxy instance — tests import rate_limiter and access
# rate_limiter._request_times, etc. via this.
import sys as _sys
_module = _sys.modules[__name__]
_proxy = _DefaultBucketProxy()


def __getattr__(name):
    """Module-level __getattr__ to proxy default-bucket attributes for tests."""
    if hasattr(_proxy, name):
        return getattr(_proxy, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def update_from_headers(headers: dict, model: str | None = None):
    """Update rate limits from Anthropic API response headers.

    Parses:
    - anthropic-ratelimit-requests-limit -> RPM limit
    - anthropic-ratelimit-tokens-limit -> ITPM limit
    - anthropic-ratelimit-requests-remaining -> correct RPM window
    - anthropic-priority-input-tokens-limit -> priority ITPM (use if lower)

    Args:
        headers: Response headers (httpx Headers or dict-like).
        model: Model name to update limits for (e.g. "claude-haiku-4-5-20251001").
    """
    rpm_limit = headers.get("anthropic-ratelimit-requests-limit")
    itpm_limit = headers.get("anthropic-ratelimit-tokens-limit")
    priority_itpm = headers.get("anthropic-priority-input-tokens-limit")
    rpm_remaining = headers.get("anthropic-ratelimit-requests-remaining")

    changed = False
    with _lock:
        bucket = _get_bucket(model)

        if rpm_limit is not None:
            try:
                new_rpm = int(rpm_limit)
                if bucket.api_rpm_limit != new_rpm:
                    bucket.api_rpm_limit = new_rpm
                    changed = True
            except (ValueError, TypeError):
                pass

        # Use the most restrictive of tokens-limit and priority-input-tokens-limit
        candidates = []
        if itpm_limit is not None:
            try:
                candidates.append(int(itpm_limit))
            except (ValueError, TypeError):
                pass
        if priority_itpm is not None:
            try:
                candidates.append(int(priority_itpm))
            except (ValueError, TypeError):
                pass
        if candidates:
            new_itpm = min(candidates)
            if bucket.api_itpm_limit != new_itpm:
                bucket.api_itpm_limit = new_itpm
                changed = True

        # Use requests-remaining to correct our RPM window if it drifted.
        if rpm_remaining is not None and bucket.api_rpm_limit is not None:
            try:
                remaining = int(rpm_remaining)
                api_used = bucket.api_rpm_limit - remaining
                our_count = len(bucket.request_times)
                if our_count > api_used + 2:
                    trim = our_count - api_used
                    for _ in range(trim):
                        if bucket.request_times:
                            bucket.request_times.popleft()
            except (ValueError, TypeError):
                pass

    if changed:
        with _lock:
            b = _get_bucket(model)
            rpm_eff = b.get_rpm_limit()
            itpm_eff = b.get_itpm_limit()
        logger.info(
            "Rate limits updated from API [%s]: RPM=%s (effective %d), "
            "ITPM=%s (effective %d)",
            model or "default",
            b.api_rpm_limit, rpm_eff,
            b.api_itpm_limit, itpm_eff,
        )


def acquire(timeout: float = 120.0, model: str | None = None) -> bool:
    """Block until a request slot is available within both RPM and ITPM windows.

    Uses the per-model bucket for the given model. If model is None,
    uses the default bucket.

    Args:
        timeout: Maximum seconds to wait for a slot. Returns False if exceeded.
        model: Model name for per-model rate limiting.

    Returns:
        True if a slot was acquired, False if timed out.
    """
    deadline = time.monotonic() + timeout

    with _lock:
        bucket = _get_bucket(model)
        rpm_limit = bucket.get_rpm_limit()
        itpm_limit = bucket.get_itpm_limit()

    if rpm_limit <= 0 and itpm_limit <= 0:
        return True

    while True:
        if _shutdown.is_set():
            return False

        now = time.monotonic()
        if now >= deadline:
            logger.error("Rate limiter timed out after %.0fs [%s]",
                         timeout, model or "default")
            return False

        with _lock:
            bucket = _get_bucket(model)
            # Re-read limits in case headers updated them
            rpm_limit = bucket.get_rpm_limit()
            itpm_limit = bucket.get_itpm_limit()

            bucket.clean_windows(now)

            rpm_ok = rpm_limit <= 0 or len(bucket.request_times) < rpm_limit
            current_tokens = bucket.current_itpm()
            itpm_ok = itpm_limit <= 0 or (current_tokens + bucket.next_estimate) <= itpm_limit

            if rpm_ok and itpm_ok:
                bucket.request_times.append(now)
                used_pct = max(
                    len(bucket.request_times) / max(rpm_limit, 1) if rpm_limit > 0 else 0,
                    current_tokens / max(itpm_limit, 1) if itpm_limit > 0 else 0,
                )
                if used_pct > 0.7:
                    logger.debug(
                        "Rate limiter [%s]: RPM %d/%d, ITPM %d/%d (est next: %d)",
                        model or "default",
                        len(bucket.request_times), rpm_limit,
                        current_tokens, itpm_limit, bucket.next_estimate,
                    )
                return True

            # Calculate wait time based on which limit is hit
            wait = 0.0
            if not rpm_ok and bucket.request_times:
                oldest = bucket.request_times[0]
                wait = max(wait, oldest + 60.0 - now + 0.1)
            if not itpm_ok and bucket.token_entries:
                needed = (current_tokens + bucket.next_estimate) - itpm_limit
                accumulated = 0
                for ts, tokens in bucket.token_entries:
                    accumulated += tokens
                    if accumulated >= needed:
                        wait = max(wait, ts + 60.0 - now + 0.1)
                        break

            wait = max(wait, 1.0)

        reason = []
        if not rpm_ok:
            reason.append(f"RPM {len(bucket.request_times)}/{rpm_limit}")
        if not itpm_ok:
            reason.append(f"ITPM {current_tokens}/{itpm_limit} (est +{bucket.next_estimate})")

        logger.info(
            "Rate limiter [%s]: %s, waiting %.1fs",
            model or "default", ", ".join(reason), wait,
        )
        sleep_time = min(wait, max(0, deadline - time.monotonic()))
        if _shutdown.wait(timeout=sleep_time):
            return False


def record(input_tokens: int, model: str | None = None):
    """Record actual token usage from a completed API call.

    Updates the per-model ITPM sliding window and adjusts the estimate
    for the next request using an exponential moving average.

    Args:
        input_tokens: Number of input tokens from the API response.
        model: Model name for per-model tracking.
    """
    with _lock:
        bucket = _get_bucket(model)
        now = time.monotonic()
        bucket.token_entries.append((now, input_tokens))
        bucket.next_estimate = int(0.3 * bucket.next_estimate + 0.7 * input_tokens)
        bucket.clean_windows(now)


def record_429(retry_after: float = 5.0, model: str | None = None):
    """Record a 429 response to dynamically slow down future requests.

    Fills both windows to a configurable fraction of capacity (default 75%)
    to create a cooldown while leaving headroom for chat and notifications.
    The fraction is controlled by the ``rate_limit_429_fill_fraction`` config
    key (0.0–1.0).
    """
    fill_fraction = config.CONFIG.get("rate_limit_429_fill_fraction", 0.75)
    with _lock:
        bucket = _get_bucket(model)
        now = time.monotonic()
        # Fill RPM window to target fraction of capacity
        rpm_limit = bucket.get_rpm_limit()
        if rpm_limit > 0:
            current_rpm = len(bucket.request_times)
            target = int(rpm_limit * fill_fraction)
            fill = max(0, target - current_rpm)
            for i in range(fill):
                bucket.request_times.append(now + i * 0.01)
        # Fill ITPM window to target fraction of capacity
        itpm_limit = bucket.get_itpm_limit()
        synthetic_tokens = 0
        if itpm_limit > 0:
            current_tokens = bucket.current_itpm()
            target_tokens = int(itpm_limit * fill_fraction)
            synthetic_tokens = max(0, target_tokens - current_tokens)
            if synthetic_tokens > 0:
                bucket.token_entries.append((now, synthetic_tokens))
    logger.info(
        "Rate limiter [%s]: 429 received, filled to %.0f%% capacity "
        "(retry_after=%.1fs, synthetic_tokens=%d)",
        model or "default", fill_fraction * 100, retry_after, synthetic_tokens,
    )


def shutdown():
    """Signal the rate limiter to stop blocking. Called during server shutdown."""
    _shutdown.set()


def reset():
    """Clear all rate limiter state (all models). Used in tests."""
    with _lock:
        _buckets.clear()
    _shutdown.clear()
