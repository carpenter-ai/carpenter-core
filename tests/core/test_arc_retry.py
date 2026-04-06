"""Tests for arc-level retry decision logic."""

import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.arcs import retry as arc_retry
from carpenter.agent.error_classifier import ErrorInfo
from carpenter.db import get_db


@pytest.fixture
def sample_arc():
    """Create a sample arc for testing."""
    arc_id = arc_manager.create_arc(
        name="test_arc",
        goal="Test retry logic",
        integrity_level="trusted",
    )
    return arc_id


def test_should_retry_rate_limit(sample_arc):
    """RateLimitError should retry with retry_after backoff."""
    error_info = ErrorInfo(
        type="RateLimitError",
        retry_count=1,
        source_location="test",
        message="Rate limited",
        retry_after=30.0,
    )

    decision = arc_retry.should_retry_arc(sample_arc, error_info)

    assert decision.should_retry is True
    # Backoff should be around 30s (±10% jitter)
    assert 27.0 <= decision.backoff_seconds <= 33.0
    assert decision.escalate_on_exhaust is False
    assert "RateLimitError" in decision.reason


def test_should_retry_api_outage(sample_arc):
    """APIOutageError should retry with exponential backoff."""
    error_info = ErrorInfo(
        type="APIOutageError",
        retry_count=1,
        source_location="test",
        message="Service unavailable",
    )

    decision = arc_retry.should_retry_arc(sample_arc, error_info)

    assert decision.should_retry is True
    assert decision.backoff_seconds > 0
    assert decision.escalate_on_exhaust is False  # Will be True after exhaust


def test_should_retry_network(sample_arc):
    """NetworkError should retry with capped backoff."""
    error_info = ErrorInfo(
        type="NetworkError",
        retry_count=1,
        source_location="test",
        message="Connection timeout",
    )

    decision = arc_retry.should_retry_arc(sample_arc, error_info)

    assert decision.should_retry is True
    assert decision.backoff_seconds > 0
    assert decision.backoff_seconds <= 60  # Network cap is 60s
    assert decision.escalate_on_exhaust is False


def test_should_not_retry_auth(sample_arc):
    """AuthError should not retry."""
    error_info = ErrorInfo(
        type="AuthError",
        retry_count=1,
        source_location="test",
        message="Unauthorized",
        status_code=401,
    )

    decision = arc_retry.should_retry_arc(sample_arc, error_info)

    assert decision.should_retry is False
    assert decision.backoff_seconds == 0
    assert decision.escalate_on_exhaust is False
    assert "not retriable" in decision.reason


def test_should_not_retry_model(sample_arc):
    """ModelError should not retry but should escalate."""
    error_info = ErrorInfo(
        type="ModelError",
        retry_count=1,
        source_location="test",
        message="Model not found",
        status_code=404,
    )

    decision = arc_retry.should_retry_arc(sample_arc, error_info)

    assert decision.should_retry is False
    assert decision.backoff_seconds == 0
    assert decision.escalate_on_exhaust is True
    assert "not retriable" in decision.reason


def test_should_not_retry_client(sample_arc):
    """ClientError should not retry."""
    error_info = ErrorInfo(
        type="ClientError",
        retry_count=1,
        source_location="test",
        message="Bad request",
        status_code=400,
    )

    decision = arc_retry.should_retry_arc(sample_arc, error_info)

    assert decision.should_retry is False
    assert decision.backoff_seconds == 0
    assert decision.escalate_on_exhaust is False


def test_unknown_error_limited_retry(sample_arc):
    """UnknownError should retry up to 2 times."""
    error_info = ErrorInfo(
        type="UnknownError",
        retry_count=1,
        source_location="test",
        message="Unknown error",
    )

    # First attempt should retry
    decision = arc_retry.should_retry_arc(sample_arc, error_info)
    assert decision.should_retry is True

    # Record first retry
    arc_retry.record_retry_attempt(sample_arc, error_info, decision.backoff_seconds)

    # Second attempt should still retry (count=1, max=2)
    decision2 = arc_retry.should_retry_arc(sample_arc, error_info)
    assert decision2.should_retry is True

    # Record second retry
    arc_retry.record_retry_attempt(sample_arc, error_info, decision2.backoff_seconds)

    # Third attempt should NOT retry (count=2, max=2)
    decision3 = arc_retry.should_retry_arc(sample_arc, error_info)
    assert decision3.should_retry is False
    assert "exhausted" in decision3.reason


