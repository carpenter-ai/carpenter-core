"""Tests for dispatch_bridge.py security fixes from PR #124 follow-up.

Tests capability-granted tool bypass (Fix #1) and SCOPE_BYPASS_CAPABILITIES
check for cross-arc state reads (Fix #2).
"""

import pytest

from carpenter.executor.dispatch_bridge import validate_and_dispatch, DispatchError
from carpenter.core.trust.types import AgentType
from carpenter.db import get_db


@pytest.fixture
def db_with_arcs():
    """Create test arcs in the database."""
    from datetime import datetime, timezone, timedelta

    db = get_db()
    try:
        # Create parent arc (PLANNER)
        db.execute(
            """
            INSERT INTO arcs (id, name, goal, status, agent_type, integrity_level, parent_id)
            VALUES (1, 'test-planner', 'test goal', 'active', 'PLANNER', 'trusted', NULL)
            """
        )
        # Create child arc (EXECUTOR)
        db.execute(
            """
            INSERT INTO arcs (id, name, goal, status, agent_type, integrity_level, parent_id)
            VALUES (2, 'test-executor-child', 'test goal', 'active', 'EXECUTOR', 'trusted', 1)
            """
        )
        # Create unrelated arc
        db.execute(
            """
            INSERT INTO arcs (id, name, goal, status, agent_type, integrity_level, parent_id)
            VALUES (3, 'test-executor-other', 'test goal', 'active', 'EXECUTOR', 'trusted', NULL)
            """
        )
        # Create a valid execution session
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        db.execute(
            """
            INSERT INTO execution_sessions (session_id, reviewed, expires_at, execution_context)
            VALUES ('test-session', 1, ?, 'reviewed')
            """,
            (expires_at,),
        )
        db.commit()
        yield db
    finally:
        db.execute("DELETE FROM arcs WHERE id IN (1, 2, 3)")
        db.execute("DELETE FROM execution_sessions WHERE session_id = 'test-session'")
        db.commit()
        db.close()


class TestCapabilityGrantedToolBypass:
    """Test that capability-granted tools work through dispatch bridge (Fix #1)."""

    def test_planner_blocked_from_state_set_without_capability(self, db_with_arcs):
        """PLANNER agents cannot normally use state.set."""
        # PLANNER allowed_tools does not include state.set
        with pytest.raises(DispatchError, match="PLANNER agents cannot use state.set"):
            validate_and_dispatch(
                "state.set",
                {"key": "test", "value": "data"},
                arc_id=1,
                session_id="test-session",
            )

    def test_planner_allowed_with_capability_grant(self, db_with_arcs, monkeypatch):
        """PLANNER with capability grant can use restricted tools."""
        # Mock get_arc_capabilities to return a grant
        def mock_get_arc_capabilities(arc_id):
            if arc_id == 1:
                return {"storage.write"}
            return set()

        # Mock resolve_capability_tools to map storage.write -> state.set
        def mock_resolve_capability_tools(caps):
            if "storage.write" in caps:
                return {"state.set", "state.delete"}
            return set()

        # Patch at the dispatch_bridge module level
        monkeypatch.setattr(
            "carpenter.executor.dispatch_bridge.get_arc_capabilities",
            mock_get_arc_capabilities
        )
        monkeypatch.setattr(
            "carpenter.executor.dispatch_bridge.resolve_capability_tools",
            mock_resolve_capability_tools
        )

        # Should succeed with capability grant
        try:
            result = validate_and_dispatch(
                "state.set",
                {"key": "test", "value": "data"},
                arc_id=1,
                session_id="test-session",
            )
            # If the handler runs without raising DispatchError, the check passed
            # (the actual handler may fail for other reasons, but we're testing the bypass)
        except DispatchError as e:
            if "PLANNER agents cannot use" in str(e):
                pytest.fail("Capability grant did not bypass agent-type restriction")
            # Other errors (like missing session) are acceptable for this test


class TestScopeBypassCapabilities:
    """Test SCOPE_BYPASS_CAPABILITIES for cross-arc state reads (Fix #2)."""

    def test_cross_arc_read_blocked_without_capability(self, db_with_arcs):
        """Cross-arc state read fails without descendant relationship or grant."""
        # Arc 1 trying to read from arc 3 (not a descendant)
        with pytest.raises(DispatchError, match="not a descendant"):
            validate_and_dispatch(
                "state.get",
                {"key": "test", "_target_arc_id": 3},
                arc_id=1,
                session_id="test-session",
            )

    def test_cross_arc_read_allowed_with_scope_bypass(self, db_with_arcs, monkeypatch):
        """Cross-arc read succeeds with scope-bypass capability (e.g., system.read)."""
        # Mock get_arc_capabilities to return system.read
        def mock_get_arc_capabilities(arc_id):
            if arc_id == 1:
                # system.read is in SCOPE_BYPASS_CAPABILITIES
                return {"system.read"}
            return set()

        # Patch at the dispatch_bridge module level
        monkeypatch.setattr(
            "carpenter.executor.dispatch_bridge.get_arc_capabilities",
            mock_get_arc_capabilities
        )

        # Should bypass the descendant/grant check
        try:
            result = validate_and_dispatch(
                "state.get",
                {"key": "test", "_target_arc_id": 3},
                arc_id=1,
                session_id="test-session",
            )
            # If we get past the scope check without "not a descendant" error, the fix works
        except DispatchError as e:
            if "not a descendant" in str(e):
                pytest.fail("SCOPE_BYPASS_CAPABILITIES did not bypass descendant check")
            # Other errors are acceptable for this test

    def test_descendant_read_still_works(self, db_with_arcs):
        """Normal parent->child read still works without special capabilities."""
        # Arc 1 reading from arc 2 (its child)
        try:
            result = validate_and_dispatch(
                "state.get",
                {"key": "test", "_target_arc_id": 2},
                arc_id=1,
                session_id="test-session",
            )
            # Should pass the descendant check (may fail later for other reasons)
        except DispatchError as e:
            if "not a descendant" in str(e):
                pytest.fail("Descendant check incorrectly rejected parent->child read")
