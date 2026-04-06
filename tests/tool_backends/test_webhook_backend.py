"""Tests for carpenter.tool_backends.webhook."""

import json
from unittest.mock import patch, MagicMock

from carpenter.tool_backends import webhook
from carpenter.core.workflows import webhook_dispatch_handler as handler


# ---------------------------------------------------------------------------
# handle_subscribe
# ---------------------------------------------------------------------------


def test_subscribe_creates_subscription_and_forgejo_hook(monkeypatch):
    """Subscribe creates local subscription and registers Forgejo webhook."""
    mock_hook_response = MagicMock()
    mock_hook_response.status_code = 201
    mock_hook_response.json.return_value = {
        "id": 77,
        "active": True,
    }

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.post.return_value = mock_hook_response
        result = webhook.handle_subscribe({
            "source_type": "forgejo",
            "event_filter": ["pull_request"],
            "action_type": "create_arc",
            "action_config": {"template_name": "pr-review"},
            "repo_owner": "owner",
            "repo_name": "repo",
        })

    assert "webhook_id" in result
    assert result["subscription_id"] > 0
    assert result["forge_hook_id"] == 77

    # Verify the subscription was stored in DB
    sub = handler.get_subscription(result["webhook_id"])
    assert sub is not None
    assert sub["source_type"] == "forgejo"
    assert sub["action_type"] == "create_arc"
    assert sub["forge_hook_id"] == 77


def test_subscribe_without_repo_info():
    """Subscribe without repo info skips Forgejo registration."""
    result = webhook.handle_subscribe({
        "source_type": "forgejo",
        "event_filter": ["push"],
        "action_type": "enqueue_work",
        "action_config": {"event_type": "custom.build"},
    })

    assert "webhook_id" in result
    assert result["subscription_id"] > 0
    assert result["forge_hook_id"] is None

    # Verify subscription stored
    sub = handler.get_subscription(result["webhook_id"])
    assert sub is not None
    assert sub["forge_hook_id"] is None


def test_subscribe_forgejo_error():
    """Subscribe returns error when Forgejo registration fails."""
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.json.return_value = {"message": "forbidden"}

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.post.return_value = mock_response
        result = webhook.handle_subscribe({
            "source_type": "forgejo",
            "event_filter": ["pull_request"],
            "action_type": "create_arc",
            "action_config": {},
            "repo_owner": "owner",
            "repo_name": "repo",
        })

    assert "error" in result
    assert "forbidden" in result["error"]


def test_subscribe_with_conversation_id():
    """Subscribe stores conversation_id in the subscription."""
    result = webhook.handle_subscribe({
        "source_type": "forgejo",
        "event_filter": ["push"],
        "action_type": "enqueue_work",
        "action_config": {},
        "conversation_id": 42,
    })

    sub = handler.get_subscription(result["webhook_id"])
    assert sub["conversation_id"] == 42


def test_subscribe_builds_target_url(monkeypatch):
    """Subscribe builds correct target URL from config."""
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"id": 88, "active": True}

    # Override specific config keys for URL construction
    from carpenter import config as tc_config
    monkeypatch.setitem(tc_config.CONFIG, "tls_domain", "tc.example.com")
    monkeypatch.setitem(tc_config.CONFIG, "git_server_url", "https://forge.example.com")
    monkeypatch.setitem(tc_config.CONFIG, "git_token", "test-token")

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.post.return_value = mock_response
        result = webhook.handle_subscribe({
            "source_type": "forgejo",
            "event_filter": ["pull_request"],
            "action_type": "create_arc",
            "action_config": {},
            "repo_owner": "owner",
            "repo_name": "repo",
        })

    # Verify the target URL passed to Forgejo uses tls_domain
    call_args = mock_httpx.post.call_args
    payload = call_args[1]["json"]
    target_url = payload["config"]["url"]
    assert "tc.example.com" in target_url
    assert target_url.startswith("https://")
    assert result["webhook_id"] in target_url


def test_subscribe_builds_target_url_no_tls(monkeypatch):
    """Subscribe builds correct target URL without TLS domain."""
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"id": 89, "active": True}

    from carpenter import config as tc_config
    monkeypatch.setitem(tc_config.CONFIG, "tls_domain", "")
    monkeypatch.setitem(tc_config.CONFIG, "tls_enabled", False)
    monkeypatch.setitem(tc_config.CONFIG, "host", "192.168.1.10")
    monkeypatch.setitem(tc_config.CONFIG, "port", 7842)
    monkeypatch.setitem(tc_config.CONFIG, "git_server_url", "https://forge.example.com")
    monkeypatch.setitem(tc_config.CONFIG, "git_token", "test-token")

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.post.return_value = mock_response
        webhook.handle_subscribe({
            "source_type": "forgejo",
            "event_filter": ["push"],
            "action_type": "enqueue_work",
            "action_config": {},
            "repo_owner": "owner",
            "repo_name": "repo",
        })

    call_args = mock_httpx.post.call_args
    payload = call_args[1]["json"]
    target_url = payload["config"]["url"]
    assert "192.168.1.10:7842" in target_url
    assert target_url.startswith("http://")