def test_retry_count_enforcement(sample_arc):
    """Retry should stop at _max_retries."""
    # Set max_retries to 2
    arc_retry.initialize_retry_state(sample_arc, max_retries=2)

    error_info = ErrorInfo(
        type="NetworkError",
        retry_count=1,
        source_location="test",
        message="Timeout",
    )

    # First retry
    decision1 = arc_retry.should_retry_arc(sample_arc, error_info)
    assert decision1.should_retry is True
    arc_retry.record_retry_attempt(sample_arc, error_info, decision1.backoff_seconds)

    # Second retry
    decision2 = arc_retry.should_retry_arc(sample_arc, error_info)
    assert decision2.should_retry is True
    arc_retry.record_retry_attempt(sample_arc, error_info, decision2.backoff_seconds)

    # Third attempt should fail (exhausted)
    decision3 = arc_retry.should_retry_arc(sample_arc, error_info)
    assert decision3.should_retry is False
    assert "exhausted" in decision3.reason


def test_retry_count_increment(sample_arc):
    """_retry_count should increment correctly."""
    error_info = ErrorInfo(
        type="NetworkError",
        retry_count=1,
        source_location="test",
        message="Timeout",
    )

    # Record multiple retries
    for i in range(3):
        decision = arc_retry.should_retry_arc(sample_arc, error_info)
        if decision.should_retry:
            arc_retry.record_retry_attempt(sample_arc, error_info, decision.backoff_seconds)

    # Check final retry count
    state = arc_retry.get_retry_state(sample_arc)
    assert state["_retry_count"] == 3


def test_backoff_calculation():
    """Exponential backoff should be calculated correctly."""
    # RateLimitError with retry_after
    backoff1 = arc_retry.calculate_backoff("RateLimitError", 0, retry_after=30.0)
    assert backoff1 >= 27.0  # 30 - 10% jitter
    assert backoff1 <= 33.0  # 30 + 10% jitter

    # APIOutageError exponential
    backoff2 = arc_retry.calculate_backoff("APIOutageError", 0, None)
    assert backoff2 > 0 and backoff2 < 5  # 2^0 = 1, with jitter

    backoff3 = arc_retry.calculate_backoff("APIOutageError", 3, None)
    assert backoff3 > 5 and backoff3 < 15  # 2^3 = 8, with jitter

    # NetworkError with cap
    backoff4 = arc_retry.calculate_backoff("NetworkError", 10, None)
    assert backoff4 <= 66  # Cap is 60, plus 10% jitter

    # UnknownError fixed 5s
    backoff5 = arc_retry.calculate_backoff("UnknownError", 0, None)
    assert backoff5 >= 4.5 and backoff5 <= 5.5  # 5 ± 10%


def test_jitter_randomization():
    """Backoff should have jitter variance."""
    # Calculate many backoffs and check they're not all identical
    backoffs = [
        arc_retry.calculate_backoff("NetworkError", 1, None)
        for _ in range(10)
    ]

    # Should have variance due to jitter
    unique_backoffs = set(backoffs)
    assert len(unique_backoffs) > 1, "Jitter should produce varied backoffs"


def test_initialize_retry_state(sample_arc):
    """Retry state should be initialized correctly."""
    arc_retry.initialize_retry_state(
        sample_arc,
        retry_policy="aggressive",
        max_retries=5,
    )

    state = arc_retry.get_retry_state(sample_arc)
    assert state["_retry_count"] == 0
    assert state["_max_retries"] == 5
    assert state["_retry_policy"] == "aggressive"


def test_record_retry_attempt(sample_arc):
    """Retry attempt should be recorded correctly."""
    error_info = ErrorInfo(
        type="RateLimitError",
        retry_count=1,
        source_location="test",
        message="Rate limited",
        retry_after=10.0,
    )

    arc_retry.record_retry_attempt(sample_arc, error_info, backoff_seconds=15.0)

    state = arc_retry.get_retry_state(sample_arc)
    assert state["_retry_count"] == 1
    assert state["_last_error"]["error_info"]["type"] == "RateLimitError"
    assert "_last_attempt_at" in state
    assert "_backoff_until" in state
    assert "_first_error_at" in state

    # Check backoff_until is in the future
    backoff_until = datetime.fromisoformat(state["_backoff_until"])
    now = datetime.now(timezone.utc)
    assert backoff_until > now

    # Check arc_history entry
    db = get_db()
    try:
        history = db.execute(
            "SELECT * FROM arc_history WHERE arc_id = ? AND entry_type = 'retry_attempt'",
            (sample_arc,)
        ).fetchone()
        assert history is not None
        content = json.loads(history["content_json"])
        assert content["retry_count"] == 1
        assert content["error_type"] == "RateLimitError"
        assert content["backoff_seconds"] == 15.0
    finally:
        db.close()


