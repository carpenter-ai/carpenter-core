"""Tests for carpenter.core.workflows.external_coding_change_handler.

Uses the real DB (via test_db), real arc_manager, real work_queue, and real
arc_state helpers.  Only mocks truly external dependencies that cannot run
in the test environment: git_backend (needs real remotes), coding_dispatch
(needs an LLM), thread_pools (needs a pool), and workspace_manager
(needs real workspaces for diff/changed-file queries).
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import work_queue
from carpenter.core.workflows import external_coding_change_handler as handler
from carpenter.core.workflows._arc_state import (
    get_arc_state as _get_arc_state,
    set_arc_state as _set_arc_state,
)
from carpenter.db import get_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pending_work_items(event_type: str) -> list[dict]:
    """Return pending work-queue items matching *event_type*."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM work_queue WHERE event_type = ? AND status = 'pending'",
            (event_type,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# handle_clone_and_branch
# ---------------------------------------------------------------------------


class TestHandleCloneAndBranch:
    @pytest.mark.asyncio
    async def test_success_activates_arc_and_enqueues_next_step(self, test_db):
        """Successful clone sets arc to active, stores state, enqueues invoke-agent."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-test",
            goal="Add README to external repo",
        )
        assert arc_manager.get_arc(arc_id)["status"] == "pending"

        mock_setup = MagicMock(return_value={
            "success": True, "workspace_path": "/tmp/ws/ext-1",
        })
        mock_branch = MagicMock(return_value={
            "branch_name": "tc/change-1", "created": True,
        })

        with patch.object(handler.git_backend, "handle_setup_repo", mock_setup), \
             patch.object(handler.git_backend, "handle_create_branch", mock_branch), \
             patch.object(handler, "_notify_and_respond", new_callable=AsyncMock):
            await handler.handle_clone_and_branch(1, {
                "arc_id": arc_id,
                "repo_url": "https://forge.example.com/owner/repo.git",
                "fork_url": "https://forge.example.com/bot/repo.git",
                "branch_name": "feature-x",
                "workspace": "/tmp/ws/ext-1",
            })

        # Arc should be active
        assert arc_manager.get_arc(arc_id)["status"] == "active"

        # Arc state should have workspace and branch
        assert _get_arc_state(arc_id, "workspace_path") == "/tmp/ws/ext-1"
        assert _get_arc_state(arc_id, "branch_name") == "feature-x"
        assert _get_arc_state(arc_id, "repo_url") == "https://forge.example.com/owner/repo.git"
        assert _get_arc_state(arc_id, "fork_url") == "https://forge.example.com/bot/repo.git"

        # Should have enqueued invoke-agent
        items = _pending_work_items("external-coding-change.invoke-agent")
        assert len(items) == 1
        payload = json.loads(items[0]["payload_json"])
        assert payload["arc_id"] == arc_id

        # History should record the clone
        history = arc_manager.get_history(arc_id)
        assert any(h["entry_type"] == "cloned" for h in history)

    @pytest.mark.asyncio
    async def test_clone_failure_marks_arc_failed(self, test_db):
        """Clone failure marks the arc as failed with error in history."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-fail",
            goal="Failing clone",
        )

        mock_setup = MagicMock(return_value={
            "success": False, "error": "auth failed",
        })

        with patch.object(handler.git_backend, "handle_setup_repo", mock_setup), \
             patch.object(handler, "_notify_and_respond", new_callable=AsyncMock):
            await handler.handle_clone_and_branch(1, {
                "arc_id": arc_id,
                "repo_url": "https://forge.example.com/owner/repo.git",
                "workspace": "/tmp/ws/ext-fail",
            })

        assert arc_manager.get_arc(arc_id)["status"] == "failed"

        history = arc_manager.get_history(arc_id)
        errors = [h for h in history if h["entry_type"] == "error"]
        assert len(errors) >= 1
        assert "auth failed" in json.loads(errors[0]["content_json"])["message"]

    @pytest.mark.asyncio
    async def test_missing_fields_returns_early(self, test_db):
        """Missing arc_id or repo_url returns early without error."""
        # Should not raise
        await handler.handle_clone_and_branch(1, {})
        await handler.handle_clone_and_branch(1, {"arc_id": 999})

    @pytest.mark.asyncio
    async def test_default_workspace_and_branch(self, test_db):
        """Defaults are used when workspace and branch_name are not provided."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-defaults",
            goal="Test defaults",
        )

        mock_setup = MagicMock(return_value={"success": True, "workspace_path": "/tmp/ext"})
        mock_branch = MagicMock(return_value={"branch_name": f"tc/change-{arc_id}", "created": True})

        with patch.object(handler.git_backend, "handle_setup_repo", mock_setup), \
             patch.object(handler.git_backend, "handle_create_branch", mock_branch), \
             patch.object(handler, "_notify_and_respond", new_callable=AsyncMock):
            await handler.handle_clone_and_branch(1, {
                "arc_id": arc_id,
                "repo_url": "https://forge.example.com/owner/repo.git",
            })

        # handle_create_branch should have been called with the default branch name
        call_args = mock_branch.call_args[0][0]
        assert call_args["branch_name"] == f"tc/change-{arc_id}"

    @pytest.mark.asyncio
    async def test_exception_during_setup_marks_failed(self, test_db):
        """Unexpected exception during git operations fails the arc gracefully."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-exception",
            goal="Exception test",
        )

        mock_setup = MagicMock(side_effect=RuntimeError("disk full"))

        with patch.object(handler.git_backend, "handle_setup_repo", mock_setup), \
             patch.object(handler, "_notify_and_respond", new_callable=AsyncMock):
            await handler.handle_clone_and_branch(1, {
                "arc_id": arc_id,
                "repo_url": "https://forge.example.com/owner/repo.git",
                "workspace": "/tmp/ws/ext-err",
            })

        assert arc_manager.get_arc(arc_id)["status"] == "failed"
        history = arc_manager.get_history(arc_id)
        errors = [h for h in history if h["entry_type"] == "error"]
        assert any("disk full" in json.loads(e["content_json"])["message"] for e in errors)


