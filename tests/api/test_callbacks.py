"""Tests for the tool dispatch table and validation logic.

These tests verify the security logic in carpenter.api.callbacks (dispatch
table, tool classification, session validation, trust enforcement) which is
now exercised through the dispatch bridge rather than an HTTP endpoint.
"""
import pytest
from datetime import datetime, timezone, timedelta

from carpenter.api.callbacks import (
    _DEFAULT_SESSION_EXEMPT_TOOLS, get_session_exempt_tools,
    validate_tool_classification, _get_caller_context,
    _DISPATCH, get_messaging_tools,
)
from carpenter.executor.dispatch_bridge import (
    validate_and_dispatch, DispatchError,
)
from carpenter.db import get_db

from tests.api.conftest import _create_reviewed_session


def _create_arc(arc_id=1):
    db = get_db()
    try:
        db.execute("INSERT INTO arcs (id, name) VALUES (?, ?)", (arc_id, "test-arc"))
        db.commit()
    finally:
        db.close()
    return arc_id


def test_state_roundtrip():
    _create_arc()
    session_id = _create_reviewed_session("test-roundtrip")

    # Set (action tool -- requires reviewed session)
    result = validate_and_dispatch(
        "state.set",
        {"arc_id": 1, "key": "foo", "value": "bar"},
        session_id=session_id,
    )
    assert result["success"] is True

    # Get (read tool -- no session needed)
    result = validate_and_dispatch(
        "state.get",
        {"arc_id": 1, "key": "foo"},
    )
    assert result["value"] == "bar"


def test_unknown_tool():
    with pytest.raises(DispatchError, match="Unknown tool"):
        validate_and_dispatch("nonexistent.tool", {})


# --- Execution session enforcement tests ---