def test_get_retry_state(sample_arc):
    """Retry state should be retrieved correctly."""
    # Initialize state
    arc_retry.initialize_retry_state(sample_arc, retry_policy="transient_only")

    # Get state
    state = arc_retry.get_retry_state(sample_arc)

    assert "_retry_count" in state
    assert "_max_retries" in state
    assert "_retry_policy" in state
    assert state["_retry_count"] == 0
    assert state["_max_retries"] == 3  # transient_only default


def test_backoff_until_timestamp(sample_arc):
    """Backoff timestamp should be set correctly."""
    error_info = ErrorInfo(
        type="NetworkError",
        retry_count=1,
        source_location="test",
        message="Timeout",
    )

    before = datetime.now(timezone.utc)
    arc_retry.record_retry_attempt(sample_arc, error_info, backoff_seconds=10.0)
    after = datetime.now(timezone.utc)

    state = arc_retry.get_retry_state(sample_arc)
    backoff_until = datetime.fromisoformat(state["_backoff_until"])

    # Should be ~10 seconds in the future
    expected_time = before + timedelta(seconds=10)
    time_diff = abs((backoff_until - expected_time).total_seconds())
    assert time_diff < 2, "Backoff timestamp should be approximately correct"


def test_retry_policy_defaults():
    """Retry policies should set correct defaults."""
    # Create arcs with different policies
    arc1 = arc_manager.create_arc(
        name="transient",
        goal="Test transient policy",
        integrity_level="trusted",
    )

    # Initialize with different policies
    arc_retry.initialize_retry_state(arc1, retry_policy="transient_only")
    state1 = arc_retry.get_retry_state(arc1)
    assert state1["_max_retries"] == 3

    arc2 = arc_manager.create_arc(
        name="aggressive",
        goal="Test aggressive policy",
        integrity_level="trusted",
    )
    arc_retry.initialize_retry_state(arc2, retry_policy="aggressive")
    state2 = arc_retry.get_retry_state(arc2)
    assert state2["_max_retries"] == 5

    arc3 = arc_manager.create_arc(
        name="conservative",
        goal="Test conservative policy",
        integrity_level="trusted",
    )
    arc_retry.initialize_retry_state(arc3, retry_policy="conservative")
    state3 = arc_retry.get_retry_state(arc3)
    assert state3["_max_retries"] == 2


def test_first_error_at_tracking(sample_arc):
    """First error timestamp should be set and preserved."""
    error_info = ErrorInfo(
        type="NetworkError",
        retry_count=1,
        source_location="test",
        message="Timeout",
    )

    # Record first retry
    arc_retry.record_retry_attempt(sample_arc, error_info, backoff_seconds=5.0)
    state1 = arc_retry.get_retry_state(sample_arc)
    first_error_at = state1["_first_error_at"]

    # Wait a bit and record second retry
    import time
    time.sleep(0.1)
    arc_retry.record_retry_attempt(sample_arc, error_info, backoff_seconds=5.0)
    state2 = arc_retry.get_retry_state(sample_arc)

    # First error timestamp should not change
    assert state2["_first_error_at"] == first_error_at


def test_last_error_storage(sample_arc):
    """Last error should be stored with full ErrorInfo."""
    error_info = ErrorInfo(
        type="RateLimitError",
        retry_count=1,
        source_location="test",
        message="Rate limited",
        status_code=429,
        retry_after=30.0,
        model="claude-sonnet-3-5-20241022",
        provider="anthropic",
        raw_error="HTTPError: 429",
    )

    arc_retry.record_retry_attempt(sample_arc, error_info, backoff_seconds=30.0)

    state = arc_retry.get_retry_state(sample_arc)
    last_error = state["_last_error"]

    assert last_error["error_info"]["type"] == "RateLimitError"
    assert last_error["error_info"]["status_code"] == 429
    assert last_error["error_info"]["retry_after"] == 30.0
    assert last_error["error_info"]["model"] == "claude-sonnet-3-5-20241022"
    assert last_error["error_info"]["provider"] == "anthropic"


def test_config_disabled_retry(sample_arc):
    """Arc retry disabled in config should set max_retries=0."""
    with patch("carpenter.config.get_config") as mock_config:
        mock_config.return_value = {"enabled": False}

        arc_retry.initialize_retry_state(sample_arc, retry_policy="aggressive")

        state = arc_retry.get_retry_state(sample_arc)
        assert state["_max_retries"] == 0


