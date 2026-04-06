"""Tests for the judge-verification Python handler.

Verifies:
- Boolean aggregation of sibling verification arcs
- PASS verdict when all checks complete
- FAIL verdict when any check fails
- Verification rework loop (re-invoke coding agent on failure)
- Rework limit exhaustion → proceed to human review
- post-verification-docs completion → transition to waiting
"""

import json

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.arcs import dispatch_handler as arc_dispatch_handler
from carpenter.core.workflows.coding_change_handler import _get_arc_state, _set_arc_state
from carpenter.core.arcs.verification import (
    CORRECTNESS_CHECK, QUALITY_CHECK, JUDGE_VERIFICATION, DOCUMENTATION_ARC,
)
from carpenter.db import get_db


def _create_verification_set(parent_id, target_id, *, include_quality=False):
    """Create a minimal set of verification arcs for testing.

    Returns dict with arc IDs keyed by name.
    """
    ids = {}
    base_order = 10  # Arbitrary high base to avoid conflicts

    if include_quality:
        ids["quality"] = arc_manager.create_arc(
            name=QUALITY_CHECK,
            goal="Check quality",
            parent_id=parent_id,
            step_order=base_order,
            arc_role="verifier",
            verification_target_id=target_id,
            agent_type="REVIEWER",
        )

    ids["correctness"] = arc_manager.create_arc(
        name=CORRECTNESS_CHECK,
        goal="Check correctness",
        parent_id=parent_id,
        step_order=base_order + (1 if include_quality else 0),
        arc_role="verifier",
        verification_target_id=target_id,
        agent_type="REVIEWER",
    )

    ids["judge"] = arc_manager.create_arc(
        name=JUDGE_VERIFICATION,
        goal="Aggregate results",
        parent_id=parent_id,
        step_order=base_order + (2 if include_quality else 1),
        arc_role="verifier",
        verification_target_id=target_id,
        agent_type="EXECUTOR",
    )

    ids["docs"] = arc_manager.create_arc(
        name=DOCUMENTATION_ARC,
        goal="Write docs",
        parent_id=parent_id,
        step_order=base_order + (3 if include_quality else 2),
        arc_role="worker",
        verification_target_id=target_id,
        agent_type="EXECUTOR",
    )

    return ids


class TestJudgeVerificationPass:
    """Tests for judge-verification PASS verdict."""

    @pytest.mark.asyncio
    async def test_all_checks_passed(self, test_db):
        """Judge passes when all verification checks completed."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(target, "active")

        v = _create_verification_set(parent, target)

        # Complete the correctness check
        arc_manager.update_status(v["correctness"], "active")
        arc_manager.update_status(v["correctness"], "completed")

        judge_info = arc_manager.get_arc(v["judge"])
        await arc_dispatch_handler._handle_judge_verification(v["judge"], judge_info)

        # Judge should be completed
        judge = arc_manager.get_arc(v["judge"])
        assert judge["status"] in ("completed", "frozen")

        # Verdict should be pass
        db = get_db()
        try:
            row = db.execute(
                "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = 'verdict'",
                (v["judge"],),
            ).fetchone()
        finally:
            db.close()
        assert row is not None
        verdict = json.loads(row["value_json"])
        assert verdict["verdict"] == "pass"

        # Docs arc should NOT be cancelled (it should run next)
        docs = arc_manager.get_arc(v["docs"])
        assert docs["status"] == "pending"

    @pytest.mark.asyncio
    async def test_all_checks_passed_with_quality(self, test_db):
        """Judge passes when both quality and correctness checks completed."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(target, "active")

        v = _create_verification_set(parent, target, include_quality=True)

        # Complete both checks
        arc_manager.update_status(v["quality"], "active")
        arc_manager.update_status(v["quality"], "completed")
        arc_manager.update_status(v["correctness"], "active")
        arc_manager.update_status(v["correctness"], "completed")

        judge_info = arc_manager.get_arc(v["judge"])
        await arc_dispatch_handler._handle_judge_verification(v["judge"], judge_info)

        # Judge should pass
        db = get_db()
        try:
            row = db.execute(
                "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = 'verdict'",
                (v["judge"],),
            ).fetchone()
        finally:
            db.close()
        verdict = json.loads(row["value_json"])
        assert verdict["verdict"] == "pass"
        assert len(verdict["checks"]) == 2


