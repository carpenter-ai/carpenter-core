"""Tests for arc auto-dispatch mechanism."""

import json
import pytest
from unittest.mock import patch, AsyncMock

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import work_queue
from carpenter.core.arcs import dispatch_handler as arc_dispatch_handler
from carpenter.agent.error_classifier import ErrorInfo
from carpenter.db import get_db


def test_add_child_enqueues_if_ready():
    """add_child enqueues arc for dispatch if no predecessors."""
    parent = arc_manager.create_arc("parent", agent_type="PLANNER")
    arc_manager.update_status(parent, "active")

    # Add first child — should be enqueued immediately (no predecessors)
    child = arc_manager.add_child(parent, "child1", goal="First step")

    # Verify the child's work item was enqueued (root arc may also be enqueued)
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'arc.dispatch' "
            "AND payload_json = ? AND status = 'pending'",
            (json.dumps({"arc_id": child}),),
        ).fetchone()
    finally:
        db.close()

    assert row is not None


def test_add_child_does_not_enqueue_if_blocked():
    """add_child does not enqueue arc if predecessors are incomplete."""
    parent = arc_manager.create_arc("parent", agent_type="PLANNER")
    arc_manager.update_status(parent, "active")

    # Add two children
    child1 = arc_manager.add_child(parent, "child1", goal="First step")
    child2 = arc_manager.add_child(parent, "child2", goal="Second step")

    # Child1 should be enqueued, child2 should not (blocked by child1)
    db = get_db()
    try:
        rows = db.execute(
            "SELECT payload_json FROM work_queue WHERE event_type = 'arc.dispatch' AND status = 'pending'"
        ).fetchall()
    finally:
        db.close()

    arc_ids = [json.loads(row["payload_json"])["arc_id"] for row in rows]
    assert child1 in arc_ids
    assert child2 not in arc_ids


@pytest.mark.asyncio
async def test_handle_arc_dispatch_with_code():
    """handle_arc_dispatch executes code and enqueues ready children."""
    # Create parent with code file
    db = get_db()
    try:
        db.execute(
            "INSERT INTO code_files (id, file_path, source, review_status) VALUES (?, ?, ?, ?)",
            (100, "/tmp/test.py", "test", "approved"),
        )
        db.commit()
    finally:
        db.close()

    parent = arc_manager.create_arc("parent", code_file_id=100)
    child1 = arc_manager.add_child(parent, "child1", goal="Step 1")
    child2 = arc_manager.add_child(parent, "child2", goal="Step 2")

    # Mock code_manager.execute to simulate successful execution
    mock_result = {"execution_id": 1, "exit_code": 0, "execution_status": "success"}
    with patch("carpenter.core.code_manager.execute", return_value=mock_result):
        await arc_dispatch_handler.handle_arc_dispatch(
            work_id=1,
            payload={"arc_id": parent},
        )

    # Verify parent is now waiting (has children)
    parent_arc = arc_manager.get_arc(parent)
    assert parent_arc["status"] == "waiting"

    # Verify child1 was enqueued (no predecessors); root arc may also be in queue
    db = get_db()
    try:
        rows = db.execute(
            "SELECT payload_json FROM work_queue "
            "WHERE event_type = 'arc.dispatch' AND status = 'pending'"
        ).fetchall()
    finally:
        db.close()

    arc_ids = {json.loads(r["payload_json"])["arc_id"] for r in rows}
    assert child1 in arc_ids


def test_scan_for_ready_arcs_finds_pending():
    """scan_for_ready_arcs enqueues pending arcs with satisfied dependencies."""
    parent = arc_manager.create_arc("parent", agent_type="PLANNER")
    arc_manager.update_status(parent, "active")
    child = arc_manager.add_child(parent, "child", goal="Do work")

    # Clear any existing work queue items for this arc
    db = get_db()
    try:
        db.execute("DELETE FROM work_queue WHERE event_type = 'arc.dispatch'")
        db.commit()
    finally:
        db.close()

    # Run the heartbeat hook
    arc_dispatch_handler.scan_for_ready_arcs()

    # Verify child was enqueued
    db = get_db()
    try:
        row = db.execute(
            "SELECT payload_json FROM work_queue WHERE event_type = 'arc.dispatch'"
        ).fetchone()
    finally:
        db.close()

    assert row is not None
    payload = json.loads(row["payload_json"])
    assert payload["arc_id"] == child


