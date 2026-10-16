"""Tests for carpenter.tool_backends.web."""
from unittest.mock import patch, MagicMock

from carpenter.tool_backends import web


def test_handle_get_success():
    """handle_get returns response data."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "hello"
    mock_response.headers = {"content-type": "text/plain"}

    with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
        mock_httpx.get.return_value = mock_response
        result = web.handle_get({"url": "http://example.com"})

    assert result["status_code"] == 200
    assert result["text"] == "hello"


def test_handle_get_error():
    """handle_get returns error on failure."""
    with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
        mock_httpx.get.side_effect = Exception("connection refused")
        result = web.handle_get({"url": "http://bad.example.com"})

    assert "error" in result


def test_handle_post_success():
    """handle_post returns response data."""
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.text = '{"id": 1}'
    mock_response.headers = {"content-type": "application/json"}

    with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
        mock_httpx.post.return_value = mock_response
        result = web.handle_post({"url": "http://example.com/api", "json_data": {"key": "val"}})

    assert result["status_code"] == 201


# ── Egress guard ─────────────────────────────────────────────────────


def test_handle_post_refuses_loopback_without_request():
    """web.post to the platform's own loopback API is refused before sending."""
    with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
        result = web.handle_post({
            "url": "http://127.0.0.1:8000/api/chat",
            "json_data": {"text": "hello"},
        })
    assert "error" in result
    assert "not a public address" in result["error"]
    mock_httpx.post.assert_not_called()


def test_handle_get_refuses_localhost_without_request():
    with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
        result = web.handle_get({"url": "http://localhost:8000/api/chat/history"})
    assert "error" in result
    mock_httpx.get.assert_not_called()


def test_handle_get_refuses_metadata_endpoint():
    with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
        result = web.handle_get({"url": "http://169.254.169.254/latest/meta-data/"})
    assert "error" in result
    mock_httpx.get.assert_not_called()


def test_handle_get_allowlisted_loopback(monkeypatch):
    from carpenter import config
    monkeypatch.setitem(config.CONFIG, "web_egress_allowlist", ["127.0.0.1/32"])
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "ok"
    mock_response.headers = {}
    with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
        mock_httpx.get.return_value = mock_response
        result = web.handle_get({"url": "http://127.0.0.1:9000/status"})
    assert result["status_code"] == 200


def _redirect(location, url):
    r = MagicMock()
    r.status_code = 302
    r.headers = {"location": location}
    r.url = url
    return r


def test_fetch_webpage_refuses_private_initial_url():
    with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
        result = web.handle_fetch_webpage({"url": "http://192.168.1.1/admin"})
    assert "error" in result
    mock_httpx.get.assert_not_called()


def test_fetch_webpage_refuses_redirect_to_loopback():
    """A public page that redirects to loopback must not be followed."""
    with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
        mock_httpx.get.return_value = _redirect(
            "http://127.0.0.1:8000/api/chat/history", "https://example.com/r",
        )
        result = web.handle_fetch_webpage({"url": "https://example.com/r"})
    assert "error" in result
    assert "not a public address" in result["error"]
    assert mock_httpx.get.call_count == 1


def test_fetch_webpage_follows_public_redirect():
    final = MagicMock()
    final.status_code = 200
    final.text = "<html>done</html>"
    final.headers = {}
    final.url = "https://example.com/final"
    final.encoding = "utf-8"
    with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
        mock_httpx.get.side_effect = [
            _redirect("/final", "https://example.com/start"), final,
        ]
        result = web.handle_fetch_webpage({"url": "https://example.com/start"})
    assert result["status_code"] == 200
    assert result["url"] == "https://example.com/final"
    urls = [c.args[0] for c in mock_httpx.get.call_args_list]
    assert urls == ["https://example.com/start", "https://example.com/final"]
    for c in mock_httpx.get.call_args_list:
        assert c.kwargs["follow_redirects"] is False
