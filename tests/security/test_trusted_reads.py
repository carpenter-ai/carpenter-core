"""Trusted contexts must not read untrusted content (I1/I2).

Untrusted or constrained content may reach a trusted LLM context only
through REVIEWER + JUDGE.  These tests cover read paths that used to hand
such content to a trusted reader directly:

- L1: ``read_file`` / ``files.read`` on platform-written stores (Resource
  blobs, truncated tool output, code files and execution logs, per-arc
  workspaces) that carry no ``file_provenance`` row.
- L2: ``read_arc_result``, ``get_arc_detail`` and the arc completion
  notification relaying a REVIEWER's or non-trusted arc's own text.
- L3: ``get_conversation_messages`` and ``list_tool_calls`` previewing a
  REVIEWER's or non-trusted arc's conversation.
- L4: REVIEWER and non-trusted arc conversations are marked tainted.

Every refusal must carry none of the withheld bytes.  REVIEWER readers
keep access to what they review.
"""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from carpenter import config as cfg
from carpenter.agent import conversation, invocation
from carpenter.chat_tool_loader import get_handler
from carpenter.core import code_manager
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import (
    create_resource,
    derive_resource,
    resource_storage_path,
    set_resource_file_path,
)
from carpenter.core.workflows._arc_state import set_arc_state
from carpenter.db import get_db
from carpenter.security.trust import is_conversation_tainted
from carpenter.tool_backends import arc as arc_backend
from carpenter.tool_backends import files as files_backend

CANARY = "CANARY_IGNORE_PREVIOUS_INSTRUCTIONS_7f3a"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _review_batch():
    """Create an untrusted EXECUTOR + REVIEWER + JUDGE batch under a
    trusted parent.  Returns (parent, executor, reviewer, judge)."""
    parent = arc_manager.create_arc("project")
    batch = arc_backend.handle_create_batch({
        "arcs": [
            {"name": "fetch", "parent_id": parent,
             "integrity_level": "untrusted"},
            {"name": "review", "parent_id": parent,
             "agent_type": "REVIEWER",
             "reviewer_profile": "security-reviewer"},
            {"name": "judge", "parent_id": parent,
             "agent_type": "JUDGE", "reviewer_profile": "judge"},
        ]
    })
    executor, reviewer, judge = batch["arc_ids"]
    return parent, executor, reviewer, judge


def _raw_resource_with_blob(content: str) -> tuple[int, str]:
    rid = create_resource(
        content_type="text/html", file_path=None, produced_by_arc_id=None,
    )
    path = resource_storage_path(rid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    set_resource_file_path(rid, str(path))
    return rid, str(path)


def _arc_conversation(arc_id: int) -> int:
    """Create an arc working conversation the way ``_run_arc_agent`` names it."""
    conv_id = conversation.create_conversation()
    conversation.set_conversation_title(conv_id, f"[Arc #{arc_id}] review")
    return conv_id


def _complete(arc_id: int) -> None:
    arc = arc_manager.get_arc(arc_id)
    if arc["status"] == "pending":
        arc_manager.update_status(arc_id, "active")
    arc_manager.update_status(arc_id, "completed")


def _store_agent_response(arc_id: int, text: str) -> None:
    """Write ``_agent_response`` exactly as ``_run_arc_agent`` does (raw SQL)."""
    db = get_db()
    try:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?) "
            "ON CONFLICT(arc_id, key) DO NOTHING",
            (arc_id, "_agent_response", json.dumps(text)),
        )
        db.commit()
    finally:
        db.close()


def _no_canary(text) -> None:
    """Assert the canary is absent without letting pytest diff huge text."""
    leaked = CANARY in str(text)
    assert not leaked, f"canary leaked into a trusted read ({len(str(text))} chars)"


def _chat_read(path: str, **ctx) -> str:
    return get_handler("read_file")({"path": path}, **ctx)


# ---------------------------------------------------------------------------
# L1 — read_file on platform-written stores
# ---------------------------------------------------------------------------