def test_scan_for_ready_arcs_respects_dependencies():
    """scan_for_ready_arcs does not enqueue arcs blocked by predecessors."""
    parent = arc_manager.create_arc("parent", agent_type="PLANNER")
    arc_manager.update_status(parent, "active")
    child1 = arc_manager.add_child(parent, "child1", goal="First")
    child2 = arc_manager.add_child(parent, "child2", goal="Second")

    # Clear work queue
    db = get_db()
    try:
        db.execute("DELETE FROM work_queue WHERE event_type = 'arc.dispatch'")
        db.commit()
    finally:
        db.close()

    # Run heartbeat
    arc_dispatch_handler.scan_for_ready_arcs()

    # Only child1 should be enqueued (child2 is blocked)
    db = get_db()
    try:
        rows = db.execute(
            "SELECT payload_json FROM work_queue WHERE event_type = 'arc.dispatch'"
        ).fetchall()
    finally:
        db.close()

    arc_ids = [json.loads(row["payload_json"])["arc_id"] for row in rows]
    assert child1 in arc_ids
    assert child2 not in arc_ids


def test_heartbeat_skips_future_wait_until():
    """scan_for_ready_arcs does not enqueue arcs with a future wait_until."""
    parent = arc_manager.create_arc("parent", agent_type="PLANNER")
    arc_manager.update_status(parent, "active")
    child = arc_manager.add_child(
        parent, "delayed-child", goal="wait", wait_until="2099-12-31T23:59:59",
    )

    # Clear any existing work queue items
    db = get_db()
    try:
        db.execute("DELETE FROM work_queue WHERE event_type = 'arc.dispatch'")
        db.commit()
    finally:
        db.close()

    arc_dispatch_handler.scan_for_ready_arcs()

    # The delayed child should NOT be enqueued
    db = get_db()
    try:
        rows = db.execute(
            "SELECT payload_json FROM work_queue WHERE event_type = 'arc.dispatch'"
        ).fetchall()
    finally:
        db.close()

    arc_ids = [json.loads(row["payload_json"])["arc_id"] for row in rows]
    assert child not in arc_ids


def test_heartbeat_dispatches_past_wait_until():
    """scan_for_ready_arcs DOES enqueue arcs with a past wait_until."""
    parent = arc_manager.create_arc("parent", agent_type="PLANNER")
    arc_manager.update_status(parent, "active")
    child = arc_manager.add_child(
        parent, "ready-child", goal="go", wait_until="2000-01-01T00:00:00",
    )

    # Clear work queue
    db = get_db()
    try:
        db.execute("DELETE FROM work_queue WHERE event_type = 'arc.dispatch'")
        db.commit()
    finally:
        db.close()

    arc_dispatch_handler.scan_for_ready_arcs()

    db = get_db()
    try:
        rows = db.execute(
            "SELECT payload_json FROM work_queue WHERE event_type = 'arc.dispatch'"
        ).fetchall()
    finally:
        db.close()

    arc_ids = [json.loads(row["payload_json"])["arc_id"] for row in rows]
    assert child in arc_ids


@pytest.mark.asyncio
async def test_cron_payload_arc_id_unwrapping():
    """handle_arc_dispatch finds arc_id nested inside cron's event_payload."""
    # Create an arc to dispatch
    db = get_db()
    try:
        db.execute(
            "INSERT INTO code_files (id, file_path, source, review_status) VALUES (?, ?, ?, ?)",
            (200, "/tmp/unwrap_test.py", "pass", "approved"),
        )
        db.commit()
    finally:
        db.close()

    arc_id = arc_manager.create_arc("cron-dispatched", code_file_id=200)

    # Simulate the payload format that check_cron() produces
    cron_payload = {
        "cron_id": 1,
        "cron_name": "s006-reminder",
        "fire_time": "2026-03-18T14:30:00+00:00",
        "event_payload": {"arc_id": arc_id},
    }

    mock_result = {"execution_id": 1, "exit_code": 0, "execution_status": "success"}
    with patch("carpenter.core.code_manager.execute", return_value=mock_result):
        await arc_dispatch_handler.handle_arc_dispatch(work_id=99, payload=cron_payload)

    # Arc should have been dispatched (status should now be completed since no children)
    arc = arc_manager.get_arc(arc_id)
    assert arc["status"] == "completed"


