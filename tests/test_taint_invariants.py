"""Targeted tests for trust invariants I1-I9.

Each test maps to a specific invariant documented in docs/trust-invariants.md.
These tests verify the exact security boundary — not general functionality.
"""

import json
from unittest.mock import patch

import pytest

from carpenter.agent import invocation, conversation
from carpenter.chat_tool_loader import get_handler
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.workflows import review_manager
from carpenter.review.code_reviewer import ReviewResult
from carpenter.tool_backends import arc as arc_backend
from carpenter.db import get_db


# ---------------------------------------------------------------------------
# I1 — No CHAT/PLANNER context contains raw untrusted tool output
# ---------------------------------------------------------------------------

class TestI1:
    """submit_code and get_execution_output must withhold tainted output."""

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_submit_code_withholds_tainted_output(self, mock_review):
        """I1: submit_code with web import is BLOCKED from chat context."""
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="",
        )
        conv_id = conversation.create_conversation()
        code = (
            'from carpenter_tools.act.web import get\n'
            'result = get("http://example.com")\n'
            'print("INJECTED_PROMPT_CONTENT")\n'
        )
        result = invocation._execute_chat_tool(
            "submit_code",
            {"code": code, "description": "fetch"},
            conversation_id=conv_id,
        )
        assert "INJECTED_PROMPT_CONTENT" not in result
        # Result should be a BLOCKED message
        assert "BLOCKED" in result

    @patch("carpenter.review.pipeline.review_code_for_intent")
    def test_get_execution_output_withholds_tainted_log(self, mock_review):
        """I1: get_execution_output refuses to return tainted execution log."""
        mock_review.return_value = ReviewResult(
            status="approve", reason="", sanitized_code="",
        )
        from carpenter.core import code_manager
        code = (
            'from carpenter_tools.act.web import get\n'
            'print("LEAKED_SECRETS")\n'
        )
        save = code_manager.save_code(code, source="test", name="tainted")
        exec_result = code_manager.execute(save["code_file_id"])

        result = get_handler("get_execution_output")(
            {"execution_id": exec_result["execution_id"]}
        )
        assert "LEAKED_SECRETS" not in result
        assert "withheld" in result.lower()


# ---------------------------------------------------------------------------
# I2 — Trusted arcs cannot access untrusted data tools
# ---------------------------------------------------------------------------

class TestI2:
    """Trusted arcs are blocked from UNTRUSTED data tools."""

    def test_trusted_arc_blocked_from_read_output_untrusted(self):
        """I2: trusted arc cannot call arc.read_output_UNTRUSTED — dispatch raises DispatchError."""
        from carpenter.executor.dispatch_bridge import validate_and_dispatch, DispatchError

        # Create a trusted arc (the default integrity_level)
        arc_id = arc_manager.create_arc("trusted-caller", integrity_level="trusted")

        # Attempt to call arc.read_output_UNTRUSTED as the trusted arc
        with pytest.raises(DispatchError, match="(?i)trusted|untrusted"):
            validate_and_dispatch(
                "arc.read_output_UNTRUSTED",
                {"_caller_arc_id": arc_id, "arc_id": arc_id},
            )

        # Verify the denial was recorded in the trust audit log
        from carpenter.core.trust.audit import get_trust_events
        events = get_trust_events(arc_id=arc_id, event_type="access_denied")
        assert len(events) >= 1
        assert events[0]["details"]["tool"] == "arc.read_output_UNTRUSTED"


# ---------------------------------------------------------------------------
# I3 — Only path from untrusted->trusted is review arc + judge approval
# ---------------------------------------------------------------------------

class TestI3:
    """Only a JUDGE arc's approval triggers trust promotion."""

    def test_reviewer_approve_does_not_promote(self):
        """I3: REVIEWER approve is advisory — target stays untrusted."""
        parent = arc_manager.create_arc("project")
        batch = arc_backend.handle_create_batch({
            "arcs": [
                {"name": "target", "parent_id": parent, "integrity_level": "untrusted"},
                {"name": "reviewer", "parent_id": parent, "agent_type": "REVIEWER",
                 "reviewer_profile": "security-reviewer"},
                {"name": "judge", "parent_id": parent, "agent_type": "JUDGE",
                 "reviewer_profile": "judge"},
            ]
        })
        target, reviewer, judge = batch["arc_ids"]

        result = review_manager.submit_verdict(reviewer, target, "approve", "ok")
        assert result["promoted"] is False
        assert arc_manager.get_arc(target)["integrity_level"] == "untrusted"

    def test_judge_approve_promotes_target(self):
        """I3: JUDGE approve promotes target to trusted."""
        parent = arc_manager.create_arc("project")
        batch = arc_backend.handle_create_batch({
            "arcs": [
                {"name": "target", "parent_id": parent, "integrity_level": "untrusted"},
                {"name": "judge", "parent_id": parent, "agent_type": "JUDGE",
                 "reviewer_profile": "judge"},
            ]
        })
        target, judge = batch["arc_ids"]

        result = review_manager.submit_verdict(judge, target, "approve", "safe")
        assert result["promoted"] is True
        assert arc_manager.get_arc(target)["integrity_level"] == "trusted"