class TestL1ReadFileByPath:

    def test_chat_cannot_read_raw_resource_blob(self):
        _rid, path = _raw_resource_with_blob(CANARY)
        out = _chat_read(path)
        _no_canary(out)
        assert "Access denied" in out

    def test_chat_can_read_approved_resource_blob(self, tmp_path):
        rid = derive_resource(
            content_type="text/plain", file_path=None,
            produced_by_arc_id=None, produced_by_template="t",
            template_verdict="approved",
        )
        path = resource_storage_path(rid, "derived.txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("approved summary", encoding="utf-8")
        set_resource_file_path(rid, str(path))
        assert _chat_read(str(path)) == "approved summary"

    def test_raw_resource_file_outside_store_is_refused(self, tmp_path):
        """A Resource row's ``file_path`` labels the file wherever it is."""
        base = Path(cfg.CONFIG["base_dir"])
        path = base / "inbox" / "payload.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CANARY, encoding="utf-8")
        create_resource(
            content_type="application/json", file_path=str(path),
            produced_by_arc_id=None,
        )
        out = _chat_read(str(path))
        _no_canary(out)

    def test_reviewer_can_read_raw_resource_blob(self):
        _p, _e, reviewer, _j = _review_batch()
        _rid, path = _raw_resource_with_blob(CANARY)
        assert _chat_read(path, executor_arc_id=reviewer) == CANARY

    def test_trusted_arc_files_read_refused_raw_resource_blob(self):
        from carpenter.executor.dispatch_bridge import DispatchError
        planner = arc_manager.create_arc("planner")
        _rid, path = _raw_resource_with_blob(CANARY)
        with pytest.raises(DispatchError) as exc:
            files_backend.handle_read({"path": path, "_caller_arc_id": planner})
        _no_canary(str(exc.value))

    def test_chat_submit_code_files_read_refused_raw_resource_blob(self):
        """``files.read`` with no caller arc is the chat's own submit_code."""
        from carpenter.executor.dispatch_bridge import DispatchError
        _rid, path = _raw_resource_with_blob(CANARY)
        with pytest.raises(DispatchError):
            files_backend.handle_read({"path": path})

    def test_reviewer_files_read_allowed_raw_resource_blob(self):
        _p, _e, reviewer, _j = _review_batch()
        _rid, path = _raw_resource_with_blob(CANARY)
        out = files_backend.handle_read({"path": path, "_caller_arc_id": reviewer})
        assert out["content"] == CANARY

    def test_unlabelled_tool_output_file_is_refused(self):
        """A truncated result written with no recorded context fails closed."""
        big = (CANARY + "\n") * 5000
        stub = invocation._truncate_tool_output(big, "read_file")
        path = stub.split("full output saved to ")[1].split(" (")[0]
        assert os.path.exists(path)
        out = _chat_read(path)
        _no_canary(out)

    def test_reviewer_tool_output_file_is_refused_for_chat(self):
        _p, _e, reviewer, _j = _review_batch()
        conv = _arc_conversation(reviewer)
        big = (CANARY + "\n") * 5000
        stub = invocation._truncate_tool_output(
            big, "read_file", conversation_id=conv, executor_arc_id=reviewer,
        )
        path = stub.split("full output saved to ")[1].split(" (")[0]
        _no_canary(_chat_read(path))
        # The REVIEWER itself can page back through its own output.
        out = _chat_read(path, executor_arc_id=reviewer)
        found = CANARY in out
        assert found, out[:200]

    def test_chat_tool_output_file_stays_readable_for_chat(self):
        conv = conversation.create_conversation()
        big = ("trusted line\n") * 5000
        stub = invocation._truncate_tool_output(
            big, "list_files", conversation_id=conv,
        )
        path = stub.split("full output saved to ")[1].split(" (")[0]
        out = _chat_read(path)
        same = out == big
        assert same, f"chat could not read its own truncated output: {out[:200]!r}"

    def test_untrusted_arc_execution_log_and_code_refused(self):
        _p, executor, _r, _j = _review_batch()
        save = code_manager.save_code(
            f'print("{CANARY}")\n# {CANARY}\n', source="agent", arc_id=executor,
        )
        result = code_manager.execute(save["code_file_id"], arc_id=executor)
        log_file = result["log_file"]
        assert CANARY in Path(log_file).read_text()
        _no_canary(_chat_read(log_file))
        _no_canary(_chat_read(save["file_path"]))

    def test_tainted_execution_log_refused(self):
        save = code_manager.save_code(f'print("{CANARY}")\n', source="chat_agent")
        result = code_manager.execute(save["code_file_id"])
        db = get_db()
        try:
            db.execute(
                "UPDATE code_executions SET taint_source = ? WHERE id = ?",
                ("carpenter_tools.act.web", result["execution_id"]),
            )
            db.commit()
        finally:
            db.close()
        _no_canary(_chat_read(result["log_file"]))

    def test_trusted_execution_log_stays_readable(self):
        save = code_manager.save_code('print("hello")\n', source="chat_agent")
        result = code_manager.execute(save["code_file_id"])
        assert "hello" in _chat_read(result["log_file"])

    def test_untrusted_arc_workspace_file_without_provenance_refused(self):
        """Files an untrusted arc gets into its workspace by other means than
        ``files.write`` (a git clone, say) carry no provenance row."""
        _p, executor, _r, _j = _review_batch()
        ws = Path(cfg.CONFIG["workspaces_dir"]) / f"arc-{executor}" / "repo"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "README.md").write_text(CANARY, encoding="utf-8")
        _no_canary(_chat_read(str(ws / "README.md")))

    def test_reviewer_file_write_is_labelled_untrusted(self):
        """A REVIEWER writes from an untrusted context; its files are not
        trusted just because the arc's integrity level is."""
        _p, _e, reviewer, _j = _review_batch()
        path = Path(cfg.CONFIG["base_dir"]) / "notes" / "summary.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        files_backend.handle_write({
            "path": str(path), "content": CANARY, "_caller_arc_id": reviewer,
        })
        _no_canary(_chat_read(str(path)))