def test_escalation_policy(sample_arc):
    """Escalation policy should be set based on retry policy."""
    arc_retry.initialize_retry_state(sample_arc, retry_policy="aggressive")

    state = arc_retry.get_retry_state(sample_arc)
    assert state["_escalation_on_exhaust"] is True

    arc2 = arc_manager.create_arc(
        name="test2",
        goal="Test",
        integrity_level="trusted",
    )
    arc_retry.initialize_retry_state(arc2, retry_policy="transient_only")
    state2 = arc_retry.get_retry_state(arc2)
    assert state2["_escalation_on_exhaust"] is False


def test_multiple_errors_different_types(sample_arc):
    """Arc should handle multiple errors of different types."""
    # First error: NetworkError
    error1 = ErrorInfo(
        type="NetworkError",
        retry_count=1,
        source_location="test",
        message="Timeout",
    )
    arc_retry.record_retry_attempt(sample_arc, error1, backoff_seconds=5.0)

    # Second error: RateLimitError
    error2 = ErrorInfo(
        type="RateLimitError",
        retry_count=2,
        source_location="test",
        message="Rate limited",
        retry_after=30.0,
    )
    arc_retry.record_retry_attempt(sample_arc, error2, backoff_seconds=30.0)

    # Last error should be RateLimitError
    state = arc_retry.get_retry_state(sample_arc)
    assert state["_last_error"]["error_info"]["type"] == "RateLimitError"
    assert state["_retry_count"] == 2


def test_backoff_cap_enforcement():
    """Backoff caps should be enforced per error type."""
    # APIOutageError cap is 300s
    backoff_large = arc_retry.calculate_backoff("APIOutageError", 20, None)
    assert backoff_large <= 330  # 300 + 10% jitter

    # NetworkError cap is 60s
    backoff_network = arc_retry.calculate_backoff("NetworkError", 20, None)
    assert backoff_network <= 66  # 60 + 10% jitter

    # RateLimitError cap is 600s
    backoff_rate = arc_retry.calculate_backoff("RateLimitError", 0, retry_after=700.0)
    # RateLimitError uses max(10, retry_after), so should respect the 700
    # But config cap should apply if configured
    # For now, the implementation uses retry_after directly
    assert backoff_rate >= 630  # 700 - 10% jitter


def test_zero_retry_count_initialization(sample_arc):
    """New arcs should have retry_count=0."""
    state = arc_retry.get_retry_state(sample_arc)
    # After creation, retry count should be 0
    assert state.get("_retry_count", 0) == 0


def test_api_outage_escalation(sample_arc):
    """APIOutageError should escalate after retry exhaust."""
    arc_retry.initialize_retry_state(sample_arc, max_retries=1)

    error_info = ErrorInfo(
        type="APIOutageError",
        retry_count=1,
        source_location="test",
        message="Service unavailable",
    )

    # First attempt should retry
    decision1 = arc_retry.should_retry_arc(sample_arc, error_info)
    assert decision1.should_retry is True
    arc_retry.record_retry_attempt(sample_arc, error_info, decision1.backoff_seconds)

    # Second attempt should not retry but should escalate
    decision2 = arc_retry.should_retry_arc(sample_arc, error_info)
    assert decision2.should_retry is False
    assert decision2.escalate_on_exhaust is True


def test_waiting_arcs_recovered_on_startup():
    """Waiting arcs should be reset to pending on startup recovery.

    After a restart, arcs in 'waiting' status (retry backoff) should be
    reset to 'pending' so they get re-dispatched. Retry state in arc_state
    is preserved.
    """
    from carpenter.db import get_db, _recover_on_startup

    # Create an arc and set it to waiting (simulating retry backoff)
    arc_id = arc_manager.create_arc(
        name="waiting_test_arc",
        goal="Test waiting recovery",
        integrity_level="trusted",
    )

    # Initialize retry state with known values
    arc_retry.initialize_retry_state(arc_id, max_retries=5)

    db = get_db()
    try:
        db.execute("UPDATE arcs SET status='waiting' WHERE id=?", (arc_id,))
        # Store retry count to verify it's preserved
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) "
            "VALUES (?, '_retry_count', '2') "
            "ON CONFLICT(arc_id, key) DO UPDATE SET value_json = '2'",
            (arc_id,),
        )
        db.commit()
    finally:
        db.close()

    # Run startup recovery
    db = get_db()
    try:
        _recover_on_startup(db)
        db.commit()
    finally:
        db.close()

    # Verify arc is now pending
    db = get_db()
    try:
        row = db.execute("SELECT status FROM arcs WHERE id=?", (arc_id,)).fetchone()
        assert row["status"] == "pending"

        # Verify retry state is preserved in arc_state table
        retry_row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id=? AND key='_retry_count'",
            (arc_id,),
        ).fetchone()
        assert retry_row is not None
        assert json.loads(retry_row["value_json"]) == 2

        # Verify arc_history entry was created
        history = db.execute(
            "SELECT content_json, actor FROM arc_history "
            "WHERE arc_id=? AND entry_type='status_change' "
            "ORDER BY created_at DESC LIMIT 1",
            (arc_id,),
        ).fetchone()
        assert history is not None
        content = json.loads(history["content_json"])
        assert content["from"] == "waiting"
        assert content["to"] == "pending"
        assert "restart" in content["reason"]
        assert history["actor"] == "startup_recovery"
    finally:
        db.close()


