"""Tests for carpenter.core.workflows.pr_review_handler."""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from carpenter.core.workflows import pr_review_handler as handler


@pytest.fixture
def mock_arc(monkeypatch):
    """Set up mocks for arc_manager and work_queue."""
    mock_am = MagicMock()
    mock_am.get_arc.return_value = {"id": 1, "status": "pending", "goal": "Review PR"}
    mock_am.update_status = MagicMock()
    mock_am.add_history = MagicMock()
    monkeypatch.setattr(handler, "arc_manager", mock_am)

    mock_wq = MagicMock()
    mock_wq.enqueue = MagicMock()
    monkeypatch.setattr(handler, "work_queue", mock_wq)

    return mock_am, mock_wq


@pytest.fixture
def mock_state(monkeypatch):
    """Mock arc state get/set."""
    _state = {}

    def _get(arc_id, key, default=None):
        return _state.get((arc_id, key), default)

    def _set(arc_id, key, value):
        _state[(arc_id, key)] = value

    monkeypatch.setattr(handler, "_get_arc_state", _get)
    monkeypatch.setattr(handler, "_set_arc_state", _set)
    return _state


@pytest.fixture
def mock_notify(monkeypatch):
    """Mock notification functions."""
    monkeypatch.setattr(handler, "_notify_chat", MagicMock())
    monkeypatch.setattr(handler, "_notify_and_respond", AsyncMock())


# ---------------------------------------------------------------------------
# handle_fetch_pr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_pr_success(mock_arc, mock_state, mock_notify, monkeypatch):
    """Successful PR fetch stores metadata + diff and enqueues ai-review."""
    _, mock_wq = mock_arc
    mock_state[(1, "repo_owner")] = "owner"
    mock_state[(1, "repo_name")] = "repo"
    mock_state[(1, "pr_number")] = 42

    mock_get_pr = MagicMock(return_value={
        "number": 42,
        "title": "Add feature",
        "body": "This adds a feature.",
        "state": "open",
        "head_branch": "feature",
        "base_branch": "main",
        "user": "dev",
        "html_url": "https://forge.example.com/pulls/42",
    })
    mock_get_diff = MagicMock(return_value={
        "diff": "--- a/file.py\n+++ b/file.py\n@@ -1 +1,2 @@\n hello\n+world",
    })
    monkeypatch.setattr(handler.forgejo_api_backend, "handle_get_pr", mock_get_pr)
    monkeypatch.setattr(handler.forgejo_api_backend, "handle_get_pr_diff", mock_get_diff)

    await handler.handle_fetch_pr(1, {"arc_id": 1})

    mock_get_pr.assert_called_once()
    mock_get_diff.assert_called_once()
    assert mock_state[(1, "pr_metadata")]["title"] == "Add feature"
    assert "world" in mock_state[(1, "pr_diff")]
    mock_wq.enqueue.assert_called_once_with("pr-review.ai-review", {"arc_id": 1})


@pytest.mark.asyncio
async def test_fetch_pr_missing_coordinates(mock_arc, mock_state, mock_notify):
    """Fetch fails gracefully when PR coordinates are missing."""
    mock_am, _ = mock_arc
    mock_state[(1, "repo_owner")] = "owner"
    # Missing repo_name and pr_number

    await handler.handle_fetch_pr(1, {"arc_id": 1})

    mock_am.update_status.assert_called_with(1, "failed")


@pytest.mark.asyncio
async def test_fetch_pr_api_error(mock_arc, mock_state, mock_notify, monkeypatch):
    """PR fetch API error marks arc as failed."""
    mock_am, _ = mock_arc
    mock_state[(1, "repo_owner")] = "owner"
    mock_state[(1, "repo_name")] = "repo"
    mock_state[(1, "pr_number")] = 42

    mock_get_pr = MagicMock(return_value={"error": "Not found"})
    monkeypatch.setattr(handler.forgejo_api_backend, "handle_get_pr", mock_get_pr)

    await handler.handle_fetch_pr(1, {"arc_id": 1})

    mock_am.update_status.assert_any_call(1, "failed")