# ---------------------------------------------------------------------------
# L2 — arc results
# ---------------------------------------------------------------------------

class TestL2ArcResults:

    def test_read_arc_result_withholds_reviewer_response(self):
        _p, _e, reviewer, _j = _review_batch()
        _store_agent_response(reviewer, CANARY)
        _complete(reviewer)
        out = get_handler("read_arc_result")({"arc_id": reviewer})
        _no_canary(out)
        assert "withheld" in out.lower()

    def test_read_arc_result_withholds_untrusted_executor_response(self):
        _p, executor, _r, _j = _review_batch()
        _store_agent_response(executor, CANARY)
        _complete(executor)
        out = get_handler("read_arc_result")({"arc_id": executor})
        _no_canary(out)

    def test_read_arc_result_child_fallback_skips_reviewer(self):
        parent, executor, reviewer, _j = _review_batch()
        _store_agent_response(executor, CANARY + "_exec")
        _store_agent_response(reviewer, CANARY)
        _complete(executor)
        _complete(reviewer)
        _complete(parent)
        out = get_handler("read_arc_result")({"arc_id": parent})
        _no_canary(out)

    def test_read_arc_result_trusted_child_still_shown(self):
        parent = arc_manager.create_arc("parent")
        arc_manager.update_status(parent, "active")
        child = arc_manager.add_child(parent, "step", goal="do")
        _store_agent_response(child, "trusted child result")
        _complete(child)
        _complete(parent)
        out = get_handler("read_arc_result")({"arc_id": parent})
        assert "trusted child result" in out

    def test_reviewer_may_read_its_own_result(self):
        _p, _e, reviewer, _j = _review_batch()
        _store_agent_response(reviewer, CANARY)
        _complete(reviewer)
        out = get_handler("read_arc_result")(
            {"arc_id": reviewer}, executor_arc_id=reviewer,
        )
        assert CANARY in out

    def test_get_arc_detail_withholds_reviewer_state_values(self):
        _p, _e, reviewer, _j = _review_batch()
        _store_agent_response(reviewer, CANARY)
        out = get_handler("get_arc_detail")({"arc_id": reviewer})
        _no_canary(out)
        # The key is still listed, so the reader knows it exists.
        assert "_agent_response" in out

    def test_get_arc_detail_withholds_reviewer_history_content(self):
        _p, _e, reviewer, _j = _review_batch()
        arc_manager.add_history(reviewer, "note", {"text": CANARY})
        out = get_handler("get_arc_detail")({"arc_id": reviewer})
        _no_canary(out)
        assert "note" in out

    def test_get_arc_detail_trusted_arc_unchanged(self):
        arc_id = arc_manager.create_arc("plain")
        set_arc_state(arc_id, "workspace", "/tmp/ws")
        out = get_handler("get_arc_detail")({"arc_id": arc_id})
        assert "/tmp/ws" in out

    @pytest.mark.asyncio
    async def test_completion_notice_does_not_relay_reviewer_response(self):
        from carpenter.core.workflows.arc_notify_handler import (
            handle_arc_chat_notify,
        )
        parent, executor, reviewer, _j = _review_batch()
        _store_agent_response(reviewer, CANARY)
        for a in (executor, reviewer):
            _complete(a)
        _complete(parent)
        conv_id = conversation.get_or_create_conversation()
        db = get_db()
        try:
            db.execute(
                "INSERT INTO conversation_arcs (conversation_id, arc_id) "
                "VALUES (?, ?)", (conv_id, parent),
            )
            db.commit()
        finally:
            db.close()
        with patch(
            "carpenter.core.workflows.arc_notify_handler.thread_pools."
            "run_in_work_pool", AsyncMock(),
        ):
            await handle_arc_chat_notify(1, {"arc_id": parent})
        system = [m for m in conversation.get_messages(conv_id)
                  if m["role"] == "system"]
        assert system
        assert all(CANARY not in m["content"] for m in system)


