"""Tests for local model fallback in _call_with_retries."""
import json
from unittest.mock import patch, MagicMock

import pytest

from carpenter.agent import invocation
from carpenter.core.models import health as mh


# --- _try_local_fallback tests ---


def test_fallback_disabled_by_default():
    """Returns None when local_fallback.enabled is False (default)."""
    result = invocation._try_local_fallback(
        "system", [{"role": "user", "content": "hi"}],
    )
    assert result is None


def test_fallback_disabled_when_no_url(monkeypatch):
    """Returns None when enabled but no URL configured."""
    monkeypatch.setitem(
        invocation.config.CONFIG, "local_fallback",
        {"enabled": True, "url": "", "model": "test"},
    )
    result = invocation._try_local_fallback(
        "system", [{"role": "user", "content": "hi"}],
    )
    assert result is None


def test_fallback_blocked_operation(monkeypatch):
    """Returns None when operation_type is in blocked_operations."""
    monkeypatch.setitem(
        invocation.config.CONFIG, "local_fallback",
        {
            "enabled": True,
            "url": "http://localhost:11434",
            "model": "test",
            "blocked_operations": ["review"],
            "allowed_operations": [],
        },
    )
    result = invocation._try_local_fallback(
        "system", [{"role": "user", "content": "hi"}],
        operation_type="review",
    )
    assert result is None


def test_fallback_not_in_allowed_operations(monkeypatch):
    """Returns None when operation_type is not in allowed_operations."""
    monkeypatch.setitem(
        invocation.config.CONFIG, "local_fallback",
        {
            "enabled": True,
            "url": "http://localhost:11434",
            "model": "test",
            "blocked_operations": [],
            "allowed_operations": ["chat"],
        },
    )
    result = invocation._try_local_fallback(
        "system", [{"role": "user", "content": "hi"}],
        operation_type="planning",
    )
    assert result is None


