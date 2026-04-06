"""Tests for carpenter.api.auth — token middleware and bind safety."""

import pytest
from starlette.testclient import TestClient

from carpenter.api.http import create_app
from carpenter.api.auth import is_protected, extract_token
from carpenter.server import _check_bind_safety, _check_tls_config


def _client():
    return TestClient(create_app())


# ── is_protected unit tests ─────────────────────────────────────────

class TestIsProtected:
    def test_root_is_protected(self):
        assert is_protected("/", "GET") is True

    def test_api_chat_is_protected(self):
        assert is_protected("/api/chat", "POST") is True

    def test_api_chat_messages_is_protected(self):
        assert is_protected("/api/chat/messages", "GET") is True

    def test_api_chat_history_is_protected(self):
        assert is_protected("/api/chat/history", "GET") is True

    def test_review_create_is_protected(self):
        assert is_protected("/api/review/create", "POST") is True

    def test_callbacks_not_protected(self):
        assert is_protected("/api/callbacks/tool_result", "POST") is False

    def test_webhooks_not_protected(self):
        assert is_protected("/api/webhooks/push", "POST") is False

    def test_review_uuid_not_protected(self):
        assert is_protected("/api/review/550e8400-e29b-41d4-a716-446655440000", "GET") is False

    def test_review_uuid_decide_not_protected(self):
        assert is_protected("/api/review/550e8400-e29b-41d4-a716-446655440000/decide", "POST") is False

    def test_hooks_not_protected(self):
        assert is_protected("/hooks/signal", "POST") is False
        assert is_protected("/hooks/telegram", "POST") is False


# ── extract_token unit tests ────────────────────────────────────────

class TestExtractToken:
    def test_from_query_param(self):
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        async def handler(request):
            return PlainTextResponse(extract_token(request))

        app = Starlette(routes=[Route("/", handler)])
        client = TestClient(app)
        resp = client.get("/?token=abc123")
        assert resp.text == "abc123"

    def test_from_bearer_header(self):
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        async def handler(request):
            return PlainTextResponse(extract_token(request))

        app = Starlette(routes=[Route("/", handler)])
        client = TestClient(app)
        resp = client.get("/", headers={"Authorization": "Bearer mytoken"})
        assert resp.text == "mytoken"

    def test_query_param_takes_precedence(self):
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        async def handler(request):
            return PlainTextResponse(extract_token(request))

        app = Starlette(routes=[Route("/", handler)])
        client = TestClient(app)
        resp = client.get("/?token=fromquery", headers={"Authorization": "Bearer fromheader"})
        assert resp.text == "fromquery"

    def test_no_token_returns_empty(self):
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        async def handler(request):
            return PlainTextResponse(extract_token(request))

        app = Starlette(routes=[Route("/", handler)])
        client = TestClient(app)
        resp = client.get("/")
        assert resp.text == ""


# ── Middleware integration tests ────────────────────────────────────

def test_no_token_configured_allows_all(monkeypatch):
    """When ui_token is empty, all endpoints return 200."""
    monkeypatch.setitem(
        __import__("carpenter.config", fromlist=["CONFIG"]).CONFIG,
        "ui_token", "",
    )
    client = _client()
    assert client.get("/").status_code == 200
    assert client.get("/api/chat/messages").status_code == 200


def test_protected_rejects_without_token(monkeypatch):
    """When ui_token is set, protected endpoints return 401 without token."""
    monkeypatch.setitem(
        __import__("carpenter.config", fromlist=["CONFIG"]).CONFIG,
        "ui_token", "secret123",
    )
    client = _client()
    assert client.get("/").status_code == 401
    assert client.get("/api/chat/messages").status_code == 401
    assert client.post("/api/chat", json={"text": "hi"}).status_code == 401


def test_accepts_query_param_token(monkeypatch):
    """Query param ?token=xxx grants access to protected endpoints."""
    monkeypatch.setitem(
        __import__("carpenter.config", fromlist=["CONFIG"]).CONFIG,
        "ui_token", "secret123",
    )
    client = _client()
    assert client.get("/?token=secret123").status_code == 200
    assert client.get("/api/chat/messages?token=secret123").status_code == 200


def test_accepts_bearer_header(monkeypatch):
    """Authorization: Bearer xxx grants access to protected endpoints."""
    monkeypatch.setitem(
        __import__("carpenter.config", fromlist=["CONFIG"]).CONFIG,
        "ui_token", "secret123",
    )
    client = _client()
    headers = {"Authorization": "Bearer secret123"}
    assert client.get("/", headers=headers).status_code == 200
    assert client.get("/api/chat/messages", headers=headers).status_code == 200


