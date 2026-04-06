"""Tests for verification of external-coding-change arcs.

Verifies:
- Verification arcs are created after external agent completes
- Pass verdict enqueues push-and-pr (or local-review)
- Fail verdict reworks the external agent
- Disabled verification goes directly to push
"""

import json

import pytest
from unittest.mock import patch, AsyncMock

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import work_queue
from carpenter.core.arcs import dispatch_handler as arc_dispatch_handler
from carpenter.core.workflows.coding_change_handler import _get_arc_state, _set_arc_state
from carpenter.core.arcs.verification import (
    CORRECTNESS_CHECK, QUALITY_CHECK, JUDGE_VERIFICATION, DOCUMENTATION_ARC,
)
from carpenter.core.workflows import external_coding_change_handler
from carpenter.db import get_db


def _create_verification_set(parent_id, target_id):
    """Create minimal verification arcs for an external-coding-change target."""
    ids = {}
    base_order = 10

    ids["correctness"] = arc_manager.create_arc(
        name=CORRECTNESS_CHECK,
        goal="Check correctness",
        parent_id=parent_id,
        step_order=base_order,
        arc_role="verifier",
        verification_target_id=target_id,
        agent_type="REVIEWER",
    )

    ids["judge"] = arc_manager.create_arc(
        name=JUDGE_VERIFICATION,
        goal="Aggregate results",
        parent_id=parent_id,
        step_order=base_order + 1,
        arc_role="verifier",
        verification_target_id=target_id,
        agent_type="EXECUTOR",
    )

    ids["docs"] = arc_manager.create_arc(
        name=DOCUMENTATION_ARC,
        goal="Write docs",
        parent_id=parent_id,
        step_order=base_order + 2,
        arc_role="worker",
        verification_target_id=target_id,
        agent_type="EXECUTOR",
    )

    return ids