class TestJudgeVerificationFail:
    """Tests for judge-verification FAIL verdict."""

    @pytest.mark.asyncio
    async def test_correctness_failed(self, test_db):
        """Judge fails when correctness check failed."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(target, "active")
        _set_arc_state(target, "original_prompt", "Write a function")
        _set_arc_state(target, "source_dir", "/tmp/test")

        v = _create_verification_set(parent, target)

        # Fail the correctness check
        arc_manager.update_status(v["correctness"], "active")
        arc_manager.update_status(v["correctness"], "failed")

        judge_info = arc_manager.get_arc(v["judge"])
        await arc_dispatch_handler._handle_judge_verification(v["judge"], judge_info)

        # Judge should be completed (with fail verdict)
        judge = arc_manager.get_arc(v["judge"])
        assert judge["status"] in ("completed", "frozen")

        # Verdict should be fail
        db = get_db()
        try:
            row = db.execute(
                "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = 'verdict'",
                (v["judge"],),
            ).fetchone()
        finally:
            db.close()
        verdict = json.loads(row["value_json"])
        assert verdict["verdict"] == "fail"

        # Docs arc should be cancelled
        docs = arc_manager.get_arc(v["docs"])
        assert docs["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_quality_failed(self, test_db):
        """Judge fails when quality check failed (even if correctness passed)."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(target, "active")
        _set_arc_state(target, "original_prompt", "Write a function")
        _set_arc_state(target, "source_dir", "/tmp/test")

        v = _create_verification_set(parent, target, include_quality=True)

        # Fail quality, pass correctness
        arc_manager.update_status(v["quality"], "active")
        arc_manager.update_status(v["quality"], "failed")
        arc_manager.update_status(v["correctness"], "active")
        arc_manager.update_status(v["correctness"], "completed")

        judge_info = arc_manager.get_arc(v["judge"])
        await arc_dispatch_handler._handle_judge_verification(v["judge"], judge_info)

        db = get_db()
        try:
            row = db.execute(
                "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = 'verdict'",
                (v["judge"],),
            ).fetchone()
        finally:
            db.close()
        verdict = json.loads(row["value_json"])
        assert verdict["verdict"] == "fail"