class TestL2StateReads:
    """The same untrusted text reached through ``state.get``."""

    def test_trusted_arc_cannot_state_get_reviewer_response(self):
        from carpenter.executor.dispatch_bridge import (
            DispatchError, validate_and_dispatch,
        )
        parent, _e, reviewer, _j = _review_batch()
        _store_agent_response(reviewer, CANARY)
        with pytest.raises(DispatchError) as exc:
            validate_and_dispatch(
                "state.get",
                {"key": "_agent_response", "arc_id": reviewer},
                arc_id=parent,
            )
        _no_canary(str(exc.value))

    def test_trusted_arc_reads_own_state(self):
        from carpenter.executor.dispatch_bridge import validate_and_dispatch
        arc_id = arc_manager.create_arc("plain")
        set_arc_state(arc_id, "note", "mine")
        out = validate_and_dispatch("state.get", {"key": "note"}, arc_id=arc_id)
        assert out["value"] == "mine"

    def test_chat_get_state_withholds_tainted_execution_output(self):
        """Tainted ``submit_code`` output is kept under arc 0 in plaintext."""
        from carpenter.tool_backends import state as state_backend
        state_backend.handle_set({
            "arc_id": 0, "key": "exec_000042",
            "value": {"_tainted": True, "output": CANARY},
        })
        out = get_handler("get_state")({"key": "exec_000042"})
        _no_canary(out)

    def test_chat_get_state_reads_plain_conversation_state(self):
        from carpenter.tool_backends import state as state_backend
        state_backend.handle_set({"arc_id": 0, "key": "k", "value": "v"})
        assert get_handler("get_state")({"key": "k"}) == '"v"'

    def test_reviewer_may_read_tainted_execution_output(self):
        from carpenter.executor.dispatch_bridge import validate_and_dispatch
        from carpenter.tool_backends import state as state_backend
        _p, _e, reviewer, _j = _review_batch()
        state_backend.handle_set({
            "arc_id": 0, "key": "exec_000043",
            "value": {"_tainted": True, "output": CANARY},
        })
        out = validate_and_dispatch(
            "state.get", {"key": "exec_000043", "arc_id": 0}, arc_id=reviewer,
        )
        assert out["value"]["output"] == CANARY