class TestExecutionContextEnforcement:
    """Verify that action tools require valid reviewed execution session."""

    # Action tools and their params for parametrized tests.
    _ACTION_TOOLS = [
        ("state.set", {"arc_id": 1, "key": "x", "value": "y"}),
        ("state.delete", {"arc_id": 1, "key": "x"}),
        ("files.write", {"path": "/tmp/test_blocked.txt", "content": "x"}),
        ("arc.create", {"name": "blocked-arc"}),
    ]

    @pytest.mark.parametrize("tool_name,params", _ACTION_TOOLS,
                             ids=[t[0] for t in _ACTION_TOOLS])
    def test_action_tool_without_context_raises(self, tool_name, params):
        """Action tools without reviewed session are rejected."""
        _create_arc()
        with pytest.raises(DispatchError):
            validate_and_dispatch(tool_name, params)

    @pytest.mark.parametrize("tool_name,params", _ACTION_TOOLS,
                             ids=[t[0] for t in _ACTION_TOOLS])
    def test_action_tool_with_reviewed_context_succeeds(self, tool_name, params):
        """Action tools with reviewed session execute normally."""
        _create_arc()
        session_id = _create_reviewed_session(f"reviewed-{tool_name}")
        result = validate_and_dispatch(tool_name, params, session_id=session_id)
        assert isinstance(result, dict)

    def test_action_tool_with_wrong_context_raises(self):
        """Action tools with invalid session ID are rejected."""
        _create_arc()
        with pytest.raises(DispatchError):
            validate_and_dispatch(
                "state.set",
                {"arc_id": 1, "key": "test", "value": "blocked"},
                session_id="invalid-session",
            )

    def test_read_tool_without_context_succeeds(self):
        """Read-only tools work without reviewed session."""
        _create_arc()
        result = validate_and_dispatch(
            "state.get",
            {"arc_id": 1, "key": "nonexistent"},
        )
        assert "value" in result

    def test_read_tool_files_read_no_context(self):
        """files.read works without reviewed session."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("test content")
            path = f.name
        try:
            result = validate_and_dispatch(
                "files.read",
                {"path": path},
            )
            assert result["content"] == "test content"
        finally:
            os.unlink(path)


# --- _caller_arc_id injection tests ---


class TestCallerArcIdInjection:
    """Verify _caller_arc_id is used for trust context instead of params arc_id."""

    def test_get_caller_context_prefers_caller_arc_id(self):
        """_get_caller_context should use _caller_arc_id over arc_id."""
        from carpenter.core.arcs import manager as arc_manager

        # Create two arcs: an untrusted caller and a trusted target
        parent = arc_manager.create_arc("parent")
        caller_id = arc_manager.add_child(parent, "caller", integrity_level="untrusted")
        target_id = arc_manager.create_arc("target", integrity_level="trusted")

        # Simulate cross-arc read: _caller_arc_id=caller, arc_id=target
        params = {"_caller_arc_id": caller_id, "arc_id": target_id}
        ctx = _get_caller_context(params)

        assert ctx is not None
        assert ctx["integrity_level"] == "untrusted"  # Caller's integrity, not target's

    def test_get_caller_context_returns_none_without_caller_arc_id(self):
        """Without _caller_arc_id, returns None (arc_id is the target, not caller)."""
        from carpenter.core.arcs import manager as arc_manager

        parent = arc_manager.create_arc("parent")
        arc_id = arc_manager.add_child(parent, "some-arc", integrity_level="untrusted")
        params = {"arc_id": arc_id}
        ctx = _get_caller_context(params)

        # No _caller_arc_id means we don't know who the caller is;
        # arc_id is the target being acted upon, not the caller.
        assert ctx is None

    def test_cross_arc_untrusted_read_uses_caller_taint(self):
        """Untrusted caller reading trusted target via arc.read_output_UNTRUSTED succeeds."""
        from carpenter.core.arcs import manager as arc_manager

        # Untrusted caller (allowed to use UNTRUSTED tools)
        parent = arc_manager.create_arc("parent")
        caller_id = arc_manager.add_child(parent, "caller", integrity_level="untrusted")
        # Trusted target arc
        target_id = arc_manager.create_arc("target", integrity_level="trusted")

        # This is a session-exempt tool, so no session needed
        result = validate_and_dispatch(
            "arc.read_output_UNTRUSTED",
            {"_caller_arc_id": caller_id, "arc_id": target_id},
        )
        # Untrusted caller is allowed to access UNTRUSTED data tools
        assert isinstance(result, dict)

    def test_clean_caller_blocked_from_untrusted_tools(self):
        """Trusted caller cannot use UNTRUSTED tools even with untrusted target."""
        from carpenter.core.arcs import manager as arc_manager

        caller_id = arc_manager.create_arc("caller", integrity_level="trusted")
        parent = arc_manager.create_arc("parent")
        target_id = arc_manager.add_child(parent, "target", integrity_level="untrusted")

        with pytest.raises(DispatchError, match="untrusted"):
            validate_and_dispatch(
                "arc.read_output_UNTRUSTED",
                {"_caller_arc_id": caller_id, "arc_id": target_id},
            )


# --- Tainted conversation forces integrity on arc creation ---


class TestTaintedConversationForcesArcTaint:
    """Verify that arc.create / arc.add_child from a tainted conversation are rejected.

    Earlier behaviour silently promoted ``integrity_level`` to ``untrusted``
    and bypassed the individual-untrusted guard, producing orphan tainted
    arcs with no reviewer/judge chain. The new contract is to reject the
    call and force the caller onto ``arc.create_batch``.
    """

    def _create_tainted_session(self, session_id="tainted-session"):
        """Create a reviewed session linked to a tainted conversation."""
        db = get_db()
        try:
            # Create a conversation
            cursor = db.execute(
                "INSERT INTO conversations (started_at) VALUES (datetime('now'))"
            )
            conv_id = cursor.lastrowid

            # Taint the conversation
            db.execute(
                "INSERT INTO conversation_taint (conversation_id, source_tool) "
                "VALUES (?, ?)",
                (conv_id, "carpenter_tools.act.web"),
            )

            # Create a code file
            cursor = db.execute(
                "INSERT INTO code_files (file_path, source, review_status) VALUES (?, ?, ?)",
                ("/tmp/test.py", "test", "approved"),
            )
            code_file_id = cursor.lastrowid

            # Create execution record
            cursor = db.execute(
                "INSERT INTO code_executions (code_file_id, execution_status, started_at) "
                "VALUES (?, 'running', datetime('now'))",
                (code_file_id,),
            )
            execution_id = cursor.lastrowid

            # Create session with conversation_id
            expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            db.execute(
                "INSERT INTO execution_sessions "
                "(session_id, code_file_id, execution_id, reviewed, conversation_id, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, code_file_id, execution_id, True,
                 conv_id, expires_at.isoformat()),
            )
            db.commit()
        finally:
            db.close()
        return session_id

    def _create_clean_session(self, session_id="clean-session"):
        """Create a reviewed session linked to a clean (untainted) conversation."""
        db = get_db()
        try:
            cursor = db.execute(
                "INSERT INTO conversations (started_at) VALUES (datetime('now'))"
            )
            conv_id = cursor.lastrowid

            cursor = db.execute(
                "INSERT INTO code_files (file_path, source, review_status) VALUES (?, ?, ?)",
                ("/tmp/test.py", "test", "approved"),
            )
            code_file_id = cursor.lastrowid

            cursor = db.execute(
                "INSERT INTO code_executions (code_file_id, execution_status, started_at) "
                "VALUES (?, 'running', datetime('now'))",
                (code_file_id,),
            )
            execution_id = cursor.lastrowid

            expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            db.execute(
                "INSERT INTO execution_sessions "
                "(session_id, code_file_id, execution_id, reviewed, conversation_id, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, code_file_id, execution_id, True,
                 conv_id, expires_at.isoformat()),
            )
            db.commit()
        finally:
            db.close()
        return session_id

    def test_tainted_conversation_rejects_arc_create(self):
        """arc.create from tainted conversation is rejected with a pointer
        to arc.create_batch."""
        session_id = self._create_tainted_session("taint-create")

        with pytest.raises(DispatchError, match="arc.create_batch"):
            validate_and_dispatch(
                "arc.create",
                {"name": "new-arc", "integrity_level": "trusted"},
                session_id=session_id,
            )

    def test_tainted_conversation_rejects_add_child(self):
        """arc.add_child from tainted conversation is rejected."""
        from carpenter.core.arcs import manager as arc_manager
        parent_id = arc_manager.create_arc("parent")

        session_id = self._create_tainted_session("taint-child")

        with pytest.raises(DispatchError, match="arc.create_batch"):
            validate_and_dispatch(
                "arc.add_child",
                {"parent_id": parent_id, "name": "child",
                 "integrity_level": "trusted"},
                session_id=session_id,
            )

    def test_clean_conversation_preserves_requested_integrity_level(self):
        """arc.create from clean conversation keeps requested integrity_level."""
        session_id = self._create_clean_session("clean-create")

        result = validate_and_dispatch(
            "arc.create",
            {"name": "clean-arc", "integrity_level": "trusted"},
            session_id=session_id,
        )
        arc_id = result["arc_id"]

        from carpenter.core.arcs import manager as arc_manager
        arc = arc_manager.get_arc(arc_id)
        assert arc["integrity_level"] == "trusted"

    def test_tainted_conversation_rejects_explicit_untrusted(self):
        """Even when the caller explicitly requests untrusted, single-arc
        creation is rejected — the review chain must come from a batch."""
        session_id = self._create_tainted_session("taint-explicit")

        with pytest.raises(DispatchError, match="arc.create_batch"):
            validate_and_dispatch(
                "arc.create",
                {"name": "untrusted-arc", "integrity_level": "untrusted"},
                session_id=session_id,
            )


# --- Tool classification validation tests ---


class TestToolClassification:
    """Verify session-exempt and dispatch sets are consistent."""

    def test_validate_tool_classification_passes(self):
        """All session-exempt tools exist in the dispatch table."""
        validate_tool_classification()

    def test_session_exempt_covers_only_read_tools(self):
        """Session-exempt tools should not include any act/ package tools."""
        import importlib
        import pkgutil
        import carpenter_tools.act as act_pkg
        from carpenter_tools.tool_meta import get_tool_meta

        act_tool_names = set()
        for _imp, modname, _ispkg in pkgutil.iter_modules(act_pkg.__path__):
            mod = importlib.import_module(f"carpenter_tools.act.{modname}")
            for attr in dir(mod):
                meta = get_tool_meta(getattr(mod, attr))
                if meta is not None:
                    act_tool_names.add(f"{modname}.{attr}")

        # No act tool should be session-exempt
        overlap = get_session_exempt_tools() & act_tool_names
        assert overlap == set(), f"Act tools wrongly session-exempt: {overlap}"

    def test_default_deny_for_new_dispatch_entry(self):
        """A tool in _DISPATCH but not in get_session_exempt_tools() requires session."""
        non_exempt = set(_DISPATCH.keys()) - get_session_exempt_tools()
        # Every non-exempt tool must require a session (the check is inverted)
        assert len(non_exempt) > 0, "There should be action tools requiring sessions"


# --- Executor messaging restriction tests ---


class TestExecutorMessagingRestrictions:
    """Verify that arc executor code is blocked from messaging tools."""

    def _create_arc_step_session(self, session_id):
        """Create a reviewed session with execution_context=arc-step."""
        return _create_reviewed_session(session_id, execution_context="arc-step")

    def test_arc_executor_messaging_send_blocked(self):
        """messaging.send from arc-step execution context raises DispatchError."""
        _create_arc()
        session_id = self._create_arc_step_session("arc-msg-send")

        with pytest.raises(DispatchError, match="(?i)arc executor"):
            validate_and_dispatch(
                "messaging.send",
                {"message": "hello user", "arc_id": 1},
                session_id=session_id,
            )

    def test_arc_executor_messaging_ask_blocked(self):
        """messaging.ask from arc-step execution context raises DispatchError."""
        session_id = self._create_arc_step_session("arc-msg-ask")

        with pytest.raises(DispatchError, match="(?i)arc executor"):
            validate_and_dispatch(
                "messaging.ask",
                {"question": "what do you want?"},
                session_id=session_id,
            )

    def test_chat_submit_code_messaging_send_succeeds(self):
        """messaging.send from reviewed (chat submit_code) context succeeds."""
        _create_arc()
        session_id = _create_reviewed_session("chat-msg-send")

        result = validate_and_dispatch(
            "messaging.send",
            {"message": "hello from chat", "arc_id": 1},
            session_id=session_id,
        )
        assert result["success"] is True

    def test_chat_submit_code_messaging_ask_succeeds(self):
        """messaging.ask from reviewed (chat submit_code) context succeeds."""
        session_id = _create_reviewed_session("chat-msg-ask")

        result = validate_and_dispatch(
            "messaging.ask",
            {"question": "what next?"},
            session_id=session_id,
        )
        assert isinstance(result, dict)

    def test_arc_executor_state_tools_still_work(self):
        """state.set from arc-step context still works (not a messaging tool)."""
        _create_arc()
        session_id = self._create_arc_step_session("arc-state")

        result = validate_and_dispatch(
            "state.set",
            {"arc_id": 1, "key": "result", "value": "42"},
            session_id=session_id,
        )
        assert result["success"] is True

    def test_arc_executor_files_tools_still_work(self):
        """files.read from arc-step context still works (read-only, session-exempt)."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("test data")
            path = f.name
        try:
            result = validate_and_dispatch(
                "files.read",
                {"path": path},
            )
            assert result["content"] == "test data"
        finally:
            os.unlink(path)

    def test_arc_executor_arc_tools_still_work(self):
        """arc.get from arc-step context still works (read-only, session-exempt)."""
        _create_arc(arc_id=10)

        result = validate_and_dispatch(
            "arc.get",
            {"arc_id": 10},
        )
        assert isinstance(result, dict)

    def test_no_session_messaging_ask_not_blocked_by_arc_check(self):
        """messaging.ask without session is session-exempt (read-only) and allowed.

        The messaging restriction only fires for arc-step sessions.
        Without a session, there is no arc-step context, so the read-only
        messaging.ask (which is session-exempt) proceeds.
        """
        result = validate_and_dispatch(
            "messaging.ask",
            {"question": "test"},
        )
        # messaging.ask is session-exempt, so it should succeed
        assert isinstance(result, dict)

    def test_messaging_send_without_session_requires_session(self):
        """messaging.send without a session is blocked by session enforcement (not messaging restriction).

        messaging.send is NOT session-exempt, so it gets blocked by the session
        check before the messaging restriction even applies.
        """
        with pytest.raises(DispatchError, match="reviewed execution session"):
            validate_and_dispatch(
                "messaging.send",
                {"message": "no session"},
            )

    def test_messaging_tools_set_is_complete(self):
        """get_messaging_tools() covers all messaging dispatch entries."""
        messaging_dispatch = {k for k in _DISPATCH if k.startswith("messaging.")}
        assert messaging_dispatch == get_messaging_tools()