@pytest.mark.asyncio
async def test_explicit_dispatch_works_regardless_of_wait_until():
    """handle_arc_dispatch succeeds even if arc has a future wait_until."""
    parent = arc_manager.create_arc("parent", agent_type="PLANNER")
    arc_manager.update_status(parent, "active")
    child = arc_manager.add_child(
        parent, "wait-child", goal="Send a message", wait_until="2099-12-31T23:59:59",
    )

    # Explicit dispatch should still work (the heartbeat guard is only in scan)
    with patch("carpenter.core.arcs.dispatch_handler._run_arc_agent") as mock_agent:
        await arc_dispatch_handler.handle_arc_dispatch(
            work_id=50, payload={"arc_id": child},
        )

    # Arc should transition through active → waiting/completed
    arc = arc_manager.get_arc(child)
    assert arc["status"] in ("waiting", "completed")


def test_enqueue_ready_children_after_child_completes():
    """When a child completes, the next child should be enqueued."""
    parent = arc_manager.create_arc("parent", agent_type="PLANNER")
    arc_manager.update_status(parent, "active")
    child1 = arc_manager.add_child(parent, "child1", goal="First")
    child2 = arc_manager.add_child(parent, "child2", goal="Second")

    # Complete child1
    arc_manager.update_status(child1, "active")
    arc_manager.update_status(child1, "completed")

    # Clear work queue
    db = get_db()
    try:
        db.execute("DELETE FROM work_queue WHERE event_type = 'arc.dispatch'")
        db.commit()
    finally:
        db.close()

    # Manually call enqueue_ready_children (normally called by dispatch handler)
    arc_dispatch_handler._enqueue_ready_children(parent)

    # Child2 should now be enqueued (child1 is complete)
    db = get_db()
    try:
        row = db.execute(
            "SELECT payload_json FROM work_queue WHERE event_type = 'arc.dispatch'"
        ).fetchone()
    finally:
        db.close()

    assert row is not None
    payload = json.loads(row["payload_json"])
    assert payload["arc_id"] == child2


# Arc retry integration tests


@pytest.mark.asyncio
async def test_arc_dispatch_retry_on_rate_limit():
    """Arc dispatch should retry with backoff on RateLimitError."""
    arc_id = arc_manager.create_arc("test_retry", goal="Test retry", integrity_level="trusted")

    # Mock agent invocation to raise a rate limit error
    from carpenter.agent.error_classifier import ErrorInfo

    error_info = ErrorInfo(
        type="RateLimitError",
        retry_count=1,
        source_location="test",
        message="Rate limited",
        status_code=429,
        retry_after=10.0,
    )

    mock_exception = Exception("Rate limit")
    mock_exception.status_code = 429

    with patch("carpenter.core.arcs.dispatch_handler._run_arc_agent", side_effect=mock_exception):
        with patch("carpenter.core.arcs.dispatch_handler._find_arc_conversation", return_value=1):
            await arc_dispatch_handler.handle_arc_dispatch(work_id=1, payload={"arc_id": arc_id})

    # Arc should be waiting (waiting for retry backoff)
    arc = arc_manager.get_arc(arc_id)
    assert arc["status"] == "waiting"

    # Check retry state was recorded
    from carpenter.core.arcs import retry as arc_retry
    state = arc_retry.get_retry_state(arc_id)
    assert state["_retry_count"] == 1

    # Check a new work item was enqueued with scheduled_at
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'arc.dispatch' "
            "AND json_extract(payload_json, '$.arc_id') = ? "
            "AND status = 'pending' "
            "ORDER BY created_at DESC LIMIT 1",
            (arc_id,)
        ).fetchone()
    finally:
        db.close()

    assert row is not None
    assert row["scheduled_at"] is not None


@pytest.mark.asyncio
async def test_arc_dispatch_no_retry_on_auth_error():
    """Arc dispatch should fail immediately on AuthError."""
    arc_id = arc_manager.create_arc("test_auth_fail", goal="Test auth error", integrity_level="trusted")

    # Mock agent invocation to raise an auth error
    mock_exception = Exception("Unauthorized")
    mock_exception.status_code = 401

    with patch("carpenter.core.arcs.dispatch_handler._run_arc_agent", side_effect=mock_exception):
        with patch("carpenter.core.arcs.dispatch_handler._find_arc_conversation", return_value=1):
            await arc_dispatch_handler.handle_arc_dispatch(work_id=1, payload={"arc_id": arc_id})

    # Arc should be failed (no retry)
    arc = arc_manager.get_arc(arc_id)
    assert arc["status"] == "failed"

    # Check no retry was recorded
    from carpenter.core.arcs import retry as arc_retry
    state = arc_retry.get_retry_state(arc_id)
    # May not have retry count if it failed before recording
    assert state.get("_retry_count", 0) == 0