class TestVerificationRework:
    """Tests for verification-driven rework loop."""

    @pytest.mark.asyncio
    async def test_rework_re_enqueues_coding_agent(self, test_db):
        """On first failure, judge re-enqueues the coding agent with feedback."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(target, "active")
        _set_arc_state(target, "original_prompt", "Write a function")
        _set_arc_state(target, "source_dir", "/tmp/test")

        v = _create_verification_set(parent, target)

        # Fail correctness
        arc_manager.update_status(v["correctness"], "active")
        arc_manager.update_status(v["correctness"], "failed")

        judge_info = arc_manager.get_arc(v["judge"])
        await arc_dispatch_handler._handle_judge_verification(v["judge"], judge_info)

        # _retry_count should be incremented (via arc_retry system)
        from carpenter.core.arcs import retry as arc_retry
        retry_state = arc_retry.get_retry_state(target)
        assert retry_state.get("_retry_count", 0) == 1

        # Verification pending should be cleared (new cycle will set it)
        assert _get_arc_state(target, "_verification_pending") is False

        # A coding-change.invoke-agent work item should be enqueued
        db = get_db()
        try:
            row = db.execute(
                "SELECT payload_json FROM work_queue "
                "WHERE event_type = 'coding-change.invoke-agent' "
                "AND status = 'pending'",
            ).fetchone()
        finally:
            db.close()
        assert row is not None
        payload = json.loads(row["payload_json"])
        assert payload["arc_id"] == target
        assert "VERIFICATION FEEDBACK" in payload["prompt"]

    @pytest.mark.asyncio
    async def test_rework_limit_proceeds_to_human(self, test_db):
        """After rework limit, judge proceeds to human review."""
        from carpenter.core.arcs import retry as arc_retry

        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(target, "active")
        _set_arc_state(target, "original_prompt", "Write a function")
        _set_arc_state(target, "source_dir", "/tmp/test")

        # Pre-exhaust retry budget: set _retry_count to max (2)
        arc_retry.initialize_retry_state(target, max_retries=2)
        db = get_db()
        try:
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) "
                "VALUES (?, '_retry_count', '2') "
                "ON CONFLICT(arc_id, key) DO UPDATE SET value_json = '2'",
                (target,),
            )
            db.commit()
        finally:
            db.close()

        v = _create_verification_set(parent, target)

        # Fail correctness
        arc_manager.update_status(v["correctness"], "active")
        arc_manager.update_status(v["correctness"], "failed")

        judge_info = arc_manager.get_arc(v["judge"])
        await arc_dispatch_handler._handle_judge_verification(v["judge"], judge_info)

        # Target arc should be transitioned to waiting for human review
        target_arc = arc_manager.get_arc(target)
        assert target_arc["status"] == "waiting"

        # Verification pending should be cleared
        assert _get_arc_state(target, "_verification_pending") is False

        # No new invoke-agent work item should be enqueued
        db = get_db()
        try:
            row = db.execute(
                "SELECT payload_json FROM work_queue "
                "WHERE event_type = 'coding-change.invoke-agent' "
                "AND status = 'pending'",
            ).fetchone()
        finally:
            db.close()
        assert row is None

    @pytest.mark.asyncio
    async def test_verification_error_reads_from_config(self, test_db):
        """Verification rework limit should respect arc_retry config."""
        from carpenter.core.arcs import retry as arc_retry

        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(target, "active")
        _set_arc_state(target, "original_prompt", "Write a function")
        _set_arc_state(target, "source_dir", "/tmp/test")

        v = _create_verification_set(parent, target)

        # Fail correctness — first attempt
        arc_manager.update_status(v["correctness"], "active")
        arc_manager.update_status(v["correctness"], "failed")

        judge_info = arc_manager.get_arc(v["judge"])
        await arc_dispatch_handler._handle_judge_verification(v["judge"], judge_info)

        # Should have used arc_retry (VerificationError max_retries=2 from config)
        retry_state = arc_retry.get_retry_state(target)
        assert retry_state.get("_retry_count", 0) == 1
        last_error = retry_state.get("_last_error", {})
        assert last_error.get("error_info", {}).get("type") == "VerificationError"

    @pytest.mark.asyncio
    async def test_rework_increments_general_rework_count(self, test_db):
        """Verification rework also bumps general rework_count for workspace reuse."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(target, "active")
        _set_arc_state(target, "original_prompt", "Write a function")
        _set_arc_state(target, "source_dir", "/tmp/test")
        _set_arc_state(target, "rework_count", 0)

        v = _create_verification_set(parent, target)

        arc_manager.update_status(v["correctness"], "active")
        arc_manager.update_status(v["correctness"], "failed")

        judge_info = arc_manager.get_arc(v["judge"])
        await arc_dispatch_handler._handle_judge_verification(v["judge"], judge_info)

        # General rework_count should be bumped so workspace is reused
        assert _get_arc_state(target, "rework_count") == 1


