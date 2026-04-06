"""Tests for carpenter.api.webhooks.

Tests verify that the webhook API endpoint routes through the
trigger/event pipeline (via WebhookTrigger) instead of dispatching
directly to the work queue.
"""
import json
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from carpenter.api import webhooks
from carpenter.core.engine import event_bus, work_queue, subscriptions


@pytest.fixture
def client():
    app = Starlette(routes=webhooks.routes)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _register_webhook_subscription():
    """Register the built-in webhook dispatch subscription for tests.

    In production, the coordinator registers this at startup.  Tests
    bypass coordinator startup, so we register it explicitly here.
    """
    subscriptions.load_subscriptions([webhooks.WEBHOOK_DISPATCH_SUBSCRIPTION])
    yield
    subscriptions.reset()


def test_webhook_creates_event(client):
    """POST to webhook endpoint creates an event via the trigger pipeline."""
    response = client.post(
        "/api/webhooks/my-hook",
        json={"key": "value"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["webhook_id"] == "my-hook"
    assert data["event_id"] is not None

    # Verify event was recorded via the trigger pipeline
    event = event_bus.get_event(data["event_id"])
    assert event is not None
    assert event["event_type"] == "webhook.received"

    # Verify trigger metadata was injected
    payload = json.loads(event["payload_json"])
    assert payload["webhook_id"] == "my-hook"
    assert payload["data"] == {"key": "value"}
    assert payload["_trigger"] == "api-webhooks"
    assert payload["_trigger_type"] == "api_webhook"


def test_webhook_empty_body(client):
    """Webhook with empty body creates event with empty data."""
    response = client.post(
        "/api/webhooks/empty",
        content="not json",
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 200

    data = response.json()
    event = event_bus.get_event(data["event_id"])
    payload = json.loads(event["payload_json"])
    assert payload["data"] == {}


def test_webhook_unique_ids(client):
    """Each webhook call creates a unique event."""
    r1 = client.post("/api/webhooks/hook1", json={})
    r2 = client.post("/api/webhooks/hook1", json={})
    assert r1.json()["event_id"] != r2.json()["event_id"]


def test_webhook_different_hooks(client):
    """Different webhook IDs are captured in payload."""
    r1 = client.post("/api/webhooks/alpha", json={})
    r2 = client.post("/api/webhooks/beta", json={})
    assert r1.json()["webhook_id"] == "alpha"
    assert r2.json()["webhook_id"] == "beta"


def test_webhook_creates_work_item_via_subscription(client):
    """Webhook creates a work item through the subscription pipeline.

    The API endpoint emits an event; the built-in webhook-dispatch
    subscription creates the corresponding work item when
    process_subscriptions() runs.
    """
    response = client.post(
        "/api/webhooks/dispatch-test",
        json={"action": "opened", "pull_request": {"number": 1}},
    )
    assert response.status_code == 200

    # Simulate main loop processing subscriptions
    actions_created = subscriptions.process_subscriptions()
    assert actions_created == 1

    # Verify a work item was created by the subscription
    item = work_queue.claim()
    assert item is not None
    assert item["event_type"] == "webhook.received"
    payload = json.loads(item["payload_json"])
    assert payload["webhook_id"] == "dispatch-test"
    assert payload["data"]["action"] == "opened"
    assert payload["_subscription"] == "webhook-dispatch"


def test_webhook_event_source_includes_trigger(client):
    """Event source field identifies the trigger."""
    response = client.post("/api/webhooks/src-test", json={})
    event = event_bus.get_event(response.json()["event_id"])
    assert event["source"] == "trigger:api-webhooks"


def test_webhook_subscription_idempotent(client):
    """Same event processed twice by subscriptions does not duplicate work."""
    client.post("/api/webhooks/idem-test", json={"x": 1})

    assert subscriptions.process_subscriptions() == 1
    # Second pass: event already processed, no new work items
    assert subscriptions.process_subscriptions() == 0