@pytest.mark.asyncio
async def test_arc_dispatch_exhaust_retries():
    """Arc dispatch should fail after exhausting retries."""
    arc_id = arc_manager.create_arc("test_exhaust", goal="Test exhaust", integrity_level="trusted")

    # Set max retries to 1
    from carpenter.core.arcs import retry as arc_retry
    arc_retry.initialize_retry_state(arc_id, max_retries=1)

    # Mock agent invocation to raise a network error
    mock_exception = Exception("Timeout")

    with patch("carpenter.core.arcs.dispatch_handler._run_arc_agent", side_effect=mock_exception):
        with patch("carpenter.core.arcs.dispatch_handler._find_arc_conversation", return_value=1):
            # First attempt
            await arc_dispatch_handler.handle_arc_dispatch(work_id=1, payload={"arc_id": arc_id})

            # Arc should be waiting after first retry (waiting for backoff)
            arc = arc_manager.get_arc(arc_id)
            assert arc["status"] == "waiting"

            # Second attempt (should exhaust retries)
            await arc_dispatch_handler.handle_arc_dispatch(work_id=2, payload={"arc_id": arc_id})

    # Arc should be failed after exhausting retries
    arc = arc_manager.get_arc(arc_id)
    assert arc["status"] == "failed"

    # Check retry count (max_retries=1 means 1 retry attempt total)
    # Initial attempt (0) + 1 retry = 2 total attempts, retry_count=1
    state = arc_retry.get_retry_state(arc_id)
    assert state["_retry_count"] == 1


@pytest.mark.asyncio
async def test_arc_dispatch_retry_backoff_enforced():
    """Work queue should not claim items before scheduled_at time."""
    arc_id = arc_manager.create_arc("test_backoff", goal="Test backoff", integrity_level="trusted")

    # Schedule a work item 10 seconds in the future
    from datetime import datetime, timedelta, timezone
    scheduled_at = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()

    work_queue.enqueue(
        "arc.dispatch",
        {"arc_id": arc_id},
        idempotency_key=f"test_backoff_{arc_id}",
        scheduled_at=scheduled_at,
    )

    # Try to claim immediately — should return None (item not ready)
    item = work_queue.claim()
    assert item is None or item["event_type"] != "arc.dispatch" or json.loads(item["payload_json"])["arc_id"] != arc_id


@pytest.mark.asyncio
async def test_arc_dispatch_error_history_logged():
    """Retry attempts should be logged to arc_history."""
    arc_id = arc_manager.create_arc("test_history", goal="Test history", integrity_level="trusted")

    # Mock agent invocation to raise a network error
    mock_exception = Exception("Connection timeout")

    with patch("carpenter.core.arcs.dispatch_handler._run_arc_agent", side_effect=mock_exception):
        with patch("carpenter.core.arcs.dispatch_handler._find_arc_conversation", return_value=1):
            await arc_dispatch_handler.handle_arc_dispatch(work_id=1, payload={"arc_id": arc_id})

    # Check arc_history for retry attempt
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM arc_history WHERE arc_id = ? AND entry_type = 'retry_attempt'",
            (arc_id,)
        ).fetchone()
    finally:
        db.close()

    assert row is not None
    content = json.loads(row["content_json"])
    assert content["retry_count"] == 1
    assert "backoff_seconds" in content


@pytest.mark.asyncio
async def test_extract_error_info_from_messages():
    """_extract_error_info should parse ErrorInfo from conversation messages."""
    from carpenter.agent.error_classifier import ErrorInfo
    from carpenter.agent import conversation

    # Create arc and conversation
    arc_id = arc_manager.create_arc("test_extract", goal="Test extract", integrity_level="trusted")
    conv_id = conversation.create_conversation()
    conversation.link_arc_to_conversation(conv_id, arc_id)

    # Add a system message with error_info
    error_info = ErrorInfo(
        type="RateLimitError",
        retry_count=1,
        source_location="test",
        message="Rate limited",
        status_code=429,
        retry_after=30.0,
    )

    db = get_db()
    try:
        db.execute(
            "INSERT INTO messages (conversation_id, role, content, content_json) "
            "VALUES (?, 'system', ?, ?)",
            (conv_id, "Error occurred", json.dumps(error_info.to_json())),
        )
        db.commit()
    finally:
        db.close()

    # Extract error info
    extracted = arc_dispatch_handler._extract_error_info(arc_id, Exception("test"))

    assert extracted.type == "RateLimitError"
    assert extracted.status_code == 429
    assert extracted.retry_after == 30.0