# ---------------------------------------------------------------------------
# handle_ai_review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ai_review_success(mock_arc, mock_state, mock_notify, monkeypatch):
    """Successful AI review stores result and enqueues post-review."""
    _, mock_wq = mock_arc
    mock_state[(1, "pr_metadata")] = {
        "title": "Add feature",
        "body": "Adds widget",
        "head_branch": "feature",
        "base_branch": "main",
    }
    mock_state[(1, "pr_diff")] = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new"

    # Mock model resolution
    monkeypatch.setattr(handler, "config", MagicMock())
    handler.config.CONFIG = {"model_roles": {"pr_review": "anthropic:claude-sonnet-4-20250514"}}

    mock_get_role = MagicMock(return_value="anthropic:claude-sonnet-4-20250514")
    mock_parse = MagicMock(return_value=("anthropic", "claude-sonnet-4-20250514"))
    mock_client = MagicMock()
    mock_client.chat.return_value = {
        "content": [{"type": "text", "text": json.dumps({
            "verdict": "APPROVED",
            "body": "Looks good!",
            "summary": "Simple rename",
            "issues": [],
            "suggestions": [],
        })}],
    }
    mock_create_client = MagicMock(return_value=mock_client)

    monkeypatch.setattr(
        "carpenter.core.workflows.pr_review_handler.get_model_for_role",
        mock_get_role, raising=False,
    )
    monkeypatch.setattr(
        "carpenter.core.workflows.pr_review_handler.parse_model_string",
        mock_parse, raising=False,
    )
    monkeypatch.setattr(
        "carpenter.core.workflows.pr_review_handler.create_client_for_model",
        mock_create_client, raising=False,
    )

    # We need to patch the imports inside the function
    with patch("carpenter.agent.model_resolver.get_model_for_role", mock_get_role), \
         patch("carpenter.agent.model_resolver.create_client_for_model", mock_create_client), \
         patch("carpenter.agent.model_resolver.parse_model_string", mock_parse):
        await handler.handle_ai_review(1, {"arc_id": 1})

    assert mock_state[(1, "review_result")]["verdict"] == "APPROVED"
    mock_wq.enqueue.assert_called_once_with("pr-review.post-review", {"arc_id": 1})


@pytest.mark.asyncio
async def test_ai_review_empty_diff(mock_arc, mock_state, mock_notify):
    """Empty diff produces a COMMENT verdict without calling AI."""
    _, mock_wq = mock_arc
    mock_state[(1, "pr_metadata")] = {"title": "Empty PR"}
    mock_state[(1, "pr_diff")] = ""

    await handler.handle_ai_review(1, {"arc_id": 1})

    assert mock_state[(1, "review_result")]["verdict"] == "COMMENT"
    mock_wq.enqueue.assert_called_once_with("pr-review.post-review", {"arc_id": 1})


# ---------------------------------------------------------------------------
# _parse_review_response
# ---------------------------------------------------------------------------


def test_parse_review_json():
    """Parse valid JSON review response."""
    text = json.dumps({
        "verdict": "REQUEST_CHANGES",
        "body": "Needs fixes",
        "summary": "Found bugs",
        "issues": [{"file": "x.py", "line": 10, "severity": "high", "description": "Bug"}],
        "suggestions": ["Use constants"],
    })
    result = handler._parse_review_response(text)
    assert result["verdict"] == "REQUEST_CHANGES"
    assert len(result["issues"]) == 1
    assert result["issues"][0]["file"] == "x.py"


def test_parse_review_json_in_code_fence():
    """Parse JSON wrapped in markdown code fences."""
    text = '```json\n{"verdict": "APPROVED", "body": "LGTM"}\n```'
    result = handler._parse_review_response(text)
    assert result["verdict"] == "APPROVED"


def test_parse_review_plain_text_approve():
    """Plain text mentioning 'approve' results in APPROVED verdict."""
    text = "Everything looks good, I approve these changes."
    result = handler._parse_review_response(text)
    assert result["verdict"] == "APPROVED"
    assert result["body"] == text


def test_parse_review_plain_text_request_changes():
    """Plain text mentioning 'request changes' results in REQUEST_CHANGES."""
    text = "I request changes on this PR. Please fix the SQL injection."
    result = handler._parse_review_response(text)
    assert result["verdict"] == "REQUEST_CHANGES"


def test_parse_review_plain_text_neutral():
    """Plain text without verdict keywords defaults to COMMENT."""
    text = "Some interesting changes here. Let me think about this."
    result = handler._parse_review_response(text)
    assert result["verdict"] == "COMMENT"


def test_parse_review_invalid_verdict_normalized():
    """Invalid verdict in JSON is normalized to COMMENT."""
    text = json.dumps({"verdict": "MAYBE", "body": "Not sure"})
    result = handler._parse_review_response(text)
    assert result["verdict"] == "COMMENT"


