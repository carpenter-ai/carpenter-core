"""Tests for carpenter.api.chat."""
import asyncio
import time
from unittest.mock import patch, MagicMock
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from carpenter.api import chat
from carpenter.core.engine import event_bus, main_loop
from carpenter.agent import conversation
from carpenter.channels.channel import get_invocation_tracker


@pytest.fixture
def client():
    app = Starlette(routes=chat.routes)
    # Reset wake signal for tests
    main_loop.wake_signal = asyncio.Event()
    # Clear any pending tasks from previous tests
    get_invocation_tracker().clear()
    return TestClient(app)


def test_chat_returns_202_immediately(client):
    """POST to chat returns 202 and fires AI in background."""
    with patch("carpenter.agent.invocation.invoke_for_chat") as mock_inv:
        mock_inv.return_value = {
            "conversation_id": 1,
            "response_text": "Hello!",
            "code": None,
            "message_id": 1,
        }
        response = client.post(
            "/api/chat",
            json={"text": "Hello", "user": "test"},
        )

    assert response.status_code == 202
    data = response.json()
    assert data["event_id"] is not None
    assert data["conversation_id"] is not None


def test_chat_saves_user_message_to_db(client):
    """User message is persisted before the response returns."""
    conv_id = conversation.get_or_create_conversation()

    with patch("carpenter.agent.invocation.invoke_for_chat") as mock_inv:
        mock_inv.return_value = {
            "conversation_id": conv_id,
            "response_text": "Hi",
            "code": None,
            "message_id": 1,
        }
        response = client.post(
            "/api/chat",
            json={"text": "Test message", "conversation_id": conv_id},
        )

    assert response.status_code == 202
    # User message should already be in DB
    messages = conversation.get_messages(conv_id)
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert any("Test message" in m["content"] for m in user_msgs)


def test_chat_records_event_in_db(client):
    """Chat message is recorded as event."""
    with patch("carpenter.agent.invocation.invoke_for_chat") as mock_inv:
        mock_inv.return_value = {
            "conversation_id": 1,
            "response_text": "Hi",
            "code": None,
            "message_id": 1,
        }
        response = client.post(
            "/api/chat",
            json={"text": "Test message"},
        )

    event_id = response.json()["event_id"]
    event = event_bus.get_event(event_id)
    assert event is not None
    assert event["event_type"] == "chat.message"


def test_chat_wakes_main_loop(client):
    """Chat message sets the wake signal."""
    with patch("carpenter.agent.invocation.invoke_for_chat") as mock_inv:
        mock_inv.return_value = {
            "conversation_id": 1,
            "response_text": "Sure",
            "code": None,
            "message_id": 1,
        }
        client.post("/api/chat", json={"text": "Wake up"})

    # Wake signal should have been set
    assert main_loop.wake_signal.is_set()


def test_chat_passes_message_already_saved(client):
    """invoke_for_chat is called with _message_already_saved=True."""
    conv_id = conversation.get_or_create_conversation()

    with patch("carpenter.agent.invocation.invoke_for_chat") as mock_inv:
        mock_inv.return_value = {
            "conversation_id": conv_id,
            "response_text": "Ok",
            "code": None,
            "message_id": 1,
        }
        client.post(
            "/api/chat",
            json={"text": "Hello", "conversation_id": conv_id},
        )

    # Give background task time to start
    time.sleep(0.1)
    mock_inv.assert_called_once()
    call_kwargs = mock_inv.call_args
    assert call_kwargs.kwargs.get("_message_already_saved") is True


def test_chat_invalid_conversation_returns_404(client):
    """POST with non-existent conversation_id returns 404."""
    response = client.post(
        "/api/chat",
        json={"text": "Hello", "conversation_id": 99999},
    )
    assert response.status_code == 404


def test_chat_pending_true_when_task_registered(client):
    """GET /api/chat/pending returns true when a task exists for the conversation."""
    conv_id = 42
    # Simulate a pending background task via InvocationTracker
    tracker = get_invocation_tracker()
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    fake_task = asyncio.ensure_future(future)
    tracker.track(conv_id, fake_task)
    try:
        resp = client.get(f"/api/chat/pending?c={conv_id}")
        assert resp.json()["pending"] is True
    finally:
        future.cancel()
        tracker.clear()


def test_chat_pending_false_when_idle(client):
    """GET /api/chat/pending returns false when no invocation is running."""
    resp = client.get("/api/chat/pending?c=1")
    assert resp.json()["pending"] is False


def test_chat_history(client):
    """GET /api/chat/history returns messages."""
    conv_id = conversation.get_or_create_conversation()
    conversation.add_message(conv_id, "user", "Hello")

    response = client.get(f"/api/chat/history?conversation_id={conv_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["conversation_id"] == conv_id
    assert isinstance(data["messages"], list)
