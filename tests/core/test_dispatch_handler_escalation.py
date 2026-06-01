"""Tests for escalation paths in arcs/dispatch_handler.py.

Covers the circuit-breaker and retries-exhausted escalation paths, plus
the underlying ``escalate_to_next_model`` helper extracted to
``root_failure_handler``. Regression for the bug where dispatch_handler
called ``_escalate_arc(arc_id)`` with one arg, raising ``TypeError`` at
runtime since the signature is ``_escalate_arc(arc_id, next_model)``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from carpenter.core.arcs import (
    dispatch_handler as arc_dispatch_handler,
    manager as arc_manager,
    retry as arc_retry,
)
from carpenter.core.arcs.root_failure_handler import escalate_to_next_model
from carpenter.agent.error_classifier import ErrorInfo


# ── Helper: escalate_to_next_model ──────────────────────────────────────


def test_escalate_to_next_model_returns_none_when_no_chain():
    """If get_next_model returns None, helper returns None and creates no arc."""
    arc_id = arc_manager.create_arc(
        "no-chain", goal="x", integrity_level="trusted",
    )

    with patch(
        "carpenter.agent.model_resolver.get_next_model",
        return_value=None,
    ):
        result = escalate_to_next_model(arc_id)

    assert result is None
    arc = arc_manager.get_arc(arc_id)
    # Original arc should not have been touched (still pending)
    assert arc["status"] == "pending"


def test_escalate_to_next_model_creates_sibling_with_next_model():
    """When a next model exists, helper creates an escalated sibling arc."""
    arc_id = arc_manager.create_arc(
        "needs-escalation", goal="x", integrity_level="trusted",
    )
    arc_manager.update_status(arc_id, "active")

    with patch(
        "carpenter.agent.model_resolver.get_next_model",
        return_value="anthropic:claude-opus-4-7",
    ):
        new_id = escalate_to_next_model(arc_id)

    assert new_id is not None
    assert new_id != arc_id
    new_arc = arc_manager.get_arc(new_id)
    assert new_arc is not None
    assert "escalated" in new_arc["name"]
    # Original marked as escalated
    original = arc_manager.get_arc(arc_id)
    assert original["status"] == "escalated"


def test_escalate_to_next_model_unknown_arc_returns_none():
    """Helper returns None for a missing arc id rather than raising."""
    assert escalate_to_next_model(999_999) is None


# ── dispatch_handler: circuit-breaker path ──────────────────────────────


@pytest.mark.asyncio
async def test_circuit_breaker_path_escalates_via_helper():
    """When circuit breaker is OPEN, dispatch handler escalates via helper.

    Regression: previously called ``_escalate_arc(arc_id)`` (one arg),
    which raised ``TypeError`` because the real signature requires
    ``next_model``. Now goes through ``escalate_to_next_model``.
    """
    arc_id = arc_manager.create_arc(
        "test_circuit", goal="x", integrity_level="trusted",
    )

    error_info = ErrorInfo(
        type="APIOutageError",
        retry_count=0,
        source_location="test",
        message="boom",
        model="anthropic:claude-haiku-4-5",
    )

    with patch(
        "carpenter.core.arcs.dispatch_handler._run_arc_agent",
        side_effect=Exception("boom"),
    ), patch(
        "carpenter.core.arcs.dispatch_handler._find_arc_conversation",
        return_value=1,
    ), patch(
        "carpenter.core.arcs.dispatch_handler._extract_error_info",
        return_value=error_info,
    ), patch(
        "carpenter.core.models.health.should_circuit_break",
        return_value=True,
    ), patch(
        "carpenter.core.arcs.root_failure_handler.escalate_to_next_model",
        return_value=42,
    ) as mock_escalate:
        await arc_dispatch_handler.handle_arc_dispatch(
            work_id=1, payload={"arc_id": arc_id},
        )

    mock_escalate.assert_called_once_with(arc_id)


@pytest.mark.asyncio
async def test_circuit_breaker_path_fails_arc_when_no_next_model():
    """If escalation has no next model, arc is marked failed (not crashed)."""
    arc_id = arc_manager.create_arc(
        "test_circuit_no_chain", goal="x", integrity_level="trusted",
    )

    error_info = ErrorInfo(
        type="APIOutageError",
        retry_count=0,
        source_location="test",
        message="boom",
        model="anthropic:claude-haiku-4-5",
    )

    with patch(
        "carpenter.core.arcs.dispatch_handler._run_arc_agent",
        side_effect=Exception("boom"),
    ), patch(
        "carpenter.core.arcs.dispatch_handler._find_arc_conversation",
        return_value=1,
    ), patch(
        "carpenter.core.arcs.dispatch_handler._extract_error_info",
        return_value=error_info,
    ), patch(
        "carpenter.core.models.health.should_circuit_break",
        return_value=True,
    ), patch(
        "carpenter.core.arcs.root_failure_handler.escalate_to_next_model",
        return_value=None,
    ):
        await arc_dispatch_handler.handle_arc_dispatch(
            work_id=1, payload={"arc_id": arc_id},
        )

    arc = arc_manager.get_arc(arc_id)
    assert arc["status"] == "failed"


# ── dispatch_handler: retry-exhausted path ──────────────────────────────


@pytest.mark.asyncio
async def test_retry_exhausted_with_escalate_invokes_helper():
    """When retries exhaust and decision.escalate_on_exhaust=True, helper is called."""
    arc_id = arc_manager.create_arc(
        "test_exhaust_escalate", goal="x", integrity_level="trusted",
    )
    arc_retry.initialize_retry_state(arc_id, max_retries=0)

    # Force the retry decision: not retry, escalate on exhaust.
    from carpenter.core.arcs.retry import RetryDecision

    decision = RetryDecision(
        should_retry=False,
        backoff_seconds=0.0,
        reason="exhausted (test)",
        escalate_on_exhaust=True,
    )

    with patch(
        "carpenter.core.arcs.dispatch_handler._run_arc_agent",
        side_effect=Exception("nope"),
    ), patch(
        "carpenter.core.arcs.dispatch_handler._find_arc_conversation",
        return_value=1,
    ), patch(
        "carpenter.core.models.health.should_circuit_break",
        return_value=False,
    ), patch(
        "carpenter.core.arcs.retry.should_retry_arc",
        return_value=decision,
    ), patch(
        "carpenter.core.arcs.root_failure_handler.escalate_to_next_model",
        return_value=99,
    ) as mock_escalate:
        await arc_dispatch_handler.handle_arc_dispatch(
            work_id=1, payload={"arc_id": arc_id},
        )

    mock_escalate.assert_called_once_with(arc_id)