def test_wrong_token_rejected(monkeypatch):
    """Wrong token returns 401."""
    monkeypatch.setitem(
        __import__("carpenter.config", fromlist=["CONFIG"]).CONFIG,
        "ui_token", "correct",
    )
    client = _client()
    assert client.get("/?token=wrong").status_code == 401
    assert client.get("/", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_callbacks_not_gated(monkeypatch):
    """Callback endpoints are accessible even with ui_token set."""
    monkeypatch.setitem(
        __import__("carpenter.config", fromlist=["CONFIG"]).CONFIG,
        "ui_token", "secret123",
    )
    client = _client()
    # Callbacks require X-Callback-Token but should not be blocked by ui_token.
    # They'll return 403 (wrong callback token) not 401 (ui auth).
    resp = client.post("/api/callbacks/tool_result", json={"tool": "test"})
    assert resp.status_code != 401


def test_webhooks_not_gated(monkeypatch):
    """Webhook endpoints are accessible even with ui_token set."""
    monkeypatch.setitem(
        __import__("carpenter.config", fromlist=["CONFIG"]).CONFIG,
        "ui_token", "secret123",
    )
    client = _client()
    resp = client.post("/api/webhooks/push", json={})
    assert resp.status_code != 401


def test_review_create_is_gated(monkeypatch):
    """POST /api/review/create is gated by ui_token."""
    monkeypatch.setitem(
        __import__("carpenter.config", fromlist=["CONFIG"]).CONFIG,
        "ui_token", "secret123",
    )
    client = _client()
    resp = client.post("/api/review/create", json={"arc_id": 1})
    assert resp.status_code == 401


# ── Bind safety check tests ────────────────────────────────────────

class TestBindSafety:
    def test_localhost_always_safe(self):
        assert _check_bind_safety("127.0.0.1", {}) is None
        assert _check_bind_safety("::1", {}) is None
        assert _check_bind_safety("localhost", {}) is None

    def test_non_local_without_token_rejected(self):
        err = _check_bind_safety("0.0.0.0", {})
        assert err is not None
        assert "ui_token" in err

    def test_non_local_with_token_allowed(self):
        assert _check_bind_safety("0.0.0.0", {"ui_token": "tok"}) is None

    def test_non_local_with_insecure_bind_allowed(self):
        assert _check_bind_safety("0.0.0.0", {"allow_insecure_bind": True}) is None

    def test_non_local_with_empty_token_rejected(self):
        err = _check_bind_safety("0.0.0.0", {"ui_token": ""})
        assert err is not None

    def test_specific_ip_without_token_rejected(self):
        err = _check_bind_safety("192.168.1.100", {})
        assert err is not None

    def test_non_loopback_with_tls_and_token_allowed(self):
        err = _check_bind_safety("0.0.0.0", {"tls_enabled": True, "ui_token": "tok"})
        assert err is None

    def test_non_loopback_with_tls_no_token_allowed(self):
        """TLS alone is sufficient to allow non-loopback binding (warning logged)."""
        err = _check_bind_safety("0.0.0.0", {"tls_enabled": True, "ui_token": ""})
        assert err is None

    def test_non_loopback_without_tls_or_token_rejected(self):
        err = _check_bind_safety("0.0.0.0", {"tls_enabled": False, "ui_token": ""})
        assert err is not None


# ── TLS configuration validation tests ─────────────────────────────


class TestTLSValidation:

    def test_tls_disabled_no_validation(self):
        err = _check_tls_config({"tls_enabled": False})
        assert err is None

    def test_tls_enabled_missing_cert_path(self):
        err = _check_tls_config({
            "tls_enabled": True, "tls_cert_path": "",
            "tls_key_path": "/tmp/key.pem", "tls_domain": "example.com",
        })
        assert "tls_cert_path" in err

    def test_tls_enabled_missing_key_path(self):
        err = _check_tls_config({
            "tls_enabled": True, "tls_cert_path": "/tmp/cert.pem",
            "tls_key_path": "", "tls_domain": "example.com",
        })
        assert "tls_key_path" in err

    def test_tls_enabled_missing_domain(self):
        err = _check_tls_config({
            "tls_enabled": True, "tls_cert_path": "/tmp/cert.pem",
            "tls_key_path": "/tmp/key.pem", "tls_domain": "",
        })
        assert "tls_domain" in err

    def test_tls_cert_file_not_found(self):
        err = _check_tls_config({
            "tls_enabled": True, "tls_cert_path": "/nonexistent/cert.pem",
            "tls_key_path": "/tmp/key.pem", "tls_domain": "example.com",
        })
        assert "not found" in err

    def test_tls_key_file_not_found(self, tmp_path):
        cert = tmp_path / "cert.pem"
        cert.write_text("fake cert")
        err = _check_tls_config({
            "tls_enabled": True, "tls_cert_path": str(cert),
            "tls_key_path": "/nonexistent/key.pem", "tls_domain": "example.com",
        })
        assert "not found" in err

    def test_tls_ca_file_not_found(self, tmp_path):
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.write_text("fake cert")
        key.write_text("fake key")
        err = _check_tls_config({
            "tls_enabled": True, "tls_cert_path": str(cert),
            "tls_key_path": str(key), "tls_domain": "example.com",
            "tls_ca_path": "/nonexistent/ca.pem",
        })
        assert "CA file not found" in err

    def test_tls_valid_config(self, tmp_path):
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.write_text("fake cert")
        key.write_text("fake key")
        err = _check_tls_config({
            "tls_enabled": True, "tls_cert_path": str(cert),
            "tls_key_path": str(key), "tls_domain": "example.com",
        })
        assert err is None

    def test_tls_valid_config_with_ca(self, tmp_path):
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        ca = tmp_path / "ca.pem"
        cert.write_text("fake cert")
        key.write_text("fake key")
        ca.write_text("fake ca")
        err = _check_tls_config({
            "tls_enabled": True, "tls_cert_path": str(cert),
            "tls_key_path": str(key), "tls_domain": "example.com",
            "tls_ca_path": str(ca),
        })
        assert err is None
