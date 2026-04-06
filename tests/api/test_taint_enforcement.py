"""Tests for trust boundary enforcement via the dispatch bridge.

These tests verify that the security checks in the dispatch bridge
correctly enforce trust boundaries, agent type restrictions, and
taint propagation.
"""

import json

import pytest

from carpenter.api.callbacks import _DEFAULT_UNTRUSTED_DATA_TOOLS  # noqa: F401
from carpenter.executor.dispatch_bridge import validate_and_dispatch, DispatchError
from carpenter.db import get_db
from carpenter.core.arcs import manager as arc_manager

from tests.api.conftest import _create_reviewed_session


# -- Trusted arc cannot access untrusted data tools --

class TestCleanArcBlocked:
    """Trusted arcs must be blocked from UNTRUSTED data tools."""

    def test_clean_arc_read_output_untrusted_blocked(self):
        arc_id = arc_manager.create_arc("clean-arc", integrity_level="trusted")
        with pytest.raises(DispatchError, match="untrusted"):
            validate_and_dispatch(
                "arc.read_output_UNTRUSTED",
                {"_caller_arc_id": arc_id, "arc_id": arc_id},
            )

    def test_clean_arc_read_state_untrusted_blocked(self):
        arc_id = arc_manager.create_arc("clean-arc", integrity_level="trusted")
        with pytest.raises(DispatchError):
            validate_and_dispatch(
                "arc.read_state_UNTRUSTED",
                {"_caller_arc_id": arc_id, "arc_id": arc_id, "key": "test"},
            )


# -- Untrusted arc can access untrusted data --

class TestTaintedArcAllowed:
    """Untrusted arcs should be able to access UNTRUSTED data tools."""

    def test_tainted_arc_read_output_untrusted_allowed(self):
        parent = arc_manager.create_arc("parent")
        target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
        result = validate_and_dispatch(
            "arc.read_output_UNTRUSTED",
            {"_caller_arc_id": target, "arc_id": target},
        )
        assert isinstance(result, dict)

    def test_tainted_arc_read_state_untrusted_allowed(self):
        parent = arc_manager.create_arc("parent")
        arc_id = arc_manager.add_child(parent, "tainted-arc", integrity_level="untrusted")
        result = validate_and_dispatch(
            "arc.read_state_UNTRUSTED",
            {"_caller_arc_id": arc_id, "arc_id": arc_id, "key": "test"},
        )
        assert isinstance(result, dict)


# -- Review arc can access untrusted data --

class TestReviewArcAllowed:
    """Review arcs should be able to access UNTRUSTED data tools."""

    def test_review_arc_read_output_untrusted_allowed(self):
        arc_id = arc_manager.create_arc("review-arc", integrity_level="trusted",
                                         agent_type="REVIEWER")
        result = validate_and_dispatch(
            "arc.read_output_UNTRUSTED",
            {"_caller_arc_id": arc_id, "arc_id": arc_id},
        )
        assert isinstance(result, dict)


# -- Planner restrictions --

class TestPlannerRestrictions:
    """Planner agents should be restricted to their tool whitelist."""

    def test_planner_arc_get_plan_allowed(self):
        arc_id = arc_manager.create_arc("planner-arc", agent_type="PLANNER")
        result = validate_and_dispatch(
            "arc.get_plan",
            {"_caller_arc_id": arc_id, "arc_id": arc_id},
        )
        assert isinstance(result, dict)

    def test_planner_cannot_use_web_get(self):
        arc_id = arc_manager.create_arc("planner-arc", agent_type="PLANNER")
        session_id = _create_reviewed_session("planner-web-session")
        with pytest.raises(DispatchError, match="(?i)planner"):
            validate_and_dispatch(
                "web.get",
                {"_caller_arc_id": arc_id, "arc_id": arc_id, "url": "http://example.com"},
                session_id=session_id,
            )

    def test_planner_cannot_set_state(self):
        arc_id = arc_manager.create_arc("planner-arc", agent_type="PLANNER")
        session_id = _create_reviewed_session("planner-state-session")
        with pytest.raises(DispatchError):
            validate_and_dispatch(
                "state.set",
                {"_caller_arc_id": arc_id, "arc_id": arc_id, "key": "x", "value": "y"},
                session_id=session_id,
            )

    def test_planner_can_create_arc(self):
        planner = arc_manager.create_arc("planner", agent_type="PLANNER")
        session_id = _create_reviewed_session("planner-create-session")
        result = validate_and_dispatch(
            "arc.create",
            {"_caller_arc_id": planner, "arc_id": planner, "name": "new-child"},
            session_id=session_id,
        )
        assert "arc_id" in result


# -- Reviewer restrictions --