# ---------------------------------------------------------------------------
# I4 — Untrusted arcs only created in batches with reviewers
# ---------------------------------------------------------------------------

class TestI4:
    """Individual untrusted arc creation must be rejected."""

    def test_individual_untrusted_arc_rejected(self):
        """I4: arc.create() with integrity_level='untrusted' raises ValueError."""
        with pytest.raises(ValueError, match="Cannot create individual untrusted arc"):
            arc_manager.create_arc("tainted", integrity_level="untrusted")

    def test_create_arc_does_not_accept_allow_tainted_kwarg(self):
        """I4: the legacy ``_allow_tainted`` bypass kwarg has been removed.

        Internal batch-builders go through ``_insert_arc`` directly; no
        public caller should be able to bypass the guard.
        """
        with pytest.raises(TypeError, match="_allow_tainted"):
            arc_manager.create_arc(
                "tainted",
                integrity_level="untrusted",
                _allow_tainted=True,  # type: ignore[call-arg]
            )

    def test_batch_without_reviewer_rejected(self):
        """I4: create_batch with untrusted arc but no reviewers is rejected."""
        result = arc_backend.handle_create_batch({
            "arcs": [{"name": "solo-untrusted", "integrity_level": "untrusted"}]
        })
        assert "error" in result
        assert "REVIEWER or JUDGE" in result["error"]


# ---------------------------------------------------------------------------
# I5 — Parent arcs stay trusted when orchestrating untrusted children
# ---------------------------------------------------------------------------

class TestI5:
    """Parents remain trusted — I2 (HTTP 403) is the real enforcement."""

    def test_parent_stays_trusted_after_untrusted_child_batch(self):
        """I5: trusted parent stays trusted when untrusted child batch is created."""
        parent = arc_manager.create_arc("trusted-parent")
        assert arc_manager.get_arc(parent)["integrity_level"] == "trusted"

        arc_backend.handle_create_batch({
            "arcs": [
                {"name": "untrusted-child", "parent_id": parent, "integrity_level": "untrusted"},
                {"name": "judge", "parent_id": parent, "agent_type": "JUDGE",
                 "reviewer_profile": "judge"},
            ]
        })

        assert arc_manager.get_arc(parent)["integrity_level"] == "trusted"


# ---------------------------------------------------------------------------
# I6 — Judge approval promotes only the target arc
# ---------------------------------------------------------------------------

class TestI6:
    """Promotion is scoped to the target; parent was never untrusted."""

    def test_parent_stays_trusted_after_child_promotion(self):
        """I6: parent stays trusted; judge promotes only the child."""
        parent = arc_manager.create_arc("parent")
        batch = arc_backend.handle_create_batch({
            "arcs": [
                {"name": "child", "parent_id": parent, "integrity_level": "untrusted"},
                {"name": "judge", "parent_id": parent, "agent_type": "JUDGE",
                 "reviewer_profile": "judge"},
            ]
        })
        child, judge = batch["arc_ids"]

        # Parent stays trusted (no upward propagation)
        assert arc_manager.get_arc(parent)["integrity_level"] == "trusted"

        # Judge promotes child
        review_manager.submit_verdict(judge, child, "approve", "ok")

        # Child promoted, parent still trusted
        assert arc_manager.get_arc(child)["integrity_level"] == "trusted"
        assert arc_manager.get_arc(parent)["integrity_level"] == "trusted"


# ---------------------------------------------------------------------------
# I8 — CONSTRAINED data cannot influence control flow without deterministic check
# ---------------------------------------------------------------------------