# ---------------------------------------------------------------------------
# handle_invoke_agent
# ---------------------------------------------------------------------------


class TestHandleInvokeAgent:
    @pytest.mark.asyncio
    async def test_success_enqueues_push_and_pr(self, test_db):
        """Successful agent run enqueues push-and-pr when local_review is off."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-agent",
            goal="Add README",
        )
        arc_manager.update_status(arc_id, "active")
        _set_arc_state(arc_id, "workspace_path", "/tmp/ws/ext-agent")
        _set_arc_state(arc_id, "prompt", "Add a README.md")

        agent_result = {"exit_code": 0, "iterations": 3, "stdout": "Done"}

        async def fake_run_in_work_pool(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("carpenter.agent.coding_dispatch.invoke_coding_agent", return_value=agent_result), \
             patch("carpenter.thread_pools.run_in_work_pool", side_effect=fake_run_in_work_pool), \
             patch("carpenter.core.arcs.verification.try_create_verification_arcs", return_value=False), \
             patch.object(handler, "_notify_and_respond", new_callable=AsyncMock):
            await handler.handle_invoke_agent(1, {"arc_id": arc_id})

        # Agent result should be stored in arc state
        assert _get_arc_state(arc_id, "agent_result") == agent_result

        # History should record completion
        history = arc_manager.get_history(arc_id)
        assert any(h["entry_type"] == "agent_completed" for h in history)

        # Should have enqueued push-and-pr (not local-review)
        items = _pending_work_items("external-coding-change.push-and-pr")
        assert len(items) == 1

    @pytest.mark.asyncio
    async def test_with_local_review_enqueues_local_review(self, test_db):
        """Agent run with local_review=True enqueues local-review step."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-review",
            goal="Add README with review",
        )
        arc_manager.update_status(arc_id, "active")
        _set_arc_state(arc_id, "workspace_path", "/tmp/ws/ext-review")
        _set_arc_state(arc_id, "prompt", "Add a README.md")
        _set_arc_state(arc_id, "local_review", True)

        agent_result = {"exit_code": 0, "iterations": 1, "stdout": ""}

        async def fake_run_in_work_pool(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("carpenter.agent.coding_dispatch.invoke_coding_agent", return_value=agent_result), \
             patch("carpenter.thread_pools.run_in_work_pool", side_effect=fake_run_in_work_pool), \
             patch("carpenter.core.arcs.verification.try_create_verification_arcs", return_value=False), \
             patch.object(handler, "_notify_and_respond", new_callable=AsyncMock):
            await handler.handle_invoke_agent(1, {"arc_id": arc_id})

        items = _pending_work_items("external-coding-change.local-review")
        assert len(items) == 1

    @pytest.mark.asyncio
    async def test_missing_arc_id_returns_early(self, test_db):
        """Missing arc_id returns early without error."""
        await handler.handle_invoke_agent(1, {})

    @pytest.mark.asyncio
    async def test_missing_workspace_marks_failed(self, test_db):
        """Missing workspace in arc state marks the arc as failed."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-no-ws",
            goal="No workspace",
        )
        arc_manager.update_status(arc_id, "active")
        # No workspace_path set

        await handler.handle_invoke_agent(1, {"arc_id": arc_id})

        assert arc_manager.get_arc(arc_id)["status"] == "failed"

    @pytest.mark.asyncio
    async def test_agent_exception_marks_failed(self, test_db):
        """Exception during coding agent marks the arc as failed."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-agent-err",
            goal="Agent failure",
        )
        arc_manager.update_status(arc_id, "active")
        _set_arc_state(arc_id, "workspace_path", "/tmp/ws/ext-err")
        _set_arc_state(arc_id, "prompt", "fail")

        async def fake_run_in_work_pool(fn, *args, **kwargs):
            raise RuntimeError("model unavailable")

        with patch("carpenter.thread_pools.run_in_work_pool", side_effect=fake_run_in_work_pool), \
             patch.object(handler, "_notify_and_respond", new_callable=AsyncMock):
            await handler.handle_invoke_agent(1, {"arc_id": arc_id})

        assert arc_manager.get_arc(arc_id)["status"] == "failed"
        history = arc_manager.get_history(arc_id)
        errors = [h for h in history if h["entry_type"] == "error"]
        assert any("model unavailable" in json.loads(e["content_json"])["message"] for e in errors)

    @pytest.mark.asyncio
    async def test_verification_arcs_skip_direct_enqueue(self, test_db):
        """When verification arcs are created, no direct push/review is enqueued."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-verify",
            goal="With verification",
        )
        arc_manager.update_status(arc_id, "active")
        _set_arc_state(arc_id, "workspace_path", "/tmp/ws/ext-verify")
        _set_arc_state(arc_id, "prompt", "test")

        agent_result = {"exit_code": 0, "iterations": 1, "stdout": "ok"}

        async def fake_run_in_work_pool(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("carpenter.agent.coding_dispatch.invoke_coding_agent", return_value=agent_result), \
             patch("carpenter.thread_pools.run_in_work_pool", side_effect=fake_run_in_work_pool), \
             patch("carpenter.core.arcs.verification.try_create_verification_arcs", return_value=True), \
             patch.object(handler, "_notify_and_respond", new_callable=AsyncMock):
            await handler.handle_invoke_agent(1, {"arc_id": arc_id})

        # Neither push-and-pr nor local-review should be enqueued
        assert _pending_work_items("external-coding-change.push-and-pr") == []
        assert _pending_work_items("external-coding-change.local-review") == []


# ---------------------------------------------------------------------------
# handle_push_and_pr
# ---------------------------------------------------------------------------


class TestHandlePushAndPr:
    @pytest.mark.asyncio
    async def test_success_completes_arc(self, test_db):
        """Successful push and PR creation completes the arc."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-push",
            goal="Push and PR",
        )
        arc_manager.update_status(arc_id, "active")
        _set_arc_state(arc_id, "workspace_path", "/tmp/ws/ext-push")
        _set_arc_state(arc_id, "branch_name", "feature-x")
        _set_arc_state(arc_id, "commit_message", "Add README")
        _set_arc_state(arc_id, "repo_owner", "owner")
        _set_arc_state(arc_id, "repo_name", "repo")
        _set_arc_state(arc_id, "fork_user", "bot")
        _set_arc_state(arc_id, "pr_title", "Add README")

        mock_push = MagicMock(return_value={"pushed": True, "commit_sha": "abc123"})
        mock_pr = MagicMock(return_value={
            "pr_number": 42, "pr_url": "https://forge.example.com/pulls/42",
        })

        with patch("carpenter.core.workspace_manager.get_changed_files", return_value=["README.md"]), \
             patch.object(handler.git_backend, "handle_commit_and_push", mock_push), \
             patch.object(handler.forgejo_api_backend, "handle_create_pr", mock_pr), \
             patch.object(handler, "_notify_and_respond", new_callable=AsyncMock):
            await handler.handle_push_and_pr(1, {"arc_id": arc_id})

        assert arc_manager.get_arc(arc_id)["status"] == "completed"

        # Arc state should have PR info
        assert _get_arc_state(arc_id, "pr_number") == 42
        assert _get_arc_state(arc_id, "pr_url") == "https://forge.example.com/pulls/42"
        assert _get_arc_state(arc_id, "commit_sha") == "abc123"

        # History should record pushed and pr_created
        history = arc_manager.get_history(arc_id)
        types = [h["entry_type"] for h in history]
        assert "pushed" in types
        assert "pr_created" in types

    @pytest.mark.asyncio
    async def test_push_failure_marks_failed(self, test_db):
        """Push failure marks the arc as failed."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-push-fail",
            goal="Push fails",
        )
        arc_manager.update_status(arc_id, "active")
        _set_arc_state(arc_id, "workspace_path", "/tmp/ws/ext-push-fail")
        _set_arc_state(arc_id, "branch_name", "feature-x")
        _set_arc_state(arc_id, "commit_message", "Add README")

        mock_push = MagicMock(return_value={"pushed": False, "error": "auth failed"})

        with patch("carpenter.core.workspace_manager.get_changed_files", return_value=["README.md"]), \
             patch.object(handler.git_backend, "handle_commit_and_push", mock_push), \
             patch.object(handler, "_notify_and_respond", new_callable=AsyncMock):
            await handler.handle_push_and_pr(1, {"arc_id": arc_id})

        assert arc_manager.get_arc(arc_id)["status"] == "failed"

        history = arc_manager.get_history(arc_id)
        assert any(h["entry_type"] == "push_failed" for h in history)

    @pytest.mark.asyncio
    async def test_no_changes_completes_arc(self, test_db):
        """No changed files completes the arc without pushing."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-no-changes",
            goal="No changes",
        )
        arc_manager.update_status(arc_id, "active")
        _set_arc_state(arc_id, "workspace_path", "/tmp/ws/ext-empty")
        _set_arc_state(arc_id, "branch_name", "feature-x")

        with patch("carpenter.core.workspace_manager.get_changed_files", return_value=[]), \
             patch.object(handler, "_notify_and_respond", new_callable=AsyncMock):
            await handler.handle_push_and_pr(1, {"arc_id": arc_id})

        assert arc_manager.get_arc(arc_id)["status"] == "completed"
        history = arc_manager.get_history(arc_id)
        assert any(h["entry_type"] == "no_changes" for h in history)

    @pytest.mark.asyncio
    async def test_push_only_no_pr_when_missing_repo_coords(self, test_db):
        """Push without PR when repo_owner/repo_name/fork_user are missing."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-push-only",
            goal="Push only, no PR",
        )
        arc_manager.update_status(arc_id, "active")
        _set_arc_state(arc_id, "workspace_path", "/tmp/ws/ext-push-only")
        _set_arc_state(arc_id, "branch_name", "feature-x")
        _set_arc_state(arc_id, "commit_message", "changes")
        # No repo_owner, repo_name, fork_user set

        mock_push = MagicMock(return_value={"pushed": True, "commit_sha": "def456"})

        with patch("carpenter.core.workspace_manager.get_changed_files", return_value=["file.py"]), \
             patch.object(handler.git_backend, "handle_commit_and_push", mock_push), \
             patch.object(handler, "_notify_and_respond", new_callable=AsyncMock) as mock_notify:
            await handler.handle_push_and_pr(1, {"arc_id": arc_id})

        assert arc_manager.get_arc(arc_id)["status"] == "completed"
        # Notification should mention push, not PR
        mock_notify.assert_called_once()
        assert "pushed" in mock_notify.call_args[0][1].lower() or "feature-x" in mock_notify.call_args[0][1]

    @pytest.mark.asyncio
    async def test_pr_creation_failure_marks_failed(self, test_db):
        """PR creation failure marks the arc as failed."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-pr-fail",
            goal="PR creation fails",
        )
        arc_manager.update_status(arc_id, "active")
        _set_arc_state(arc_id, "workspace_path", "/tmp/ws/ext-pr-fail")
        _set_arc_state(arc_id, "branch_name", "feature-x")
        _set_arc_state(arc_id, "commit_message", "changes")
        _set_arc_state(arc_id, "repo_owner", "owner")
        _set_arc_state(arc_id, "repo_name", "repo")
        _set_arc_state(arc_id, "fork_user", "bot")

        mock_push = MagicMock(return_value={"pushed": True, "commit_sha": "abc"})
        mock_pr = MagicMock(return_value={"error": "branch not found"})

        with patch("carpenter.core.workspace_manager.get_changed_files", return_value=["file.py"]), \
             patch.object(handler.git_backend, "handle_commit_and_push", mock_push), \
             patch.object(handler.forgejo_api_backend, "handle_create_pr", mock_pr), \
             patch.object(handler, "_notify_and_respond", new_callable=AsyncMock):
            await handler.handle_push_and_pr(1, {"arc_id": arc_id})

        assert arc_manager.get_arc(arc_id)["status"] == "failed"
        history = arc_manager.get_history(arc_id)
        assert any(h["entry_type"] == "pr_failed" for h in history)

    @pytest.mark.asyncio
    async def test_missing_workspace_marks_failed(self, test_db):
        """Missing workspace or branch fails the arc."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-missing-ws",
            goal="Missing workspace",
        )
        arc_manager.update_status(arc_id, "active")
        # No workspace or branch set

        await handler.handle_push_and_pr(1, {"arc_id": arc_id})

        assert arc_manager.get_arc(arc_id)["status"] == "failed"


# ---------------------------------------------------------------------------
# handle_local_review
# ---------------------------------------------------------------------------


class TestHandleLocalReview:
    @pytest.mark.asyncio
    async def test_no_changes_completes_arc(self, test_db):
        """No diff completes the arc without creating a review."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-review-empty",
            goal="Empty review",
        )
        arc_manager.update_status(arc_id, "active")
        _set_arc_state(arc_id, "workspace_path", "/tmp/ws/ext-review-empty")

        with patch("carpenter.core.workspace_manager.get_diff", return_value=""), \
             patch.object(handler, "_notify_and_respond", new_callable=AsyncMock):
            await handler.handle_local_review(1, {"arc_id": arc_id})

        assert arc_manager.get_arc(arc_id)["status"] == "completed"
        history = arc_manager.get_history(arc_id)
        assert any(h["entry_type"] == "no_changes" for h in history)

    @pytest.mark.asyncio
    async def test_with_changes_creates_review_and_waits(self, test_db):
        """Diff with changes creates review link and sets arc to waiting."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-review-ok",
            goal="Review with changes",
        )
        arc_manager.update_status(arc_id, "active")
        _set_arc_state(arc_id, "workspace_path", "/tmp/ws/ext-review-ok")

        fake_review = {"url": "/api/review/abc123", "review_id": "abc123"}

        with patch("carpenter.core.workspace_manager.get_diff", return_value="--- a\n+++ b\n-old\n+new\n"), \
             patch("carpenter.core.workspace_manager.get_changed_files", return_value=["file.py"]), \
             patch("carpenter.api.review.create_diff_review", return_value=fake_review), \
             patch.object(handler, "_notify_and_respond", new_callable=AsyncMock):
            await handler.handle_local_review(1, {"arc_id": arc_id})

        assert arc_manager.get_arc(arc_id)["status"] == "waiting"
        assert _get_arc_state(arc_id, "review_url") == "/api/review/abc123"
        assert _get_arc_state(arc_id, "review_id") == "abc123"
        assert _get_arc_state(arc_id, "diff") is not None
        assert _get_arc_state(arc_id, "changed_files") == ["file.py"]

    @pytest.mark.asyncio
    async def test_missing_workspace_marks_failed(self, test_db):
        """Missing workspace fails the arc."""
        arc_id = arc_manager.create_arc(
            name="external-coding-change-review-no-ws",
            goal="No workspace",
        )
        arc_manager.update_status(arc_id, "active")
        # No workspace set

        await handler.handle_local_review(1, {"arc_id": arc_id})

        assert arc_manager.get_arc(arc_id)["status"] == "failed"


# ---------------------------------------------------------------------------
# register_handlers
# ---------------------------------------------------------------------------


class TestRegisterHandlers:
    def test_registers_all_four_handlers(self):
        """All four handlers are registered with correct event types."""
        registered = {}

        def mock_register(event_type, handler_fn):
            registered[event_type] = handler_fn

        handler.register_handlers(mock_register)

        expected = {
            "external-coding-change.clone-and-branch",
            "external-coding-change.invoke-agent",
            "external-coding-change.local-review",
            "external-coding-change.push-and-pr",
        }
        assert set(registered.keys()) == expected

        # Verify they point to the actual handler functions
        assert registered["external-coding-change.clone-and-branch"] is handler.handle_clone_and_branch
        assert registered["external-coding-change.invoke-agent"] is handler.handle_invoke_agent
        assert registered["external-coding-change.local-review"] is handler.handle_local_review
        assert registered["external-coding-change.push-and-pr"] is handler.handle_push_and_pr