class TestDocsCompletion:
    """Tests for post-verification-docs completion hook."""

    def test_docs_completion_transitions_target_to_waiting(self, test_db):
        """When docs arc completes, target coding-change transitions to waiting."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(target, "active")

        docs_id = arc_manager.create_arc(
            name=DOCUMENTATION_ARC,
            goal="Write docs",
            parent_id=parent,
            verification_target_id=target,
            agent_type="EXECUTOR",
        )

        docs_info = arc_manager.get_arc(docs_id)
        arc_dispatch_handler._handle_docs_completed(docs_id, docs_info)

        # Target should be waiting for human approval
        target_arc = arc_manager.get_arc(target)
        assert target_arc["status"] == "waiting"

        # Verification pending should be cleared
        assert _get_arc_state(target, "_verification_pending") is False

    def test_docs_completion_noop_for_completed_target(self, test_db):
        """Docs completion is a no-op if target is already completed."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(target, "active")
        arc_manager.update_status(target, "completed")

        docs_id = arc_manager.create_arc(
            name=DOCUMENTATION_ARC,
            goal="Write docs",
            parent_id=parent,
            verification_target_id=target,
            agent_type="EXECUTOR",
        )

        docs_info = arc_manager.get_arc(docs_id)
        arc_dispatch_handler._handle_docs_completed(docs_id, docs_info)

        # Target should remain completed (not changed to waiting)
        target_arc = arc_manager.get_arc(target)
        assert target_arc["status"] == "completed"

    def test_docs_completion_noop_without_target(self, test_db):
        """Docs completion is a no-op if no verification_target_id."""
        parent = arc_manager.create_arc("project", goal="Test")
        docs_id = arc_manager.create_arc(
            name=DOCUMENTATION_ARC,
            goal="Write docs",
            parent_id=parent,
            agent_type="EXECUTOR",
        )

        docs_info = arc_manager.get_arc(docs_id)
        # Should not raise
        arc_dispatch_handler._handle_docs_completed(docs_id, docs_info)


class TestDocsFailureUnblock:
    """Tests for docs arc failure unblocking the pipeline."""

    def test_failed_docs_unblocks_target(self, test_db):
        """Failed docs arc should unblock the target coding-change arc."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(target, "active")
        _set_arc_state(target, "_verification_pending", True)

        docs_id = arc_manager.create_arc(
            name=DOCUMENTATION_ARC,
            goal="Write docs",
            parent_id=parent,
            verification_target_id=target,
            agent_type="EXECUTOR",
        )

        arc_dispatch_handler._handle_failed_docs_arc(docs_id)

        # Target should be transitioned to waiting
        target_arc = arc_manager.get_arc(target)
        assert target_arc["status"] == "waiting"

        # Verification pending should be cleared
        assert _get_arc_state(target, "_verification_pending") is False

    def test_non_docs_arc_is_noop(self, test_db):
        """_handle_failed_docs_arc should be a no-op for non-docs arcs."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(target, "active")

        # Create a non-docs arc
        other_id = arc_manager.create_arc(
            name="verify-correctness",
            goal="Check correctness",
            parent_id=parent,
            verification_target_id=target,
            agent_type="REVIEWER",
        )

        arc_dispatch_handler._handle_failed_docs_arc(other_id)

        # Target should NOT be changed
        target_arc = arc_manager.get_arc(target)
        assert target_arc["status"] == "active"

    def test_failed_docs_unblocks_external_target(self, test_db):
        """Failed docs for external-coding-change should enqueue push step."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(
            parent, "external-coding-change", goal="External change",
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

        arc_dispatch_handler._handle_failed_docs_arc(docs_id)

        # Verification pending should be cleared
        assert _get_arc_state(target, "_verification_pending") is False

        # Should have enqueued a push-and-pr work item
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


class TestJudgeVerificationMissingTarget:
    """Edge case tests."""

    @pytest.mark.asyncio
    async def test_judge_without_target_fails(self, test_db):
        """Judge with no verification_target_id fails gracefully."""
        parent = arc_manager.create_arc("project", goal="Test")
        judge_id = arc_manager.create_arc(
            name=JUDGE_VERIFICATION,
            goal="Aggregate",
            parent_id=parent,
            agent_type="EXECUTOR",
        )

        judge_info = arc_manager.get_arc(judge_id)
        await arc_dispatch_handler._handle_judge_verification(judge_id, judge_info)

        # Should fail gracefully
        judge = arc_manager.get_arc(judge_id)
        assert judge["status"] == "failed"
