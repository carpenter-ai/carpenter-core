"""Circuit breaker for AI provider API calls.

Provides resilience against transient API failures:
- Circuit breaker pattern to fast-fail when a provider is down
- Per-provider state (each provider tracked independently)

States:
- CLOSED: Normal operation, requests pass through.
- OPEN: Provider is down, requests fail immediately with CircuitOpenError.
- HALF_OPEN: Recovery probe — one request allowed to test if provider is back.

After `failure_threshold` consecutive failures, circuit opens.
After `recovery_timeout` seconds, circuit transitions to half-open for a probe.
"""

import logging
import threading
import time

from .. import config

logger = logging.getLogger(__name__)

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a circuit breaker is open and requests are blocked."""
    pass


class CircuitBreaker:
    """Per-provider circuit breaker."""

    __slots__ = (
        "name", "failure_threshold", "recovery_timeout",
        "state", "failure_count", "last_failure_time", "_lock",
    )

    def __init__(self, name: str, failure_threshold: int = 5,
                 recovery_timeout: float = 60.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.state = CLOSED
        self.failure_count = 0
        self.last_failure_time: float = 0.0
        self._lock = threading.Lock()

    def allow_request(self) -> bool:
        """Check if a request should be allowed through."""
        with self._lock:
            if self.state == CLOSED:
                return True
            if self.state == OPEN:
                if time.monotonic() - self.last_failure_time >= self.recovery_timeout:
                    self.state = HALF_OPEN
                    logger.info("Circuit breaker [%s]: OPEN -> HALF_OPEN (probing)", self.name)
                    return True
                return False
            if self.state == HALF_OPEN:
                return True
        return False

    def record_success(self):
        """Record a successful request. Resets failure count, closes circuit."""
        with self._lock:
            if self.state != CLOSED:
                logger.info("Circuit breaker [%s]: %s -> CLOSED", self.name, self.state)
            self.failure_count = 0
            self.state = CLOSED

    def record_failure(self):
        """Record a failed request. Opens circuit after threshold."""
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.monotonic()
            if self.state == HALF_OPEN:
                self.state = OPEN
                logger.warning(
                    "Circuit breaker [%s]: HALF_OPEN -> OPEN (probe failed)", self.name)
            elif self.failure_count >= self.failure_threshold:
                if self.state != OPEN:
                    logger.warning(
                        "Circuit breaker [%s]: CLOSED -> OPEN after %d consecutive failures",
                        self.name, self.failure_count)
                self.state = OPEN


# Per-provider breakers
_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def get_breaker(provider: str) -> CircuitBreaker:
    """Get or create a circuit breaker for the given provider."""
    with _breakers_lock:
        if provider not in _breakers:
            threshold = config.CONFIG.get("circuit_breaker_threshold", 5)
            recovery = config.CONFIG.get("circuit_breaker_recovery_seconds", 60)
            _breakers[provider] = CircuitBreaker(provider, threshold, recovery)
        return _breakers[provider]


def reset():
    """Clear all circuit breakers. Used in tests."""
    with _breakers_lock:
        _breakers.clear()