class TestCallerIdentity:
    """The reader's identity comes from the platform, never from input."""

    def test_chat_code_cannot_claim_reviewer_identity(self):
        from carpenter.executor.dispatch_bridge import (
            DispatchError, validate_and_dispatch,
        )
        _p, _e, reviewer, _j = _review_batch()
        _rid, path = _raw_resource_with_blob(CANARY)
        with pytest.raises(DispatchError) as exc:
            validate_and_dispatch(
                "files.read", {"path": path, "_caller_arc_id": reviewer},
            )
        _no_canary(str(exc.value))

    def test_read_file_tool_ignores_caller_arc_in_input(self):
        _p, _e, reviewer, _j = _review_batch()
        _rid, path = _raw_resource_with_blob(CANARY)
        out = get_handler("read_file")({"path": path, "_caller_arc_id": reviewer})
        _no_canary(out)

    def test_backend_uses_invocation_context_when_tool_passes_none(self):
        """A chat tool module that predates the gate passes no identity.
        The platform-set invocation context still gives the REVIEWER its
        access, and still refuses the chat."""
        _p, _e, reviewer, _j = _review_batch()
        _rid, path = _raw_resource_with_blob(CANARY)
        out = invocation._execute_chat_tool(
            "read_file", {"path": path}, executor_arc_id=reviewer,
        )
        assert out == CANARY
        from carpenter.security import read_gate
        with read_gate.invocation_context(None, reviewer):
            assert files_backend.chat_read_provenance_check(path) is None
            assert files_backend.handle_read({"path": path})["content"] == CANARY
        with read_gate.invocation_context(None, None):
            assert files_backend.chat_read_provenance_check(path)
        _no_canary(invocation._execute_chat_tool("read_file", {"path": path}))


# ---------------------------------------------------------------------------
# L3 — conversation introspection
# ---------------------------------------------------------------------------

class TestL3ConversationIntrospection:

    def _reviewer_conv_with_canary(self):
        _p, _e, reviewer, _j = _review_batch()
        conv = _arc_conversation(reviewer)
        conversation.add_message(conv, "tool_result", CANARY)
        db = get_db()
        try:
            db.execute(
                "INSERT INTO tool_calls (conversation_id, tool_use_id, "
                "tool_name, input_json, result_text) VALUES (?, ?, ?, ?, ?)",
                (conv, "tu_1", "read_file",
                 json.dumps({"path": CANARY}), CANARY),
            )
            db.commit()
        finally:
            db.close()
        return reviewer, conv

    def test_get_conversation_messages_withholds_reviewer_transcript(self):
        _r, conv = self._reviewer_conv_with_canary()
        out = get_handler("get_conversation_messages")({"conversation_id": conv})
        _no_canary(out)

    def test_list_tool_calls_withholds_reviewer_calls_filtered(self):
        _r, conv = self._reviewer_conv_with_canary()
        out = get_handler("list_tool_calls")({"conversation_id": conv})
        _no_canary(out)
        assert "read_file" in out  # metadata stays visible

    def test_list_tool_calls_withholds_reviewer_calls_unfiltered(self):
        self._reviewer_conv_with_canary()
        out = get_handler("list_tool_calls")({})
        _no_canary(out)

    def test_tainted_conversation_withheld_from_other_conversation(self):
        from carpenter.security.trust import record_taint
        other = conversation.create_conversation()
        conversation.add_message(other, "user", CANARY)
        record_taint(other, "carpenter_tools.act.web")
        me = conversation.create_conversation()
        out = get_handler("get_conversation_messages")(
            {"conversation_id": other}, conversation_id=me,
        )
        _no_canary(out)

    def test_own_conversation_readable(self):
        from carpenter.security.trust import record_taint
        me = conversation.create_conversation()
        conversation.add_message(me, "user", "my own words")
        record_taint(me, "carpenter_tools.act.web")
        out = get_handler("get_conversation_messages")(
            {"conversation_id": me}, conversation_id=me,
        )
        assert "my own words" in out

    def test_trusted_conversation_readable(self):
        conv = conversation.create_conversation()
        conversation.add_message(conv, "user", "hello there")
        out = get_handler("get_conversation_messages")({"conversation_id": conv})
        assert "hello there" in out

    def test_reviewer_may_read_its_own_transcript(self):
        reviewer, conv = self._reviewer_conv_with_canary()
        out = get_handler("list_tool_calls")(
            {"conversation_id": conv},
            conversation_id=conv, executor_arc_id=reviewer,
        )
        assert CANARY in out

    def test_get_execution_output_withholds_untrusted_arc_log(self):
        _p, executor, _r, _j = _review_batch()
        save = code_manager.save_code(
            f'print("{CANARY}")\n', source="agent", arc_id=executor,
        )
        result = code_manager.execute(save["code_file_id"], arc_id=executor)
        out = get_handler("get_execution_output")(
            {"execution_id": result["execution_id"]},
        )
        _no_canary(out)
        assert "withheld" in out.lower()


