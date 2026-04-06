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