def test_verification_error_retries(sample_arc):
    """VerificationError should retry with 0 backoff and no escalation."""
    error_info = ErrorInfo(
        type="VerificationError",
        retry_count=0,
        source_location="test",
        message="Verification failed: quality check",
    )

    # First attempt should retry
    decision = arc_retry.should_retry_arc(sample_arc, error_info)
    assert decision.should_retry is True
    # Backoff should be 0 (immediate retry) — but min is 1.0 due to max(1.0, ...)
    # Actually VerificationError has base=0.0, so with jitter it could be 0
    # but calculate_backoff returns max(1.0, ...) so it'll be >= 0
    assert decision.backoff_seconds <= 1.5  # 0 + jitter, clamped to max(1.0, ...)
    assert decision.escalate_on_exhaust is False

    arc_retry.record_retry_attempt(sample_arc, error_info, decision.backoff_seconds)

    # Second attempt should still retry
    decision2 = arc_retry.should_retry_arc(sample_arc, error_info)
    assert decision2.should_retry is True

    arc_retry.record_retry_attempt(sample_arc, error_info, decision2.backoff_seconds)

    # Third attempt should NOT retry (max=2)
    decision3 = arc_retry.should_retry_arc(sample_arc, error_info)
    assert decision3.should_retry is False
    assert "exhausted" in decision3.reason


def test_verification_error_exhaustion(sample_arc):
    """VerificationError should not escalate when exhausted."""
    arc_retry.initialize_retry_state(sample_arc, max_retries=1)

    error_info = ErrorInfo(
        type="VerificationError",
        retry_count=0,
        source_location="test",
        message="Verification failed",
    )

    # First retry
    decision1 = arc_retry.should_retry_arc(sample_arc, error_info)
    assert decision1.should_retry is True
    arc_retry.record_retry_attempt(sample_arc, error_info, decision1.backoff_seconds)

    # Second attempt — exhausted, no escalation
    decision2 = arc_retry.should_retry_arc(sample_arc, error_info)
    assert decision2.should_retry is False
    assert decision2.escalate_on_exhaust is False


def test_verification_error_zero_backoff():
    """VerificationError backoff should be minimal (0 base)."""
    backoff = arc_retry.calculate_backoff("VerificationError", 0, None)
    # base=0.0, jitter of 0 = 0, but max(1.0, 0) = 1.0
    # Actually with base=0.0, jitter = 0.0 * 0.1 * random = 0
    # So result = max(1.0, 0.0 + 0) = 1.0
    assert backoff <= 1.5


def test_non_waiting_arcs_unaffected_by_waiting_recovery():
    """Pending, completed, failed arcs should not be affected by waiting recovery."""
    from carpenter.db import get_db, _recover_on_startup

    arc_pending = arc_manager.create_arc(
        name="pending_arc", goal="Stay pending", integrity_level="trusted",
    )
    arc_failed = arc_manager.create_arc(
        name="failed_arc", goal="Stay failed", integrity_level="trusted",
    )

    db = get_db()
    try:
        db.execute("UPDATE arcs SET status='failed' WHERE id=?", (arc_failed,))
        db.commit()
    finally:
        db.close()

    # Run startup recovery
    db = get_db()
    try:
        _recover_on_startup(db)
        db.commit()
    finally:
        db.close()

    # Verify statuses unchanged
    db = get_db()
    try:
        pending_row = db.execute("SELECT status FROM arcs WHERE id=?", (arc_pending,)).fetchone()
        failed_row = db.execute("SELECT status FROM arcs WHERE id=?", (arc_failed,)).fetchone()
        assert pending_row["status"] == "pending"
        assert failed_row["status"] == "failed"
    finally:
        db.close()