# ---------------------------------------------------------------------------
# handle_post_review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_review_success(mock_arc, mock_state, mock_notify, monkeypatch):
    """Successful review post enqueues notify step."""
    _, mock_wq = mock_arc
    mock_state[(1, "repo_owner")] = "owner"
    mock_state[(1, "repo_name")] = "repo"
    mock_state[(1, "pr_number")] = 42
    mock_state[(1, "review_result")] = {
        "verdict": "APPROVED",
        "body": "Looks good!",
        "issues": [],
        "suggestions": [],
    }

    mock_post = MagicMock(return_value={"review_id": 99, "state": "APPROVED"})
    monkeypatch.setattr(handler.forgejo_api_backend, "handle_post_pr_review", mock_post)

    await handler.handle_post_review(1, {"arc_id": 1})

    mock_post.assert_called_once()
    call_params = mock_post.call_args[0][0]
    assert call_params["event"] == "APPROVED"
    assert call_params["pr_number"] == 42
    assert mock_state[(1, "forge_review_id")] == 99
    mock_wq.enqueue.assert_called_once_with("pr-review.notify", {"arc_id": 1})


@pytest.mark.asyncio
async def test_post_review_with_line_comments(mock_arc, mock_state, mock_notify, monkeypatch):
    """Review with issues includes line comments."""
    _, mock_wq = mock_arc
    mock_state[(1, "repo_owner")] = "owner"
    mock_state[(1, "repo_name")] = "repo"
    mock_state[(1, "pr_number")] = 42
    mock_state[(1, "review_result")] = {
        "verdict": "REQUEST_CHANGES",
        "body": "Fix these issues",
        "issues": [
            {"file": "main.py", "line": 10, "severity": "high", "description": "SQL injection"},
            {"file": "utils.py", "severity": "low", "description": "Missing docstring"},
        ],
        "suggestions": [],
    }

    mock_post = MagicMock(return_value={"review_id": 100, "state": "REQUEST_CHANGES"})
    monkeypatch.setattr(handler.forgejo_api_backend, "handle_post_pr_review", mock_post)

    await handler.handle_post_review(1, {"arc_id": 1})

    call_params = mock_post.call_args[0][0]
    # Only the issue with both file AND line should become a line comment
    assert len(call_params["comments"]) == 1
    assert call_params["comments"][0]["path"] == "main.py"


@pytest.mark.asyncio
async def test_post_review_api_error(mock_arc, mock_state, mock_notify, monkeypatch):
    """Post review API error marks arc as failed."""
    mock_am, _ = mock_arc
    mock_state[(1, "repo_owner")] = "owner"
    mock_state[(1, "repo_name")] = "repo"
    mock_state[(1, "pr_number")] = 42
    mock_state[(1, "review_result")] = {"verdict": "COMMENT", "body": "OK", "issues": []}

    mock_post = MagicMock(return_value={"error": "Unauthorized"})
    monkeypatch.setattr(handler.forgejo_api_backend, "handle_post_pr_review", mock_post)

    await handler.handle_post_review(1, {"arc_id": 1})

    mock_am.update_status.assert_any_call(1, "failed")


# ---------------------------------------------------------------------------
# handle_notify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_success(mock_arc, mock_state, mock_notify, monkeypatch):
    """Notify step marks arc as completed."""
    mock_am, _ = mock_arc
    mock_state[(1, "pr_number")] = 42
    mock_state[(1, "review_result")] = {
        "verdict": "APPROVED",
        "summary": "All good",
        "body": "LGTM",
    }
    mock_state[(1, "pr_metadata")] = {
        "title": "Add feature",
        "html_url": "https://forge.example.com/pulls/42",
    }
    mock_state[(1, "webhook_data")] = {}

    mock_notifications = MagicMock()
    monkeypatch.setattr(handler, "notifications", mock_notifications, raising=False)
    # Patch the import path
    with patch("carpenter.core.workflows.pr_review_handler.notifications", create=True) as mock_n:
        await handler.handle_notify(1, {"arc_id": 1})

    mock_am.update_status.assert_called_with(1, "completed")


# ---------------------------------------------------------------------------
# register_handlers
# ---------------------------------------------------------------------------


def test_register_handlers():
    """All four PR review handlers are registered."""
    registered = {}

    def mock_register(event_type, handler_fn):
        registered[event_type] = handler_fn

    handler.register_handlers(mock_register)

    assert "pr-review.fetch-pr" in registered
    assert "pr-review.ai-review" in registered
    assert "pr-review.post-review" in registered
    assert "pr-review.notify" in registered