# ---------------------------------------------------------------------------
# L4 — REVIEWER and non-trusted arc conversations are tainted
# ---------------------------------------------------------------------------

async def _run_agent(arc_id: int) -> int:
    from carpenter.core.arcs import dispatch_handler
    mock = AsyncMock(return_value={"response_text": ""})
    with patch("carpenter.thread_pools.run_in_work_pool", mock):
        await dispatch_handler._run_arc_agent(arc_id, "goal", None)
    return mock.call_args.kwargs["conversation_id"]


class TestL4ArcConversationTaint:

    @pytest.mark.asyncio
    async def test_reviewer_conversation_is_tainted(self):
        _p, _e, reviewer, _j = _review_batch()
        conv = await _run_agent(reviewer)
        assert is_conversation_tainted(conv)

    @pytest.mark.asyncio
    async def test_untrusted_executor_conversation_is_tainted(self):
        _p, executor, _r, _j = _review_batch()
        conv = await _run_agent(executor)
        assert is_conversation_tainted(conv)

    @pytest.mark.asyncio
    async def test_trusted_executor_conversation_is_clean(self):
        arc_id = arc_manager.create_arc("plain")
        conv = await _run_agent(arc_id)
        assert not is_conversation_tainted(conv)


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------

class TestReadGate:

    def test_reader_roles(self):
        from carpenter.security import read_gate as rg
        _p, executor, reviewer, judge = _review_batch()
        planner = arc_manager.create_arc("planner")
        assert rg.reader_for().role == rg.ROLE_TRUSTED
        assert rg.reader_for(arc_id=planner).role == rg.ROLE_TRUSTED
        assert rg.reader_for(arc_id=reviewer).role == rg.ROLE_REVIEWER
        assert rg.reader_for(arc_id=judge).role == rg.ROLE_JUDGE
        assert rg.reader_for(arc_id=executor).role == rg.ROLE_UNTRUSTED
        # An unknown arc is the most restrictive reader.
        assert rg.reader_for(arc_id=987654).role == rg.ROLE_TRUSTED

    def test_matrix(self):
        from carpenter.security import read_gate as rg
        t = rg.Label(rg.TRUSTED, "x")
        u = rg.Label(rg.UNTRUSTED, "y")
        for role in (rg.ROLE_TRUSTED, rg.ROLE_REVIEWER, rg.ROLE_JUDGE,
                     rg.ROLE_UNTRUSTED):
            reader = rg.Reader(role=role)
            assert rg.may_read(reader, t)
            assert rg.may_read(reader, u) == (role != rg.ROLE_TRUSTED)

    def test_arc_labels(self):
        from carpenter.security import read_gate as rg
        _p, executor, reviewer, judge = _review_batch()
        planner = arc_manager.create_arc("planner")
        assert rg.arc_label(planner).level == rg.TRUSTED
        assert rg.arc_label(judge).level == rg.TRUSTED
        assert rg.arc_label(reviewer).level == rg.UNTRUSTED
        assert rg.arc_label(executor).level == rg.UNTRUSTED
        assert rg.arc_label(987654).level == rg.UNTRUSTED

    def test_refusal_is_audited(self):
        _rid, path = _raw_resource_with_blob(CANARY)
        _chat_read(path)
        db = get_db()
        try:
            rows = db.execute(
                "SELECT details_json FROM trust_audit_log "
                "WHERE event_type = 'trusted_read_refused'"
            ).fetchall()
        finally:
            db.close()
        assert rows
        assert all(CANARY not in r["details_json"] for r in rows)