def test_fallback_allowed_operation(monkeypatch):
    """Allowed operation proceeds to httpx call (mocked)."""
    monkeypatch.setitem(
        invocation.config.CONFIG, "local_fallback",
        {
            "enabled": True,
            "url": "http://localhost:11434",
            "model": "test-model",
            "blocked_operations": [],
            "allowed_operations": ["chat"],
            "context_window": 16384,
            "timeout": 30,
            "max_tokens": 1024,
        },
    )

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{
            "message": {"content": "Hello from fallback!", "role": "assistant"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "model": "test-model",
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.post", return_value=mock_response) as mock_post:
        result = invocation._try_local_fallback(
            "You are helpful.",
            [{"role": "user", "content": "hi"}],
            operation_type="chat",
        )

    assert result is not None
    # Normalized to Anthropic format
    assert result["content"][0]["type"] == "text"
    assert "Hello from fallback!" in result["content"][0]["text"]
    assert result["stop_reason"] == "end_turn"

    # Verify httpx was called with correct URL
    call_args = mock_post.call_args
    assert "localhost:11434/v1/chat/completions" in call_args.args[0]


def test_fallback_records_success_in_model_health(monkeypatch):
    """Successful fallback records in model_health with fallback: prefix."""
    monkeypatch.setitem(
        invocation.config.CONFIG, "local_fallback",
        {
            "enabled": True,
            "url": "http://localhost:11434",
            "model": "test-model",
            "blocked_operations": [],
            "allowed_operations": [],
            "context_window": 16384,
            "timeout": 30,
            "max_tokens": 1024,
        },
    )

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{
            "message": {"content": "ok", "role": "assistant"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        "model": "test-model",
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.post", return_value=mock_response):
        invocation._try_local_fallback(
            "sys", [{"role": "user", "content": "hi"}],
        )

    # Check model_health recorded the call
    state = mh.get_model_health("fallback:test-model")
    assert state.total_attempts >= 1
    assert state.success_rate > 0


def test_fallback_records_failure_in_model_health(monkeypatch):
    """Failed fallback records failure in model_health."""
    monkeypatch.setitem(
        invocation.config.CONFIG, "local_fallback",
        {
            "enabled": True,
            "url": "http://localhost:99999",
            "model": "fail-model",
            "blocked_operations": [],
            "allowed_operations": [],
            "context_window": 16384,
            "timeout": 1,
            "max_tokens": 1024,
        },
    )

    with patch("httpx.post", side_effect=Exception("Connection refused")):
        result = invocation._try_local_fallback(
            "sys", [{"role": "user", "content": "hi"}],
        )

    assert result is None

    state = mh.get_model_health("fallback:fail-model")
    assert state.consecutive_failures >= 1


def test_fallback_truncates_long_messages(monkeypatch):
    """Long message history is truncated to fit context window."""
    monkeypatch.setitem(
        invocation.config.CONFIG, "local_fallback",
        {
            "enabled": True,
            "url": "http://localhost:11434",
            "model": "test-model",
            "blocked_operations": [],
            "allowed_operations": [],
            "context_window": 100,  # Very small to force truncation
            "timeout": 30,
            "max_tokens": 1024,
        },
    )

    # Build messages that exceed the context window
    messages = [
        {"role": "user", "content": "x" * 500},
        {"role": "assistant", "content": "y" * 500},
        {"role": "user", "content": "z" * 500},
    ]

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{
            "message": {"content": "ok", "role": "assistant"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.post", return_value=mock_response) as mock_post:
        result = invocation._try_local_fallback(
            "sys", messages,
        )

    assert result is not None
    # Verify the body was truncated (fewer messages than original)
    call_args = mock_post.call_args
    body = json.loads(call_args.kwargs.get("content", call_args.args[1] if len(call_args.args) > 1 else b"{}"))
    # Should have system + at least 1 message, but fewer than original 3+system=4
    assert len(body["messages"]) < 4


# --- _call_with_retries integration with fallback ---


def test_call_with_retries_uses_fallback_on_exhaust(monkeypatch):
    """After all retries exhaust, _call_with_retries tries local fallback."""
    monkeypatch.setitem(
        invocation.config.CONFIG, "local_fallback",
        {
            "enabled": True,
            "url": "http://localhost:11434",
            "model": "fallback-model",
            "blocked_operations": [],
            "allowed_operations": ["chat"],
            "context_window": 16384,
            "timeout": 30,
            "max_tokens": 1024,
        },
    )

    # Mock the main client to always fail
    mock_client = MagicMock()
    mock_client.call.side_effect = Exception("API down")

    # Mock the fallback to succeed
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{
            "message": {"content": "Fallback response", "role": "assistant"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.post", return_value=mock_response):
        result = invocation._call_with_retries(
            "system", [{"role": "user", "content": "hi"}],
            client=mock_client,
            max_retries=1,
            operation_type="chat",
        )

    assert result is not None
    assert "_error" not in result
    assert result["content"][0]["text"] == "Fallback response"


def test_call_with_retries_skips_fallback_with_tools(monkeypatch):
    """Fallback is skipped when tools are provided (not supported)."""
    monkeypatch.setitem(
        invocation.config.CONFIG, "local_fallback",
        {
            "enabled": True,
            "url": "http://localhost:11434",
            "model": "fallback-model",
            "blocked_operations": [],
            "allowed_operations": ["chat"],
            "context_window": 16384,
            "timeout": 30,
            "max_tokens": 1024,
        },
    )

    mock_client = MagicMock()
    mock_client.call.side_effect = Exception("API down")

    with patch("httpx.post") as mock_post:
        result = invocation._call_with_retries(
            "system", [{"role": "user", "content": "hi"}],
            client=mock_client,
            max_retries=1,
            tools=[{"name": "test_tool"}],
            operation_type="chat",
        )

    # Should not have called httpx (fallback skipped due to tools)
    mock_post.assert_not_called()
    assert result is not None
    assert "_error" in result


def test_call_with_retries_no_fallback_when_disabled(monkeypatch):
    """No fallback attempt when local_fallback is disabled."""
    # Default config has it disabled
    mock_client = MagicMock()
    mock_client.call.side_effect = Exception("API down")

    with patch("httpx.post") as mock_post:
        result = invocation._call_with_retries(
            "system", [{"role": "user", "content": "hi"}],
            client=mock_client,
            max_retries=1,
            operation_type="chat",
        )

    mock_post.assert_not_called()
    assert result is not None
    assert "_error" in result


def test_call_with_retries_returns_error_when_fallback_fails(monkeypatch):
    """If fallback also fails, returns the original error."""
    monkeypatch.setitem(
        invocation.config.CONFIG, "local_fallback",
        {
            "enabled": True,
            "url": "http://localhost:11434",
            "model": "fallback-model",
            "blocked_operations": [],
            "allowed_operations": ["chat"],
            "context_window": 16384,
            "timeout": 1,
            "max_tokens": 1024,
        },
    )

    mock_client = MagicMock()
    mock_client.call.side_effect = Exception("API down")

    with patch("httpx.post", side_effect=Exception("Fallback also down")):
        result = invocation._call_with_retries(
            "system", [{"role": "user", "content": "hi"}],
            client=mock_client,
            max_retries=1,
            operation_type="chat",
        )

    assert result is not None
    assert "_error" in result


def test_operation_type_parameter_passed_through():
    """operation_type parameter is accepted without error."""
    mock_client = MagicMock()
    mock_client.call.return_value = {
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }

    result = invocation._call_with_retries(
        "system", [{"role": "user", "content": "hi"}],
        client=mock_client,
        max_retries=1,
        operation_type="summarization",
    )

    assert result is not None
    assert "_error" not in result


# --- Fast fallback (all cloud models circuit open) ---


def test_fast_fallback_skips_retries(monkeypatch):
    """When all cloud models are circuit-open, skip retries and use fallback."""
    monkeypatch.setitem(
        invocation.config.CONFIG, "local_fallback",
        {
            "enabled": True,
            "url": "http://localhost:11434",
            "model": "fallback-model",
            "blocked_operations": [],
            "allowed_operations": [],
            "context_window": 16384,
            "timeout": 30,
            "max_tokens": 1024,
        },
    )

    # Mock all_cloud_models_circuit_open to return True
    monkeypatch.setattr(
        "carpenter.core.models.health.all_cloud_models_circuit_open",
        lambda: True,
    )

    # Mock the fallback to succeed
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{
            "message": {"content": "Fast fallback!", "role": "assistant"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    mock_response.raise_for_status = MagicMock()

    # Main client should NOT be called at all
    mock_client = MagicMock()
    mock_client.call.side_effect = Exception("Should not be called")

    with patch("httpx.post", return_value=mock_response):
        result = invocation._call_with_retries(
            "system", [{"role": "user", "content": "hi"}],
            client=mock_client,
            max_retries=3,
            operation_type="chat",
        )

    assert result is not None
    assert "_error" not in result
    assert result["content"][0]["text"] == "Fast fallback!"
    # Main client should never have been called
    mock_client.call.assert_not_called()


# --- Per-arc fallback override ---


def test_arc_fallback_override_blocks(monkeypatch):
    """When arc_state has _fallback_allowed=False, fallback is blocked."""
    monkeypatch.setitem(
        invocation.config.CONFIG, "local_fallback",
        {
            "enabled": True,
            "url": "http://localhost:11434",
            "model": "test-model",
            "blocked_operations": [],
            "allowed_operations": [],
            "context_window": 16384,
            "timeout": 30,
            "max_tokens": 1024,
        },
    )

    # Insert arc_state row blocking fallback
    from carpenter.db import get_db
    db = get_db()
    try:
        # Create a test arc
        db.execute(
            "INSERT INTO arcs (id, name, goal, status) VALUES (99999, 'test', 'test', 'active')"
        )
        db.execute(
            "INSERT OR REPLACE INTO arc_state (arc_id, key, value_json) VALUES (99999, '_fallback_allowed', 'false')"
        )
        db.commit()
    finally:
        db.close()

    with patch("httpx.post") as mock_post:
        result = invocation._try_local_fallback(
            "sys", [{"role": "user", "content": "hi"}],
            arc_id=99999,
        )

    assert result is None
    mock_post.assert_not_called()

    # Cleanup
    db = get_db()
    try:
        db.execute("DELETE FROM arc_state WHERE arc_id = 99999")
        db.execute("DELETE FROM arcs WHERE id = 99999")
        db.commit()
    finally:
        db.close()


def test_arc_fallback_override_allows(monkeypatch):
    """When arc_state has _fallback_allowed=True (or absent), fallback proceeds."""
    monkeypatch.setitem(
        invocation.config.CONFIG, "local_fallback",
        {
            "enabled": True,
            "url": "http://localhost:11434",
            "model": "test-model",
            "blocked_operations": [],
            "allowed_operations": [],
            "context_window": 16384,
            "timeout": 30,
            "max_tokens": 1024,
        },
    )

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{
            "message": {"content": "Allowed!", "role": "assistant"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }
    mock_response.raise_for_status = MagicMock()

    # No arc_state row → fallback is allowed by default
    with patch("httpx.post", return_value=mock_response):
        result = invocation._try_local_fallback(
            "sys", [{"role": "user", "content": "hi"}],
            arc_id=1,  # No arc_state for this arc
        )

    assert result is not None
