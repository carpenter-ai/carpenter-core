"""Tests for carpenter.api.analytics dashboard."""
import json

from starlette.testclient import TestClient

from carpenter.api.http import create_app
from carpenter.core.models import health as mh
from carpenter.db import get_db


def _client():
    return TestClient(create_app())


def test_analytics_page_returns_html():
    """GET /analytics returns the full dashboard page."""
    client = _client()
    resp = client.get("/analytics")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "htmx" in body.lower()
    assert "Model Health" in body
    assert "Retry Attempts" in body
    assert "Error Type" in body
    assert "hx-get" in body


def test_analytics_page_has_nav_links():
    """Dashboard includes nav links to chat and stats."""
    client = _client()
    resp = client.get("/analytics")
    body = resp.text
    assert 'href="/"' in body  # Chat link
    assert "Stats" in body


def test_health_fragment_empty():
    """GET /api/analytics/health returns empty message when no data."""
    client = _client()
    resp = client.get("/api/analytics/health")
    assert resp.status_code == 200
    assert "No model health data" in resp.text


def test_health_fragment_with_data():
    """GET /api/analytics/health shows model cards after recording calls."""
    mh.record_model_call("test-model-alpha", success=True)
    mh.record_model_call("test-model-alpha", success=True)
    mh.record_model_call("test-model-alpha", success=False, error_type="RateLimitError")

    client = _client()
    resp = client.get("/api/analytics/health")
    assert resp.status_code == 200
    body = resp.text
    assert "test-model-alpha" in body
    assert "HEALTHY" in body or "DEGRADED" in body


def test_health_fragment_circuit_open():
    """Health fragment shows circuit open status after many failures."""
    for _ in range(6):
        mh.record_model_call("test-circuit-model", success=False, error_type="APIOutageError")

    client = _client()
    resp = client.get("/api/analytics/health")
    body = resp.text
    assert "test-circuit-model" in body
    assert "CIRCUIT OPEN" in body
    assert "reopens" in body.lower()


def test_retries_fragment_empty():
    """GET /api/analytics/retries returns empty message when no data."""
    client = _client()
    resp = client.get("/api/analytics/retries")
    assert resp.status_code == 200
    assert "No retry attempts" in resp.text


def test_retries_fragment_with_data():
    """GET /api/analytics/retries shows table after inserting retry data."""
    db = get_db()
    try:
        # Need an arc for foreign key
        db.execute(
            "INSERT OR IGNORE INTO arcs (id, name, goal, status) "
            "VALUES (99, 'test-arc', 'test', 'running')"
        )
        db.execute(
            "INSERT INTO arc_history (arc_id, entry_type, content_json) "
            "VALUES (99, 'retry_attempt', ?)",
            (json.dumps({
                "retry_count": 1,
                "error_type": "RateLimitError",
                "backoff_seconds": 12.5,
                "backoff_until": "2026-03-28T12:00:00Z",
                "error_message": "Rate limit exceeded",
            }),),
        )
        db.commit()
    finally:
        db.close()

    client = _client()
    resp = client.get("/api/analytics/retries")
    body = resp.text
    assert "RateLimitError" in body
    assert "12.5" in body
    assert "99" in body  # arc_id


def test_errors_fragment_empty():
    """GET /api/analytics/errors returns empty message when no errors."""
    client = _client()
    resp = client.get("/api/analytics/errors")
    assert resp.status_code == 200
    assert "No errors" in resp.text


def test_errors_fragment_with_data():
    """GET /api/analytics/errors shows breakdown after recording failures."""
    mh.record_model_call("err-model", success=False, error_type="RateLimitError")
    mh.record_model_call("err-model", success=False, error_type="RateLimitError")
    mh.record_model_call("err-model", success=False, error_type="APIOutageError")
    mh.record_model_call("err-model", success=True)

    client = _client()
    resp = client.get("/api/analytics/errors")
    body = resp.text
    assert "RateLimitError" in body
    assert "APIOutageError" in body
    assert "Total calls:" in body


def test_chat_page_has_stats_link():
    """Main chat page includes a link to the analytics dashboard."""
    client = _client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert "/analytics" in resp.text
    assert "Stats" in resp.text