class TestReviewerRestrictions:
    """Reviewer agents should be restricted to their tool whitelist."""

    def test_reviewer_cannot_use_web_get(self):
        arc_id = arc_manager.create_arc("reviewer-arc", agent_type="REVIEWER",
                                         integrity_level="trusted")
        session_id = _create_reviewed_session("reviewer-web-session")
        with pytest.raises(DispatchError, match="(?i)reviewer"):
            validate_and_dispatch(
                "web.get",
                {"_caller_arc_id": arc_id, "arc_id": arc_id, "url": "http://example.com"},
                session_id=session_id,
            )

    def test_reviewer_can_read_files(self):
        arc_id = arc_manager.create_arc("reviewer-arc", agent_type="REVIEWER",
                                         integrity_level="trusted")
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("test")
            path = f.name
        try:
            result = validate_and_dispatch(
                "files.read",
                {"_caller_arc_id": arc_id, "arc_id": arc_id, "path": path},
            )
            assert result["content"] == "test"
        finally:
            os.unlink(path)


# -- Legacy arcs --

class TestLegacyArcsBehavior:
    """Arcs without explicit integrity_level should default to trusted behavior."""

    def test_legacy_arc_defaults_to_trusted(self):
        arc_id = arc_manager.create_arc("legacy-arc")
        arc = arc_manager.get_arc(arc_id)
        assert arc["integrity_level"] == "trusted"
        assert arc["agent_type"] == "EXECUTOR"

    def test_legacy_arc_blocked_from_untrusted(self):
        arc_id = arc_manager.create_arc("legacy-arc")
        with pytest.raises(DispatchError):
            validate_and_dispatch(
                "arc.read_output_UNTRUSTED",
                {"_caller_arc_id": arc_id, "arc_id": arc_id},
            )


# -- Trust audit log entries --

class TestTrustAuditLog:
    """Verify that access_denied events are logged to trust_audit_log."""

    def test_access_denied_logged(self):
        arc_id = arc_manager.create_arc("audit-test", integrity_level="trusted")
        with pytest.raises(DispatchError):
            validate_and_dispatch(
                "arc.read_output_UNTRUSTED",
                {"_caller_arc_id": arc_id, "arc_id": arc_id},
            )
        from carpenter.core.trust.audit import get_trust_events
        events = get_trust_events(arc_id=arc_id, event_type="access_denied")
        assert len(events) >= 1
        assert events[0]["details"]["tool"] == "arc.read_output_UNTRUSTED"

    def test_planner_denied_logged(self):
        arc_id = arc_manager.create_arc("planner-audit", agent_type="PLANNER")
        session_id = _create_reviewed_session("planner-audit-session")
        with pytest.raises(DispatchError):
            validate_and_dispatch(
                "web.get",
                {"_caller_arc_id": arc_id, "arc_id": arc_id, "url": "http://example.com"},
                session_id=session_id,
            )
        from carpenter.core.trust.audit import get_trust_events
        events = get_trust_events(arc_id=arc_id, event_type="access_denied")
        assert len(events) >= 1


# -- get_plan returns structural fields only --

class TestGetPlanHandler:
    """handle_get_plan should return only structural fields."""

    def test_get_plan_returns_structural_fields(self):
        parent = arc_manager.create_arc("parent")
        arc_id = arc_manager.add_child(parent, "plan-test", goal="test goal",
                                         integrity_level="untrusted", output_type="json")
        result = validate_and_dispatch(
            "arc.get_plan",
            {"arc_id": arc_id},
        )
        arc = result["arc"]
        assert arc["name"] == "plan-test"
        assert arc["integrity_level"] == "untrusted"
        assert arc["output_type"] == "json"
        assert "code_file_id" not in arc
        assert "disk_workspace" not in arc

    def test_get_children_plan_returns_list(self):
        parent = arc_manager.create_arc("parent")
        arc_manager.add_child(parent, "child-1", integrity_level="untrusted")
        arc_manager.add_child(parent, "child-2")
        result = validate_and_dispatch(
            "arc.get_children_plan",
            {"arc_id": parent},
        )
        children = result["children"]
        assert len(children) == 2
        assert children[0]["name"] == "child-1"
        assert children[0]["integrity_level"] == "untrusted"


# -- read_output_UNTRUSTED returns full data --

class TestReadOutputUntrusted:
    """handle_read_output_UNTRUSTED should return full arc data."""

    def test_returns_arc_history_state(self):
        parent = arc_manager.create_arc("parent")
        arc_id = arc_manager.add_child(parent, "full-data", integrity_level="untrusted")
        # Add some state
        db = get_db()
        try:
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                (arc_id, "test_key", json.dumps("test_value")),
            )
            db.commit()
        finally:
            db.close()

        # Use untrusted caller to read
        result = validate_and_dispatch(
            "arc.read_output_UNTRUSTED",
            {"_caller_arc_id": arc_id, "arc_id": arc_id},
        )
        assert "arc" in result
        assert "history" in result
        assert "state" in result
        assert result["state"]["test_key"] == "test_value"