# ── Provider error detection tests ──────────────────────────────


class TestIsProviderError:
    def test_network_error_is_provider_error(self):
        ei = ErrorInfo(type="NetworkError", retry_count=1, source_location="test", message="")
        assert arc_dispatch_handler._is_provider_error(ei) is True

    def test_api_outage_is_provider_error(self):
        ei = ErrorInfo(type="APIOutageError", retry_count=1, source_location="test", message="")
        assert arc_dispatch_handler._is_provider_error(ei) is True

    def test_connect_error_is_provider_error(self):
        ei = ErrorInfo(type="ConnectError", retry_count=1, source_location="test", message="")
        assert arc_dispatch_handler._is_provider_error(ei) is True

    def test_timeout_error_is_provider_error(self):
        ei = ErrorInfo(type="TimeoutError", retry_count=1, source_location="test", message="")
        assert arc_dispatch_handler._is_provider_error(ei) is True

    def test_rate_limit_is_not_provider_error(self):
        ei = ErrorInfo(type="RateLimitError", retry_count=1, source_location="test", message="")
        assert arc_dispatch_handler._is_provider_error(ei) is False

    def test_auth_error_is_not_provider_error(self):
        ei = ErrorInfo(type="AuthError", retry_count=1, source_location="test", message="")
        assert arc_dispatch_handler._is_provider_error(ei) is False

    def test_client_error_is_not_provider_error(self):
        ei = ErrorInfo(type="ClientError", retry_count=1, source_location="test", message="")
        assert arc_dispatch_handler._is_provider_error(ei) is False

    def test_unknown_error_is_not_provider_error(self):
        ei = ErrorInfo(type="UnknownError", retry_count=1, source_location="test", message="")
        assert arc_dispatch_handler._is_provider_error(ei) is False

    def test_none_is_not_provider_error(self):
        assert arc_dispatch_handler._is_provider_error(None) is False


# ── Model failover tests ────────────────────────────────────────


