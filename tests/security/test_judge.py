"""Tests for carpenter.security.judge (deterministic JUDGE)."""

import json
import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.workflows import review_manager
from carpenter.security.judge import (
    run_policy_checks,
    _get_review_target,
    _get_extraction_data,
    JudgeResult,
    PolicyCheck,
)
from carpenter.security import policy_store
from carpenter.tool_backends import arc as arc_backend
from carpenter.db import get_db


def _create_batch_with_judge(extra_arcs=None, parent_id=None):
    """Helper to create a standard batch with untrusted + reviewer + judge."""
    arcs = [
        {
            "name": "target",
            "integrity_level": "untrusted",
        },
        {
            "name": "reviewer",
            "agent_type": "REVIEWER",
            "reviewer_profile": "security-reviewer",
        },
        {
            "name": "judge",
            "agent_type": "JUDGE",
            "reviewer_profile": "judge",
        },
    ]
    if extra_arcs:
        arcs.extend(extra_arcs)
    if parent_id:
        for a in arcs:
            a["parent_id"] = parent_id

    result = arc_backend.handle_create_batch({"arcs": arcs})
    assert "arc_ids" in result
    return result["arc_ids"]


class TestGetReviewTarget:

    def test_returns_target_id(self):
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        # Judge should have _review_target pointing to the target
        assert _get_review_target(judge_id) == target_id

    def test_returns_none_for_non_judge(self):
        arc_id = arc_manager.create_arc("regular")
        assert _get_review_target(arc_id) is None


class TestGetExtractionData:

    def test_returns_none_when_no_data(self):
        ids = _create_batch_with_judge()
        target_id = ids[0]
        assert _get_extraction_data(target_id) is None

    def test_reads_judge_policy_checks_from_target(self):
        ids = _create_batch_with_judge()
        target_id = ids[0]

        # Set extraction data on target arc state
        checks = [
            {"field": "email", "policy_type": "email", "value": "admin@test.com"},
        ]
        db = get_db()
        try:
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                (target_id, "_judge_policy_checks", json.dumps(checks)),
            )
            db.commit()
        finally:
            db.close()

        result = _get_extraction_data(target_id)
        assert result is not None
        assert len(result) == 1
        assert result[0]["field"] == "email"

    def test_reads_extraction_output_from_reviewer(self):
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        # Set extraction output on reviewer's arc state
        extraction = [
            {"field": "domain", "policy_type": "domain", "value": "safe.example.com"},
        ]
        db = get_db()
        try:
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                (reviewer_id, "_extraction_output", json.dumps(extraction)),
            )
            db.commit()
        finally:
            db.close()

        result = _get_extraction_data(target_id)
        assert result is not None
        assert result[0]["field"] == "domain"


class TestRunPolicyChecks:

    def test_auto_approve_when_no_extraction_data(self):
        """With no structured data, judge approves by default."""
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        result = run_policy_checks(judge_id)
        assert result.approved is True
        assert "no_extraction_data" in result.reason.lower() or "no structured" in result.reason.lower()

    def test_approve_when_all_checks_pass(self):
        """Judge approves when all policy checks pass."""
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        # Add policy for email
        policy_store.add_to_allowlist("email", "safe@example.com")

        # Set policy checks on target
        checks = [
            {"field": "recipient", "policy_type": "email", "value": "safe@example.com"},
        ]
        db = get_db()
        try:
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                (target_id, "_judge_policy_checks", json.dumps(checks)),
            )
            db.commit()
        finally:
            db.close()

        result = run_policy_checks(judge_id)
        assert result.approved is True
        assert len(result.checks) == 1
        assert result.checks[0].passed is True

    def test_reject_when_check_fails(self):
        """Judge rejects when a policy check fails."""
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        # Don't add any policies — default deny
        checks = [
            {"field": "target_email", "policy_type": "email", "value": "evil@hacker.com"},
        ]
        db = get_db()
        try:
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                (target_id, "_judge_policy_checks", json.dumps(checks)),
            )
            db.commit()
        finally:
            db.close()

        result = run_policy_checks(judge_id)
        assert result.approved is False
        assert len(result.failed_checks) == 1
        assert result.failed_checks[0].field_name == "target_email"

    def test_mixed_pass_and_fail(self):
        """Judge rejects if any check fails, even if others pass."""
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        policy_store.add_to_allowlist("email", "good@example.com")
        # "bad@evil.com" is NOT in allowlist

        checks = [
            {"field": "to", "policy_type": "email", "value": "good@example.com"},
            {"field": "cc", "policy_type": "email", "value": "bad@evil.com"},
        ]
        db = get_db()
        try:
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                (target_id, "_judge_policy_checks", json.dumps(checks)),
            )
            db.commit()
        finally:
            db.close()

        result = run_policy_checks(judge_id)
        assert result.approved is False
        assert len(result.checks) == 2
        assert len(result.failed_checks) == 1

    def test_fields_without_policy_type_pass(self):
        """Fields without policy_type constraint are auto-approved."""
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        checks = [
            {"field": "summary", "value": "Any text here"},  # no policy_type
        ]
        db = get_db()
        try:
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                (target_id, "_judge_policy_checks", json.dumps(checks)),
            )
            db.commit()
        finally:
            db.close()

        result = run_policy_checks(judge_id)
        assert result.approved is True

    def test_nonexistent_judge_arc(self):
        result = run_policy_checks(99999)
        assert result.approved is False
        assert "not found" in result.reason

    def test_domain_policy_check(self):
        """Test domain policy validation through judge."""
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        policy_store.add_to_allowlist("domain", "api.example.com")

        checks = [
            {"field": "api_host", "policy_type": "domain", "value": "api.example.com"},
        ]
        db = get_db()
        try:
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                (target_id, "_judge_policy_checks", json.dumps(checks)),
            )
            db.commit()
        finally:
            db.close()

        result = run_policy_checks(judge_id)
        assert result.approved is True

    def test_int_range_policy_check(self):
        """Test int_range policy validation through judge."""
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        policy_store.add_to_allowlist("int_range", "80:443")

        checks = [
            {"field": "port", "policy_type": "int_range", "value": 443},
        ]
        db = get_db()
        try:
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                (target_id, "_judge_policy_checks", json.dumps(checks)),
            )
            db.commit()
        finally:
            db.close()

        result = run_policy_checks(judge_id)
        assert result.approved is True


class TestJudgeResultDataclass:

    def test_failed_checks_property(self):
        result = JudgeResult(
            approved=False,
            checks=[
                PolicyCheck("a", "email", "x", True),
                PolicyCheck("b", "email", "y", False, "denied"),
            ],
        )
        assert len(result.failed_checks) == 1
        assert result.failed_checks[0].field_name == "b"
