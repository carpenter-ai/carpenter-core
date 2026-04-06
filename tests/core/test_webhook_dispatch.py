"""Tests for carpenter.core.webhook_dispatch_handler."""

import json
import pytest
from unittest.mock import patch, MagicMock

from carpenter.core.workflows import webhook_dispatch_handler as handler


# ---------------------------------------------------------------------------
# Forgejo payload parser
# ---------------------------------------------------------------------------


def test_parse_forgejo_pr_opened():
    """Parse a Forgejo pull_request opened event."""
    data = {
        "action": "opened",
        "pull_request": {
            "number": 42,
            "title": "Add widget",
            "body": "This adds a widget.",
            "state": "open",
            "head": {"ref": "feature-widget"},
            "base": {"ref": "main"},
            "user": {"login": "dev-user"},
            "html_url": "https://forge.example.com/owner/repo/pulls/42",
        },
        "repository": {
            "name": "repo",
            "owner": {"login": "owner"},
        },
    }

    result = handler._parse_forgejo_payload(data, ["pull_request"])

    assert result is not None
    assert result["event_type"] == "pull_request"
    assert result["action"] == "opened"
    assert result["pr_number"] == 42
    assert result["pr_title"] == "Add widget"
    assert result["repo_owner"] == "owner"
    assert result["repo_name"] == "repo"
    assert result["head_branch"] == "feature-widget"


def test_parse_forgejo_pr_filtered():
    """PR event is filtered out when not in event_filter."""
    data = {
        "action": "opened",
        "pull_request": {"number": 1, "state": "open"},
        "repository": {"name": "repo", "owner": {"login": "owner"}},
    }

    result = handler._parse_forgejo_payload(data, ["push"])
    assert result is None


def test_parse_forgejo_push():
    """Parse a Forgejo push event."""
    data = {
        "ref": "refs/heads/main",
        "commits": [{"id": "abc123"}],
    }

    result = handler._parse_forgejo_payload(data, [])
    assert result is not None
    assert result["event_type"] == "push"
    assert result["ref"] == "refs/heads/main"
    assert result["commits"] == 1


def test_parse_forgejo_closed_pr():
    """Closed PR action is filtered out for PR subscriptions."""
    data = {
        "action": "closed",
        "pull_request": {
            "number": 5,
            "state": "closed",
            "head": {"ref": "fix"},
            "base": {"ref": "main"},
            "user": {"login": "user"},
        },
        "repository": {"name": "repo", "owner": {"login": "owner"}},
    }

    result = handler._parse_forgejo_payload(data, ["pull_request"])
    assert result is None


# ---------------------------------------------------------------------------
# Subscription CRUD
# ---------------------------------------------------------------------------


def test_create_and_get_subscription():
    """Create and retrieve a webhook subscription."""
    sub_id = handler.create_subscription(
        webhook_id="test-hook-123",
        source_type="forgejo",
        action_type="create_arc",
        action_config={"template_name": "pr-review"},
        event_filter=["pull_request"],
        conversation_id=1,
        forge_hook_id=77,
    )

    assert sub_id > 0

    sub = handler.get_subscription("test-hook-123")
    assert sub is not None
    assert sub["webhook_id"] == "test-hook-123"
    assert sub["source_type"] == "forgejo"
    assert sub["action_type"] == "create_arc"
    assert sub["conversation_id"] == 1
    assert sub["forge_hook_id"] == 77


def test_get_subscription_not_found():
    """Get returns None for unknown webhook_id."""
    sub = handler.get_subscription("nonexistent-hook")
    assert sub is None


def test_delete_subscription():
    """Delete removes a subscription."""
    handler.create_subscription(
        webhook_id="to-delete",
        source_type="forgejo",
        action_type="enqueue_work",
    )
    assert handler.get_subscription("to-delete") is not None

    deleted = handler.delete_subscription("to-delete")
    assert deleted is True
    assert handler.get_subscription("to-delete") is None


def test_delete_nonexistent():
    """Delete returns False for unknown webhook_id."""
    deleted = handler.delete_subscription("no-such-hook")
    assert deleted is False


# ---------------------------------------------------------------------------
# Dispatch handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_creates_arc(monkeypatch):
    """Webhook dispatch creates arc from template."""
    # Create a subscription
    handler.create_subscription(
        webhook_id="pr-hook",
        source_type="forgejo",
        action_type="create_arc",
        action_config={
            "template_name": "pr-review",
            "first_step": "pr-review.fetch-pr",
        },
        event_filter=["pull_request"],
    )

    mock_am = MagicMock()
    mock_am.create_arc.return_value = 99
    monkeypatch.setattr(handler, "arc_manager", mock_am)

    mock_wq = MagicMock()
    monkeypatch.setattr(handler, "work_queue", mock_wq)

    mock_set_state = MagicMock()
    monkeypatch.setattr(
        "carpenter.core.workflows.webhook_dispatch_handler._set_arc_state",
        mock_set_state,
        raising=False,
    )
    # Patch the canonical _arc_state module used by the local import
    monkeypatch.setattr(
        "carpenter.core.workflows._arc_state.set_arc_state",
        mock_set_state,
    )

    await handler.handle_webhook_received(1, {
        "webhook_id": "pr-hook",
        "data": {
            "action": "opened",
            "pull_request": {
                "number": 42,
                "title": "Add feature",
                "body": "",
                "state": "open",
                "head": {"ref": "feature"},
                "base": {"ref": "main"},
                "user": {"login": "dev"},
                "html_url": "https://forge.example.com/pulls/42",
            },
            "repository": {
                "name": "repo",
                "owner": {"login": "owner"},
            },
        },
    })

    mock_am.create_arc.assert_called_once()
    mock_wq.enqueue.assert_called_once_with("pr-review.fetch-pr", {"arc_id": 99})


@pytest.mark.asyncio
async def test_dispatch_no_subscription():
    """Webhook with no subscription is ignored silently."""
    # Should not raise
    await handler.handle_webhook_received(1, {
        "webhook_id": "unknown-hook",
        "data": {"action": "opened"},
    })


@pytest.mark.asyncio
async def test_dispatch_enqueue_work(monkeypatch):
    """Webhook dispatch enqueues a work item."""
    handler.create_subscription(
        webhook_id="work-hook",
        source_type="forgejo",
        action_type="enqueue_work",
        action_config={
            "event_type": "custom.action",
            "payload": {"custom_key": "custom_value"},
        },
        event_filter=[],
    )

    mock_wq = MagicMock()
    monkeypatch.setattr(handler, "work_queue", mock_wq)

    await handler.handle_webhook_received(1, {
        "webhook_id": "work-hook",
        "data": {
            "ref": "refs/heads/main",
            "commits": [{"id": "abc"}],
        },
    })

    mock_wq.enqueue.assert_called_once()
    call_args = mock_wq.enqueue.call_args
    assert call_args[0][0] == "custom.action"
    payload = call_args[0][1]
    assert payload["custom_key"] == "custom_value"
    assert payload["event_type"] == "push"


# ---------------------------------------------------------------------------
# register_handlers
# ---------------------------------------------------------------------------


def test_register_handlers():
    """Webhook dispatch handler is registered."""
    registered = {}

    def mock_register(event_type, handler_fn):
        registered[event_type] = handler_fn

    handler.register_handlers(mock_register)

    assert "webhook.received" in registered
