"""Tests for carpenter.api.ui."""
from starlette.testclient import TestClient

from carpenter.api.http import create_app
from carpenter.agent import conversation


def _client():
    return TestClient(create_app())


def test_root_returns_chat_page():
    """GET / returns 200 with the HTMX chat page."""
    client = _client()
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "htmx" in body.lower()
    assert "chat-input" in body


def test_root_with_c_scopes_to_conversation():
    """GET /?c=<id> scopes the page to that conversation."""
    conv_id = conversation.create_conversation()
    client = _client()
    response = client.get(f"/?c={conv_id}")
    assert response.status_code == 200
    body = response.text
    assert f"CONV_ID = {conv_id}" in body
    assert f"c={conv_id}" in body  # poll URL includes conversation


def test_root_bad_c_falls_back():
    """GET /?c=bad falls back to the last active conversation."""
    client = _client()
    response = client.get("/?c=notanumber")
    assert response.status_code == 200
    body = response.text
    assert "CONV_ID" in body


def test_new_creates_and_redirects():
    """GET /new creates a conversation and 302-redirects."""
    client = _client()
    response = client.get("/new", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert "/?c=" in location


def test_new_with_token_preserves_token():
    """GET /new?token=xyz includes token in redirect URL."""
    client = _client()
    response = client.get("/new?token=xyz", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert "token=xyz" in location


def test_conversations_list_endpoint():
    """GET /api/conversations returns JSON list."""
    conv_id = conversation.create_conversation()
    conversation.set_conversation_title(conv_id, "Test Conv")
    conversation.add_message(conv_id, "user", "Hello")

    client = _client()
    response = client.get("/api/conversations")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert any(c["id"] == conv_id for c in data)


def test_archive_endpoint():
    """POST /api/conversations/<id>/archive archives the conversation."""
    conv_id = conversation.create_conversation()

    client = _client()
    response = client.post(f"/api/conversations/{conv_id}/archive")
    assert response.status_code == 200
    assert response.json()["ok"] is True

    conv = conversation.get_conversation(conv_id)
    assert conv["archived"] == 1


def test_messages_accepts_c_param():
    """GET /api/chat/messages?c=<id> uses that conversation."""
    conv_id = conversation.create_conversation()
    conversation.add_message(conv_id, "user", "Unique message here")

    client = _client()
    response = client.get(f"/api/chat/messages?c={conv_id}")
    assert response.status_code == 200
    assert "Unique message here" in response.text


def test_messages_endpoint_returns_html():
    """GET /api/chat/messages returns 200 with HTML content."""
    client = _client()
    response = client.get("/api/chat/messages")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_messages_empty_conversation():
    """GET /api/chat/messages with no messages returns empty content."""
    client = _client()
    response = client.get("/api/chat/messages")
    assert response.status_code == 200
    # No messages have been added, so the body should be empty
    assert response.text.strip() == ""


def test_token_injected_into_htmx_poll_url():
    """When token query param is provided, HTMX poll URL includes it."""
    client = _client()
    response = client.get("/?token=testtoken123")
    body = response.text
    assert 'token=testtoken123' in body


def test_token_injected_into_js_variable():
    """When token query param is provided, UI_TOKEN JS var is set."""
    client = _client()
    response = client.get("/?token=mytoken")
    body = response.text
    assert 'UI_TOKEN = "mytoken"' in body


def test_no_token_leaves_clean_urls():
    """Without token, UI_TOKEN is empty."""
    client = _client()
    response = client.get("/")
    body = response.text
    assert 'UI_TOKEN = ""' in body


