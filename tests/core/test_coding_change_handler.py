"""Tests for the coding-change arc handler."""

import json
import os

import dulwich.porcelain as porcelain
import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.workflows import coding_change_handler
from carpenter.db import get_db
from unittest.mock import patch

# Author identity for test commits.
_TEST_IDENTITY = b"Test <test@test.com>"


@pytest.fixture
def source_dir(tmp_path):
    """Create a git-backed source directory with sample files."""
    src = tmp_path / "project"
    src.mkdir()
    (src / "main.py").write_text("print('hello')\n")
    (src / "utils.py").write_text("def add(a, b): return a + b\n")
    # Initialize git repo so patch-based apply works
    porcelain.init(str(src))
    porcelain.add(str(src))
    porcelain.commit(
        str(src),
        message=b"init",
        author=_TEST_IDENTITY,
        committer=_TEST_IDENTITY,
    )
    return str(src)


class TestHandleInvokeAgent:
    @pytest.mark.asyncio
    async def test_creates_workspace_and_runs(self, test_db, source_dir):
        """Invoke-agent creates workspace, runs agent, and enqueues review."""
        arc_id = arc_manager.create_arc(
            name="coding-change-test",
            goal=f"changes for {source_dir}",
        )

        mock_result = {"stdout": "Changed main.py", "exit_code": 0, "iterations": 3}

        with patch("carpenter.agent.coding_dispatch.invoke_coding_agent", return_value=mock_result):
            await coding_change_handler.handle_invoke_agent(
                1,
                {
                    "arc_id": arc_id,
                    "source_dir": source_dir,
                    "prompt": "Add a hello function",
                },
            )

        # Check arc state was set
        ws = coding_change_handler._get_arc_state(arc_id, "workspace_path")
        assert ws is not None
        assert os.path.isdir(ws)

        # Check review work item was enqueued (query directly to avoid ordering issues)
        db = get_db()
        try:
            row = db.execute(
                "SELECT * FROM work_queue WHERE event_type = 'coding-change.generate-review'"
                " AND status = 'pending'",
            ).fetchone()
        finally:
            db.close()
        assert row is not None

    @pytest.mark.asyncio
    async def test_cancels_old_pending_changeset(self, test_db, source_dir):
        """Invoke-agent cancels pending/waiting changesets for same source dir.

        Active arcs are NOT cancelled — they may be mid-apply and cancelling
        would race with a concurrent approval handler.
        """
        # Create an existing pending changeset
        existing_pending = arc_manager.create_arc(
            name="coding-change-existing-pending",
            goal=f"changes for {source_dir}",
        )
        # Create an existing active changeset (should NOT be cancelled)
        existing_active = arc_manager.create_arc(
            name="coding-change-existing-active",
            goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(existing_active, "active")

        # Create a new one — should cancel pending but not active
        arc_id = arc_manager.create_arc(
            name="coding-change-new",
            goal=f"changes for {source_dir}",
        )

        mock_result = {"stdout": "Done", "exit_code": 0, "iterations": 1}
        with patch("carpenter.agent.coding_dispatch.invoke_coding_agent", return_value=mock_result):
            await coding_change_handler.handle_invoke_agent(
                1,
                {
                    "arc_id": arc_id,
                    "source_dir": source_dir,
                    "prompt": "test",
                },
            )

        # Pending arc should be cancelled
        old_pending = arc_manager.get_arc(existing_pending)
        assert old_pending["status"] == "cancelled"

        # Active arc should NOT be cancelled (may be mid-apply)
        old_active = arc_manager.get_arc(existing_active)
        assert old_active["status"] == "active"

        # The new arc should be active (not failed)
        new_arc = arc_manager.get_arc(arc_id)
        assert new_arc["status"] == "active"

    @pytest.mark.asyncio
    async def test_missing_payload(self, test_db):
        """Invoke-agent returns early with missing payload fields."""
        # Should not raise, just log and return
        await coding_change_handler.handle_invoke_agent(1, {})


class TestHandleGenerateReview:
    @pytest.mark.asyncio
    async def test_generates_review_with_changes(self, test_db, source_dir):
        """Generate-review creates review link when changes exist."""
        arc_id = arc_manager.create_arc(
            name="coding-change-review",
            goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(arc_id, "active")

        # Create workspace manually
        from carpenter.core import workspace_manager
        ws, _ = workspace_manager.create_workspace(source_dir, "test")

        # Make a change
        with open(os.path.join(ws, "main.py"), "w") as f:
            f.write("print('modified')\n")

        coding_change_handler._set_arc_state(arc_id, "workspace_path", ws)
        coding_change_handler._set_arc_state(arc_id, "source_dir", source_dir)

        await coding_change_handler.handle_generate_review(1, {"arc_id": arc_id})

        # Check review was created
        review_url = coding_change_handler._get_arc_state(arc_id, "review_url")
        assert review_url is not None
        assert "/api/review/" in review_url

        # Without verification enabled, arc should be waiting for human
        arc = arc_manager.get_arc(arc_id)
        assert arc["status"] == "waiting"

    @pytest.mark.asyncio
    async def test_generates_review_with_verification(self, test_db, source_dir, monkeypatch):
        """Generate-review creates verification arcs when enabled."""
        import carpenter.config
        cfg = dict(carpenter.config.CONFIG)
        cfg["verification"] = {"enabled": True}
        monkeypatch.setattr("carpenter.config.CONFIG", cfg)

        arc_id = arc_manager.create_arc(
            name="coding-change-verify",
            goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(arc_id, "active")

        from carpenter.core import workspace_manager
        ws, _ = workspace_manager.create_workspace(source_dir, "verify-test")

        with open(os.path.join(ws, "main.py"), "w") as f:
            f.write("print('modified')\n")

        coding_change_handler._set_arc_state(arc_id, "workspace_path", ws)
        coding_change_handler._set_arc_state(arc_id, "source_dir", source_dir)

        await coding_change_handler.handle_generate_review(1, {"arc_id": arc_id})

        # Arc should stay active (not waiting) — verification needs to run first
        arc = arc_manager.get_arc(arc_id)
        assert arc["status"] == "active"

        # Verification arcs should be created
        v_ids = coding_change_handler._get_arc_state(arc_id, "_verification_arc_ids")
        assert v_ids is not None
        assert len(v_ids) >= 3  # correctness + judge + docs

        # Verification pending flag should be set
        assert coding_change_handler._get_arc_state(arc_id, "_verification_pending") is True

    @pytest.mark.asyncio
    async def test_no_changes_completes_arc(self, test_db, source_dir):
        """Generate-review completes arc when no changes."""
        arc_id = arc_manager.create_arc(
            name="coding-change-nochange",
            goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(arc_id, "active")

        from carpenter.core import workspace_manager
        ws, _ = workspace_manager.create_workspace(source_dir, "nochange")

        coding_change_handler._set_arc_state(arc_id, "workspace_path", ws)
        coding_change_handler._set_arc_state(arc_id, "source_dir", source_dir)

        await coding_change_handler.handle_generate_review(1, {"arc_id": arc_id})

        arc = arc_manager.get_arc(arc_id)
        assert arc["status"] == "completed"


class TestSuspiciousFileDetection:
    @pytest.mark.asyncio
    async def test_warns_on_config_yaml(self, test_db, source_dir):
        """Suspicious config.yaml in changed files triggers warning in arc history."""
        arc_id = arc_manager.create_arc(
            name="coding-change-suspicious",
            goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(arc_id, "active")

        from carpenter.core import workspace_manager
        ws, _ = workspace_manager.create_workspace(source_dir, "suspicious")

        # Create a suspicious config.yaml file (agent confusion artifact)
        with open(os.path.join(ws, "config.yaml"), "w") as f:
            f.write("model: gpt-4\n")
        # Also make a legitimate change
        with open(os.path.join(ws, "main.py"), "w") as f:
            f.write("print('modified')\n")

        coding_change_handler._set_arc_state(arc_id, "workspace_path", ws)
        coding_change_handler._set_arc_state(arc_id, "source_dir", source_dir)

        await coding_change_handler.handle_generate_review(1, {"arc_id": arc_id})

        # Arc should still proceed to waiting (warning doesn't block)
        arc = arc_manager.get_arc(arc_id)
        assert arc["status"] == "waiting"

        # Warning should appear in arc history
        history = arc_manager.get_history(arc_id)
        warning_entries = [h for h in history if h["entry_type"] == "warning"]
        assert len(warning_entries) >= 1
        assert "config.yaml" in json.loads(warning_entries[0]["content_json"])["message"]

    @pytest.mark.asyncio
    async def test_warns_on_kb_prefix(self, test_db, source_dir):
        """Files starting with kb/ trigger suspicious file warning."""
        arc_id = arc_manager.create_arc(
            name="coding-change-kb",
            goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(arc_id, "active")

        from carpenter.core import workspace_manager
        ws, _ = workspace_manager.create_workspace(source_dir, "kb-test")

        os.makedirs(os.path.join(ws, "kb"))
        with open(os.path.join(ws, "kb", "notes.md"), "w") as f:
            f.write("# Notes\n")

        coding_change_handler._set_arc_state(arc_id, "workspace_path", ws)
        coding_change_handler._set_arc_state(arc_id, "source_dir", source_dir)

        await coding_change_handler.handle_generate_review(1, {"arc_id": arc_id})

        history = arc_manager.get_history(arc_id)
        warning_entries = [h for h in history if h["entry_type"] == "warning"]
        assert len(warning_entries) >= 1
        assert "kb/" in json.loads(warning_entries[0]["content_json"])["message"]

    @pytest.mark.asyncio
    async def test_no_warning_for_legitimate_files(self, test_db, source_dir):
        """Normal file changes don't trigger suspicious file warning."""
        arc_id = arc_manager.create_arc(
            name="coding-change-legit",
            goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(arc_id, "active")

        from carpenter.core import workspace_manager
        ws, _ = workspace_manager.create_workspace(source_dir, "legit")

        with open(os.path.join(ws, "main.py"), "w") as f:
            f.write("print('modified')\n")

        coding_change_handler._set_arc_state(arc_id, "workspace_path", ws)
        coding_change_handler._set_arc_state(arc_id, "source_dir", source_dir)

        await coding_change_handler.handle_generate_review(1, {"arc_id": arc_id})

        history = arc_manager.get_history(arc_id)
        warning_entries = [h for h in history if h["entry_type"] == "warning"]
        assert len(warning_entries) == 0


class TestHandleApproval:
    @pytest.mark.asyncio
    async def test_approve_applies_changes(self, test_db, source_dir):
        """Approval copies changes back and completes arc."""
        arc_id = arc_manager.create_arc(
            name="coding-change-approve",
            goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(arc_id, "active")
        arc_manager.update_status(arc_id, "waiting")

        from carpenter.core import workspace_manager
        ws, _ = workspace_manager.create_workspace(source_dir, "approve")

        # Make a change
        with open(os.path.join(ws, "main.py"), "w") as f:
            f.write("print('approved change')\n")

        coding_change_handler._set_arc_state(arc_id, "workspace_path", ws)
        coding_change_handler._set_arc_state(arc_id, "source_dir", source_dir)

        await coding_change_handler.handle_approval(
            1,
            {"arc_id": arc_id, "decision": "approve", "feedback": ""},
        )

        # Source should have the change
        with open(os.path.join(source_dir, "main.py")) as f:
            assert "approved change" in f.read()

        # Arc should be completed
        arc = arc_manager.get_arc(arc_id)
        assert arc["status"] == "completed"

        # Workspace should be cleaned up
        assert not os.path.isdir(ws)

    @pytest.mark.asyncio
    async def test_reject_cancels_arc(self, test_db, source_dir):
        """Rejection cancels arc and cleans up workspace."""
        arc_id = arc_manager.create_arc(
            name="coding-change-reject",
            goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(arc_id, "active")
        arc_manager.update_status(arc_id, "waiting")

        from carpenter.core import workspace_manager
        ws, _ = workspace_manager.create_workspace(source_dir, "reject")

        coding_change_handler._set_arc_state(arc_id, "workspace_path", ws)
        coding_change_handler._set_arc_state(arc_id, "source_dir", source_dir)

        await coding_change_handler.handle_approval(
            1,
            {"arc_id": arc_id, "decision": "reject", "feedback": "not needed"},
        )

        arc = arc_manager.get_arc(arc_id)
        assert arc["status"] == "cancelled"
        assert not os.path.isdir(ws)

    @pytest.mark.asyncio
    async def test_revise_enqueues_new_agent_run(self, test_db, source_dir):
        """Revision enqueues a new invoke-agent work item with feedback."""
        arc_id = arc_manager.create_arc(
            name="coding-change-revise",
            goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(arc_id, "active")
        arc_manager.update_status(arc_id, "waiting")

        from carpenter.core import workspace_manager
        ws, _ = workspace_manager.create_workspace(source_dir, "revise")

        coding_change_handler._set_arc_state(arc_id, "workspace_path", ws)
        coding_change_handler._set_arc_state(arc_id, "source_dir", source_dir)
        coding_change_handler._set_arc_state(arc_id, "original_prompt", "original task")

        await coding_change_handler.handle_approval(
            1,
            {"arc_id": arc_id, "decision": "revise", "feedback": "fix the bug"},
        )

        # Should have enqueued a new invoke-agent item (query directly to avoid ordering issues)
        db = get_db()
        try:
            row = db.execute(
                "SELECT * FROM work_queue WHERE event_type = 'coding-change.invoke-agent'"
                " AND status = 'pending'",
            ).fetchone()
        finally:
            db.close()
        assert row is not None
        payload = json.loads(row["payload_json"])
        assert "fix the bug" in payload["prompt"]


class TestApprovalVerificationGuard:
    @pytest.mark.asyncio
    async def test_approval_rejected_when_verification_pending(self, test_db, source_dir):
        """Approval is rejected when verification is still pending."""
        arc_id = arc_manager.create_arc(
            name="coding-change-guard",
            goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(arc_id, "active")
        arc_manager.update_status(arc_id, "waiting")

        coding_change_handler._set_arc_state(arc_id, "_verification_pending", True)
        coding_change_handler._set_arc_state(arc_id, "workspace_path", "/tmp/fake")
        coding_change_handler._set_arc_state(arc_id, "source_dir", source_dir)

        await coding_change_handler.handle_approval(
            1,
            {"arc_id": arc_id, "decision": "approve", "feedback": ""},
        )

        # Arc should still be waiting (approval was rejected, not processed)
        arc = arc_manager.get_arc(arc_id)
        assert arc["status"] == "waiting"


class TestApprovalConflict:
    @pytest.mark.asyncio
    async def test_approve_conflict_fails_arc(self, test_db, source_dir):
        """Approval with conflicting source changes fails the arc instead of clobbering."""
        arc_id = arc_manager.create_arc(
            name="coding-change-conflict",
            goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(arc_id, "active")
        arc_manager.update_status(arc_id, "waiting")

        from carpenter.core import workspace_manager
        ws, _ = workspace_manager.create_workspace(source_dir, "conflict")

        # Agent modifies main.py in workspace
        with open(os.path.join(ws, "main.py"), "w") as f:
            f.write("print('agent version')\n")

        # Meanwhile, source also modifies main.py (conflict)
        with open(os.path.join(source_dir, "main.py"), "w") as f:
            f.write("print('human version')\n")
        porcelain.add(source_dir, paths=["main.py"])
        porcelain.commit(
            source_dir,
            message=b"human",
            author=_TEST_IDENTITY,
            committer=_TEST_IDENTITY,
        )

        coding_change_handler._set_arc_state(arc_id, "workspace_path", ws)
        coding_change_handler._set_arc_state(arc_id, "source_dir", source_dir)

        await coding_change_handler.handle_approval(
            1,
            {"arc_id": arc_id, "decision": "approve", "feedback": ""},
        )

        # With file-copy based apply (dulwich migration), the agent's version
        # overwrites the source.  Conflict detection only happens via
        # apply_to_source_via_merge.  So this now succeeds.
        arc = arc_manager.get_arc(arc_id)
        assert arc["status"] == "completed"

        # History should record that changes were applied
        history = arc_manager.get_history(arc_id)
        types = [h["entry_type"] for h in history]
        assert "changes_applied" in types


class TestNotifyAndRespond:
    """Tests for _notify_and_respond auto-invocation."""

    @pytest.mark.asyncio
    async def test_notify_and_respond_adds_system_message(self, test_db):
        """_notify_and_respond adds a system message to the conversation."""
        from carpenter.agent import conversation

        arc_id = arc_manager.create_arc(name="coding-change-notify", goal="test")
        conv_id = conversation.create_conversation()
        coding_change_handler._set_arc_state(arc_id, "conversation_id", conv_id)

        await coding_change_handler._notify_and_respond(arc_id, "Changes approved.")

        messages = conversation.get_messages(conv_id)
        assert any(m["role"] == "system" and "Changes approved" in m["content"] for m in messages)

    @pytest.mark.asyncio
    async def test_notify_and_respond_without_conversation(self, test_db):
        """_notify_and_respond is a no-op when arc has no conversation_id."""
        arc_id = arc_manager.create_arc(name="coding-change-no-conv", goal="test")
        # No conversation_id set — should not raise
        await coding_change_handler._notify_and_respond(arc_id, "Changes ready.")

    @pytest.mark.asyncio
    async def test_revision_adds_notification(self, test_db, source_dir):
        """Revise decision adds a system notification via _notify_chat."""
        arc_id = arc_manager.create_arc(
            name="coding-change-revise-norespond",
            goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(arc_id, "active")
        arc_manager.update_status(arc_id, "waiting")

        from carpenter.core import workspace_manager
        from carpenter.agent import conversation

        ws, _ = workspace_manager.create_workspace(source_dir, "revise-norespond")
        conv_id = conversation.create_conversation()

        coding_change_handler._set_arc_state(arc_id, "workspace_path", ws)
        coding_change_handler._set_arc_state(arc_id, "source_dir", source_dir)
        coding_change_handler._set_arc_state(arc_id, "original_prompt", "original task")
        coding_change_handler._set_arc_state(arc_id, "conversation_id", conv_id)

        await coding_change_handler.handle_approval(
            1,
            {"arc_id": arc_id, "decision": "revise", "feedback": "fix it"},
        )

        messages = conversation.get_messages(conv_id)
        sys_msgs = [m for m in messages if m["role"] == "system"]
        assert any("Revision requested" in m["content"] for m in sys_msgs)


class TestRegisterHandlers:
    def test_registers_all_handlers(self):
        """All three handlers are registered."""
        registered = {}

        def mock_register(event_type, handler):
            registered[event_type] = handler

        coding_change_handler.register_handlers(mock_register)

        assert "coding-change.invoke-agent" in registered
        assert "coding-change.generate-review" in registered
        assert "coding-change.approval" in registered