class TestModelFailover:
    @pytest.mark.asyncio
    async def test_fallback_succeeds_on_provider_error(self):
        """When primary model fails with provider error, fallback model succeeds."""
        from carpenter.core.models.selector import SelectionResult

        arc_id = arc_manager.create_arc(
            "test_failover", goal="Test failover", integrity_level="trusted",
        )
        # In production, the arc is already active when fallback runs
        arc_manager.update_status(arc_id, "active")

        # Create fallback models
        fallback_models = [
            SelectionResult(
                model_key="haiku",
                model_id="anthropic:claude-haiku-4-5-20251001",
                score=0.6,
                reason="fallback",
            ),
        ]

        agent_config = {
            "model": "ollama:qwen3.5:9b",
            "agent_role": None,
            "temperature": None,
            "max_tokens": None,
        }

        error_info = ErrorInfo(
            type="NetworkError", retry_count=1,
            source_location="test", message="Connection refused",
        )

        with patch("carpenter.core.arcs.dispatch_handler._run_arc_agent") as mock_agent, \
             patch("carpenter.core.arcs.dispatch_handler._find_arc_conversation", return_value=1), \
             patch("carpenter.core.arcs.dispatch_handler._propagate_completion"):
            # Fallback invocation succeeds
            mock_agent.return_value = None

            result = await arc_dispatch_handler._try_fallback_models(
                arc_id, fallback_models, agent_config, error_info,
            )

        assert result is True
        # Verify the agent was called with the fallback model
        mock_agent.assert_called_once()
        call_kwargs = mock_agent.call_args
        assert call_kwargs[1]["agent_config"]["model"] == "anthropic:claude-haiku-4-5-20251001"

    @pytest.mark.asyncio
    async def test_fallback_skips_to_next_on_provider_error(self):
        """When first fallback also fails with provider error, tries the next."""
        from carpenter.core.models.selector import SelectionResult

        arc_id = arc_manager.create_arc(
            "test_failover_chain", goal="Test chain", integrity_level="trusted",
        )
        arc_manager.update_status(arc_id, "active")

        fallback_models = [
            SelectionResult(
                model_key="sonnet",
                model_id="anthropic:claude-sonnet-4-5-20250929",
                score=0.7,
                reason="fallback-1",
            ),
            SelectionResult(
                model_key="haiku",
                model_id="anthropic:claude-haiku-4-5-20251001",
                score=0.5,
                reason="fallback-2",
            ),
        ]

        agent_config = {
            "model": "ollama:qwen3.5:9b",
            "agent_role": None,
            "temperature": None,
            "max_tokens": None,
        }

        error_info = ErrorInfo(
            type="NetworkError", retry_count=1,
            source_location="test", message="Connection refused",
        )

        call_count = 0

        async def mock_run_agent(arc_id, goal, conv_id, agent_config=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First fallback (sonnet) also fails with provider error
                raise ConnectionError("Provider unreachable")
            # Second fallback (haiku) succeeds
            return None

        with patch("carpenter.core.arcs.dispatch_handler._run_arc_agent", side_effect=mock_run_agent), \
             patch("carpenter.core.arcs.dispatch_handler._find_arc_conversation", return_value=1), \
             patch("carpenter.core.arcs.dispatch_handler._propagate_completion"):
            result = await arc_dispatch_handler._try_fallback_models(
                arc_id, fallback_models, agent_config, error_info,
            )

        assert result is True
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_fallback_stops_on_non_provider_error(self):
        """Non-provider errors stop the failover chain."""
        from carpenter.core.models.selector import SelectionResult

        arc_id = arc_manager.create_arc(
            "test_failover_stop", goal="Test stop", integrity_level="trusted",
        )
        arc_manager.update_status(arc_id, "active")

        fallback_models = [
            SelectionResult(
                model_key="sonnet",
                model_id="anthropic:claude-sonnet-4-5-20250929",
                score=0.7,
                reason="fallback-1",
            ),
            SelectionResult(
                model_key="haiku",
                model_id="anthropic:claude-haiku-4-5-20251001",
                score=0.5,
                reason="fallback-2",
            ),
        ]

        agent_config = {
            "model": "ollama:qwen3.5:9b",
            "agent_role": None,
            "temperature": None,
            "max_tokens": None,
        }

        error_info = ErrorInfo(
            type="NetworkError", retry_count=1,
            source_location="test", message="Connection refused",
        )

        # First fallback fails with auth error (not a provider error)
        auth_exc = Exception("Unauthorized")
        auth_exc.status_code = 401

        with patch("carpenter.core.arcs.dispatch_handler._run_arc_agent", side_effect=auth_exc), \
             patch("carpenter.core.arcs.dispatch_handler._find_arc_conversation", return_value=1):
            result = await arc_dispatch_handler._try_fallback_models(
                arc_id, fallback_models, agent_config, error_info,
            )

        # Should return False — auth error stops the chain
        assert result is False

    @pytest.mark.asyncio
    async def test_all_fallbacks_exhausted_returns_false(self):
        """When all fallback models fail, returns False."""
        from carpenter.core.models.selector import SelectionResult

        arc_id = arc_manager.create_arc(
            "test_all_fail", goal="Test all fail", integrity_level="trusted",
        )
        arc_manager.update_status(arc_id, "active")

        fallback_models = [
            SelectionResult(
                model_key="haiku",
                model_id="anthropic:claude-haiku-4-5-20251001",
                score=0.5,
                reason="fallback",
            ),
        ]

        agent_config = {
            "model": "ollama:qwen3.5:9b",
            "agent_role": None,
            "temperature": None,
            "max_tokens": None,
        }

        error_info = ErrorInfo(
            type="NetworkError", retry_count=1,
            source_location="test", message="Connection refused",
        )

        with patch("carpenter.core.arcs.dispatch_handler._run_arc_agent",
                    side_effect=ConnectionError("Also down")), \
             patch("carpenter.core.arcs.dispatch_handler._find_arc_conversation", return_value=1):
            result = await arc_dispatch_handler._try_fallback_models(
                arc_id, fallback_models, agent_config, error_info,
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_empty_fallbacks_returns_false(self):
        """Empty fallback list returns False immediately."""
        result = await arc_dispatch_handler._try_fallback_models(
            arc_id=1, fallback_models=[], original_agent_config={},
            original_error_info=None,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_no_agent_config_returns_false(self):
        """None agent_config returns False immediately."""
        from carpenter.core.models.selector import SelectionResult

        result = await arc_dispatch_handler._try_fallback_models(
            arc_id=1,
            fallback_models=[SelectionResult("k", "m", 0.5, "r")],
            original_agent_config=None,
            original_error_info=None,
        )
        assert result is False