class TestI8:
    """Deterministic JUDGE validates constrained data against policies."""

    def test_judge_rejects_when_policy_denies(self):
        """I8: JUDGE deterministic check rejects values not in allowlist."""
        from carpenter.security.judge import run_policy_checks, _get_review_target

        parent = arc_manager.create_arc("project")
        batch = arc_backend.handle_create_batch({
            "arcs": [
                {"name": "target", "parent_id": parent, "integrity_level": "untrusted"},
                {"name": "reviewer", "parent_id": parent, "agent_type": "REVIEWER",
                 "reviewer_profile": "security-reviewer"},
                {"name": "judge", "parent_id": parent, "agent_type": "JUDGE",
                 "reviewer_profile": "judge"},
            ]
        })
        target, reviewer, judge = batch["arc_ids"]

        # Set extraction data on target — email NOT in any allowlist (default-deny)
        checks = [
            {"field": "recipient", "policy_type": "email", "value": "attacker@evil.com"},
        ]
        db = get_db()
        try:
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                (target, "_judge_policy_checks", json.dumps(checks)),
            )
            db.commit()
        finally:
            db.close()

        result = run_policy_checks(judge)
        assert result.approved is False, "Default-deny must reject unknown email"
        assert len(result.failed_checks) == 1

    def test_judge_approves_when_policy_allows(self):
        """I8: JUDGE deterministic check approves values in allowlist."""
        from carpenter.security.judge import run_policy_checks
        from carpenter.security import policy_store

        parent = arc_manager.create_arc("project")
        batch = arc_backend.handle_create_batch({
            "arcs": [
                {"name": "target", "parent_id": parent, "integrity_level": "untrusted"},
                {"name": "reviewer", "parent_id": parent, "agent_type": "REVIEWER",
                 "reviewer_profile": "security-reviewer"},
                {"name": "judge", "parent_id": parent, "agent_type": "JUDGE",
                 "reviewer_profile": "judge"},
            ]
        })
        target, reviewer, judge = batch["arc_ids"]

        # Add email to allowlist, then set extraction data
        policy_store.add_to_allowlist("email", "trusted@example.com")
        checks = [
            {"field": "recipient", "policy_type": "email", "value": "trusted@example.com"},
        ]
        db = get_db()
        try:
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                (target, "_judge_policy_checks", json.dumps(checks)),
            )
            db.commit()
        finally:
            db.close()

        result = run_policy_checks(judge)
        assert result.approved is True, "Allowlisted email must be approved"

    def test_judge_runs_platform_code_not_llm(self):
        """I8: JUDGE arcs are intercepted at dispatch — no LLM agent invoked."""
        # Verify JUDGE agent_type has empty allowed_tools (no tool use)
        from carpenter.core.trust.types import AgentType, _DEFAULT_AGENT_CAPABILITIES
        caps = _DEFAULT_AGENT_CAPABILITIES[AgentType.JUDGE]
        assert len(caps["allowed_tools"]) == 0, "JUDGE should have no tools (platform code)"


# ---------------------------------------------------------------------------
# I9 — Policy-typed literals must validate against security policies
# ---------------------------------------------------------------------------

class TestI9:
    """Policy-typed literals validate against platform policies."""

    def test_default_deny_all_policy_types(self):
        """I9: Empty allowlists reject all values (default-deny)."""
        from carpenter.security.policies import get_policies
        from carpenter.security.exceptions import PolicyValidationError

        policies = get_policies()
        # Email not in allowlist
        with pytest.raises(PolicyValidationError):
            policies.validate("email", "anyone@anywhere.com")
        # Domain not in allowlist
        with pytest.raises(PolicyValidationError):
            policies.validate("domain", "example.com")
        # Command not in allowlist
        with pytest.raises(PolicyValidationError):
            policies.validate("command", "rm -rf /")

    def test_policy_typed_literal_equality(self):
        """I9: Policy-typed literals compare against raw values."""
        from carpenter_tools.policy import EmailPolicy, Domain, IntRange

        # Email comparison is case-insensitive
        e = EmailPolicy("User@Example.COM")
        assert e == "user@example.com"

        # Domain matches subdomains
        d = Domain("example.com")
        assert d == "sub.example.com"
        assert d != "evil.com"

        # IntRange contains check
        r = IntRange(80, 443)
        assert 200 in r
        assert 8080 not in r

    def test_policy_validate_endpoint(self):
        """I9: Platform-side policy.validate handler checks allowlists."""
        from carpenter.tool_backends.policy import handle_validate
        from carpenter.security import policy_store

        # Default-deny: should reject
        result = handle_validate({"policy_type": "email", "value": "test@test.com"})
        assert result["allowed"] is False

        # Add to allowlist: should approve
        policy_store.add_to_allowlist("email", "test@test.com")
        result = handle_validate({"policy_type": "email", "value": "test@test.com"})
        assert result["allowed"] is True