class TestExternalCodingVerification:
    """Tests for verification arc creation on external-coding-change."""

    @pytest.mark.asyncio
    async def test_verification_arcs_created(self, test_db):
        """After agent completes, verification arcs should be created."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(
            parent, "external-coding-change", goal="External fix",
        )
        arc_manager.update_status(target, "active")
        _set_arc_state(target, "workspace_path", "/tmp/ext-test")
        _set_arc_state(target, "prompt", "Fix the bug")

        mock_result = {"exit_code": 0, "iterations": 1, "stdout": "Done"}

        with patch("carpenter.core.workflows.external_coding_change_handler.config") as mock_config, \
             patch("carpenter.thread_pools.run_in_work_pool", return_value=mock_result), \
             patch("carpenter.core.arcs.verification.should_create_verification_arcs", return_value=True), \
             patch("carpenter.core.arcs.verification.create_verification_arcs", return_value=[100, 101, 102]) as mock_create:
            mock_config.CONFIG = {"workspaces_dir": "/tmp"}

            await external_coding_change_handler.handle_invoke_agent(
                1, {"arc_id": target},
            )

        # Verification arcs should be created
        assert mock_create.called
        assert _get_arc_state(target, "_verification_pending") is True
        v_ids = _get_arc_state(target, "_verification_arc_ids")
        assert v_ids == [100, 101, 102]

    @pytest.mark.asyncio
    async def test_disabled_verification_goes_to_push(self, test_db):
        """When verification is disabled, should go directly to push-and-pr."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(
            parent, "external-coding-change", goal="External fix",
        )
        arc_manager.update_status(target, "active")
        _set_arc_state(target, "workspace_path", "/tmp/ext-test")

        mock_result = {"exit_code": 0, "iterations": 1, "stdout": "Done"}

        with patch("carpenter.core.workflows.external_coding_change_handler.config") as mock_config, \
             patch("carpenter.thread_pools.run_in_work_pool", return_value=mock_result), \
             patch("carpenter.core.arcs.verification.should_create_verification_arcs", return_value=False):
            mock_config.CONFIG = {"workspaces_dir": "/tmp"}

            await external_coding_change_handler.handle_invoke_agent(
                1, {"arc_id": target},
            )

        # Should have enqueued push-and-pr directly
        db = get_db()
        try:
            row = db.execute(
                "SELECT payload_json FROM work_queue "
                "WHERE event_type = 'external-coding-change.push-and-pr' "
                "AND status = 'pending'",
            ).fetchone()
        finally:
            db.close()
        assert row is not None

    @pytest.mark.asyncio
    async def test_disabled_verification_with_local_review(self, test_db):
        """When verification disabled and local_review=True, go to local-review."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(
            parent, "external-coding-change", goal="External fix",
        )
        arc_manager.update_status(target, "active")
        _set_arc_state(target, "workspace_path", "/tmp/ext-test")
        _set_arc_state(target, "local_review", True)

        mock_result = {"exit_code": 0, "iterations": 1, "stdout": "Done"}

        with patch("carpenter.core.workflows.external_coding_change_handler.config") as mock_config, \
             patch("carpenter.thread_pools.run_in_work_pool", return_value=mock_result), \
             patch("carpenter.core.arcs.verification.should_create_verification_arcs", return_value=False):
            mock_config.CONFIG = {"workspaces_dir": "/tmp"}

            await external_coding_change_handler.handle_invoke_agent(
                1, {"arc_id": target},
            )

        # Should have enqueued local-review
        db = get_db()
        try:
            row = db.execute(
                "SELECT payload_json FROM work_queue "
                "WHERE event_type = 'external-coding-change.local-review' "
                "AND status = 'pending'",
            ).fetchone()
        finally:
            db.close()
        assert row is not None


class TestExternalDocsCompletion:
    """Tests for docs completion on external-coding-change targets."""

    def test_docs_completed_enqueues_push_for_external(self, test_db):
        """Docs completed for external target should enqueue push-and-pr."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(
            parent, "external-coding-change", goal="External fix",
        )
        arc_manager.update_status(target, "active")
        _set_arc_state(target, "_verification_pending", True)

        docs_id = arc_manager.create_arc(
            name=DOCUMENTATION_ARC,
            goal="Write docs",
            parent_id=parent,
            verification_target_id=target,
            agent_type="EXECUTOR",
        )

        docs_info = arc_manager.get_arc(docs_id)
        arc_dispatch_handler._handle_docs_completed(docs_id, docs_info)

        # Verification pending should be cleared
        assert _get_arc_state(target, "_verification_pending") is False

        # Should have enqueued push-and-pr
        db = get_db()
        try:
            row = db.execute(
                "SELECT payload_json FROM work_queue "
                "WHERE event_type = 'external-coding-change.push-and-pr' "
                "AND status = 'pending'",
            ).fetchone()
        finally:
            db.close()
        assert row is not None

    def test_docs_completed_enqueues_local_review_for_external(self, test_db):
        """Docs completed for external with local_review should enqueue review."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(
            parent, "external-coding-change", goal="External fix",
        )
        arc_manager.update_status(target, "active")
        _set_arc_state(target, "_verification_pending", True)
        _set_arc_state(target, "local_review", True)

        docs_id = arc_manager.create_arc(
            name=DOCUMENTATION_ARC,
            goal="Write docs",
            parent_id=parent,
            verification_target_id=target,
            agent_type="EXECUTOR",
        )

        docs_info = arc_manager.get_arc(docs_id)
        arc_dispatch_handler._handle_docs_completed(docs_id, docs_info)

        # Should have enqueued local-review
        db = get_db()
        try:
            row = db.execute(
                "SELECT payload_json FROM work_queue "
                "WHERE event_type = 'external-coding-change.local-review' "
                "AND status = 'pending'",
            ).fetchone()
        finally:
            db.close()
        assert row is not None


class TestExternalVerificationRework:
    """Tests for verification rework on external-coding-change targets."""

    @pytest.mark.asyncio
    async def test_fail_reworks_external_agent(self, test_db):
        """Verification fail should rework external agent (not coding-change)."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(
            parent, "external-coding-change", goal="External fix",
        )
        arc_manager.update_status(target, "active")
        _set_arc_state(target, "original_prompt", "Fix the bug")
        _set_arc_state(target, "source_dir", "/tmp/ext-test")

        v = _create_verification_set(parent, target)

        # Fail correctness
        arc_manager.update_status(v["correctness"], "active")
        arc_manager.update_status(v["correctness"], "failed")

        judge_info = arc_manager.get_arc(v["judge"])
        await arc_dispatch_handler._handle_judge_verification(v["judge"], judge_info)

        # Should enqueue external-coding-change.invoke-agent (not coding-change)
        db = get_db()
        try:
            row = db.execute(
                "SELECT payload_json FROM work_queue "
                "WHERE event_type = 'external-coding-change.invoke-agent' "
                "AND status = 'pending'",
            ).fetchone()
        finally:
            db.close()
        assert row is not None
        payload = json.loads(row["payload_json"])
        assert payload["arc_id"] == target
        assert "VERIFICATION FEEDBACK" in payload["prompt"]