# ---------------------------------------------------------------------------
# handle_list
# ---------------------------------------------------------------------------


def test_list_empty():
    """List returns empty list when no subscriptions exist."""
    result = webhook.handle_list({})
    assert result["subscriptions"] == []


def test_list_returns_subscriptions():
    """List returns all subscriptions."""
    # Create two subscriptions
    handler.create_subscription(
        webhook_id="hook-a",
        source_type="forgejo",
        action_type="create_arc",
        action_config={"template_name": "pr-review"},
        event_filter=["pull_request"],
    )
    handler.create_subscription(
        webhook_id="hook-b",
        source_type="forgejo",
        action_type="enqueue_work",
        action_config={"event_type": "custom.build"},
        event_filter=["push"],
    )

    result = webhook.handle_list({})
    subs = result["subscriptions"]
    assert len(subs) == 2

    # Verify JSON fields are parsed
    webhook_ids = {s["webhook_id"] for s in subs}
    assert "hook-a" in webhook_ids
    assert "hook-b" in webhook_ids

    for sub in subs:
        # event_filter should be parsed from JSON string to list
        assert isinstance(sub["event_filter"], list)


def test_list_filter_by_source_type():
    """List filters by source_type."""
    handler.create_subscription(
        webhook_id="hook-forgejo",
        source_type="forgejo",
        action_type="create_arc",
    )
    handler.create_subscription(
        webhook_id="hook-github",
        source_type="github",
        action_type="create_arc",
    )

    result_forgejo = webhook.handle_list({"source_type": "forgejo"})
    assert len(result_forgejo["subscriptions"]) == 1
    assert result_forgejo["subscriptions"][0]["webhook_id"] == "hook-forgejo"

    result_github = webhook.handle_list({"source_type": "github"})
    assert len(result_github["subscriptions"]) == 1
    assert result_github["subscriptions"][0]["webhook_id"] == "hook-github"


# ---------------------------------------------------------------------------
# handle_delete
# ---------------------------------------------------------------------------


def test_delete_subscription():
    """Delete removes a subscription."""
    handler.create_subscription(
        webhook_id="hook-to-delete",
        source_type="forgejo",
        action_type="create_arc",
    )
    assert handler.get_subscription("hook-to-delete") is not None

    result = webhook.handle_delete({"webhook_id": "hook-to-delete"})
    assert result["deleted"] is True
    assert handler.get_subscription("hook-to-delete") is None


def test_delete_nonexistent():
    """Delete returns False for unknown webhook_id."""
    result = webhook.handle_delete({"webhook_id": "no-such-hook"})
    assert result["deleted"] is False


def test_delete_with_forgejo_cleanup():
    """Delete removes Forgejo-side webhook when forge_hook_id is stored."""
    handler.create_subscription(
        webhook_id="hook-with-forge",
        source_type="forgejo",
        action_type="create_arc",
        source_config={"repo_owner": "owner", "repo_name": "repo"},
        forge_hook_id=77,
    )

    mock_response = MagicMock()
    mock_response.status_code = 204
    mock_response.text = ""

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.delete.return_value = mock_response
        result = webhook.handle_delete({"webhook_id": "hook-with-forge"})

    assert result["deleted"] is True

    # Verify Forgejo delete was called
    call_args = mock_httpx.delete.call_args
    assert "/repos/owner/repo/hooks/77" in call_args[0][0]


def test_delete_forgejo_failure_still_deletes_locally():
    """Delete proceeds with local deletion even if Forgejo cleanup fails."""
    handler.create_subscription(
        webhook_id="hook-forge-fail",
        source_type="forgejo",
        action_type="create_arc",
        source_config={"repo_owner": "owner", "repo_name": "repo"},
        forge_hook_id=88,
    )

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not Found"
    mock_response.json.return_value = {"message": "hook not found"}

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.delete.return_value = mock_response
        result = webhook.handle_delete({"webhook_id": "hook-forge-fail"})

    # Local deletion should succeed even though Forgejo returned 404
    assert result["deleted"] is True
    assert handler.get_subscription("hook-forge-fail") is None


def test_delete_missing_webhook_id():
    """Delete returns error when webhook_id is empty."""
    result = webhook.handle_delete({"webhook_id": ""})
    assert result["deleted"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# Full round-trip
# ---------------------------------------------------------------------------


def test_subscribe_list_delete_roundtrip():
    """Full lifecycle: subscribe, list, delete."""
    # Subscribe
    sub_result = webhook.handle_subscribe({
        "source_type": "forgejo",
        "event_filter": ["push"],
        "action_type": "enqueue_work",
        "action_config": {"event_type": "ci.build"},
    })
    webhook_id = sub_result["webhook_id"]

    # List
    list_result = webhook.handle_list({})
    webhook_ids = [s["webhook_id"] for s in list_result["subscriptions"]]
    assert webhook_id in webhook_ids

    # Delete
    delete_result = webhook.handle_delete({"webhook_id": webhook_id})
    assert delete_result["deleted"] is True

    # Verify gone
    list_result = webhook.handle_list({})
    webhook_ids = [s["webhook_id"] for s in list_result["subscriptions"]]
    assert webhook_id not in webhook_ids
