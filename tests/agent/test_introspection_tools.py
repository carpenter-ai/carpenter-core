"""Tests for chat agent introspection and arc management tools."""

import json

import pytest

from carpenter.agent.invocation import _save_api_call
from carpenter.chat_tool_loader import get_handler


def _tool_list_arcs(inp):
    return get_handler("list_arcs")(inp)


def _tool_get_arc_detail(inp):
    return get_handler("get_arc_detail")(inp)


def _tool_list_recent_activity(inp):
    return get_handler("list_recent_activity")(inp)


def _tool_list_tool_calls(inp):
    return get_handler("list_tool_calls")(inp)


def _tool_list_code_executions(inp):
    return get_handler("list_code_executions")(inp)


def _tool_get_execution_output(inp):
    return get_handler("get_execution_output")(inp)


def _tool_list_conversations(inp):
    return get_handler("list_conversations")(inp)


def _tool_get_conversation_messages(inp):
    return get_handler("get_conversation_messages")(inp)


def _tool_list_api_calls(inp):
    return get_handler("list_api_calls")(inp)


def _tool_get_cache_stats(inp):
    return get_handler("get_cache_stats")(inp)
from carpenter.agent import conversation
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import work_queue
from carpenter.core import code_manager
from carpenter.db import get_db


class TestListArcs:
    def test_empty(self):
        result = _tool_list_arcs({})
        # Sentinel arc (id=0) for conversation-level state is always present
        assert "#0" in result
        assert "_sentinel" in result

    def test_shows_arcs(self):
        arc_manager.create_arc(name="test-arc", goal="Do something useful")
        arc_manager.create_arc(name="other-arc", goal="Another task")

        result = _tool_list_arcs({})
        assert "#1" in result
        assert "#2" in result
        assert "test-arc" in result
        assert "other-arc" in result
        assert "Do something useful" in result

    def test_status_filter(self):
        a1 = arc_manager.create_arc(name="pending-arc", goal="Waiting")
        a2 = arc_manager.create_arc(name="active-arc", goal="Running")
        arc_manager.update_status(a2, "active")

        result = _tool_list_arcs({"status": "active"})
        assert "active-arc" in result
        assert "pending-arc" not in result

        result = _tool_list_arcs({"status": "pending"})
        assert "pending-arc" in result
        assert "active-arc" not in result

    def test_limit(self):
        for i in range(5):
            arc_manager.create_arc(name=f"arc-{i}", goal=f"Goal {i}")

        result = _tool_list_arcs({"limit": 2})
        # Should show the 2 newest (highest IDs)
        lines = [l for l in result.strip().split("\n") if l.strip()]
        assert len(lines) == 2

    def test_truncates_long_goal(self):
        long_goal = "x" * 200
        arc_manager.create_arc(name="long-goal-arc", goal=long_goal)

        result = _tool_list_arcs({})
        # Goal should be truncated to 80 chars
        assert "x" * 80 in result
        assert "x" * 81 not in result


class TestGetArcDetail:
    def test_not_found(self):
        result = _tool_get_arc_detail({"arc_id": 9999})
        assert "not found" in result

    def test_basic_arc(self):
        arc_id = arc_manager.create_arc(name="detail-arc", goal="Test details")

        result = _tool_get_arc_detail({"arc_id": arc_id})
        assert f"Arc #{arc_id}" in result
        assert "detail-arc" in result
        assert "Test details" in result
        assert "pending" in result

    def test_includes_state(self):
        arc_id = arc_manager.create_arc(name="stateful-arc", goal="Has state")
        db = get_db()
        try:
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                (arc_id, "workspace_path", json.dumps("/tmp/workspace")),
            )
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                (arc_id, "review_url", json.dumps("/api/review/abc123")),
            )
            db.commit()
        finally:
            db.close()

        result = _tool_get_arc_detail({"arc_id": arc_id})
        assert "State:" in result
        assert "workspace_path" in result
        assert "/tmp/workspace" in result
        assert "review_url" in result

    def test_includes_history(self):
        arc_id = arc_manager.create_arc(name="history-arc", goal="Has history")
        arc_manager.add_history(
            arc_id, "status_change",
            {"from": "pending", "to": "active"},
            actor="system",
        )
        arc_manager.add_history(
            arc_id, "note",
            {"message": "Agent started working"},
            actor="agent",
        )

        result = _tool_get_arc_detail({"arc_id": arc_id})
        assert "History" in result
        assert "status_change" in result
        assert "note" in result
        assert "agent" in result

    def test_truncates_long_state_values(self):
        arc_id = arc_manager.create_arc(name="big-state-arc", goal="Big state")
        db = get_db()
        try:
            long_val = json.dumps("x" * 500)
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                (arc_id, "big_key", long_val),
            )
            db.commit()
        finally:
            db.close()

        result = _tool_get_arc_detail({"arc_id": arc_id})
        assert "big_key" in result
        assert "..." in result


class TestListRecentActivity:
    def test_empty(self):
        result = _tool_list_recent_activity({})
        assert result == "No recent activity."

    def test_shows_work_items(self):
        work_queue.enqueue("test.event", {"data": "hello"})
        work_queue.enqueue("test.other", {"data": "world"})

        result = _tool_list_recent_activity({})
        assert "test.event" in result
        assert "test.other" in result
        assert "pending" in result

    def test_shows_completed_items(self):
        wid = work_queue.enqueue("test.complete", {"x": 1})
        item = work_queue.claim()
        work_queue.complete(item["id"])

        result = _tool_list_recent_activity({})
        assert "complete" in result
        assert "completed=" in result

    def test_shows_errors(self):
        work_queue.enqueue("test.fail", {"x": 1}, max_retries=0)
        item = work_queue.claim()
        work_queue.fail(item["id"], "Something went wrong")

        result = _tool_list_recent_activity({})
        assert "Something went wrong" in result

    def test_limit(self):
        for i in range(5):
            work_queue.enqueue(f"test.item-{i}", {"i": i})

        result = _tool_list_recent_activity({"limit": 2})
        lines = [l for l in result.strip().split("\n") if l.strip()]
        assert len(lines) == 2


# --- History introspection tools ---


class TestListToolCalls:
    def _insert_tool_call(self, conv_id, msg_id, tool_use_id, name, inp, result, dur):
        db = get_db()
        try:
            db.execute(
                "INSERT INTO tool_calls "
                "(conversation_id, message_id, tool_use_id, tool_name, "
                " input_json, result_text, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conv_id, msg_id, tool_use_id, name, json.dumps(inp), result, dur),
            )
            db.commit()
        finally:
            db.close()

    def test_empty(self):
        result = _tool_list_tool_calls({})
        assert result == "No tool calls found."

    def test_shows_tool_calls(self):
        conv_id = conversation.get_or_create_conversation()
        msg_id = conversation.add_message(conv_id, "assistant", "test")
        self._insert_tool_call(conv_id, msg_id, "tu_1", "read_file",
                               {"path": "/tmp/x"}, "file content", 25)

        result = _tool_list_tool_calls({})
        assert "read_file" in result
        assert "25ms" in result
        assert "/tmp/x" in result

    def test_filter_by_tool_name(self):
        conv_id = conversation.get_or_create_conversation()
        msg_id = conversation.add_message(conv_id, "assistant", "test")
        self._insert_tool_call(conv_id, msg_id, "tu_a", "read_file",
                               {"path": "/a"}, "a", 10)
        self._insert_tool_call(conv_id, msg_id, "tu_b", "write_file",
                               {"path": "/b"}, "ok", 20)

        result = _tool_list_tool_calls({"tool_name": "read_file"})
        assert "read_file" in result
        assert "write_file" not in result

    def test_filter_by_conversation(self):
        c1 = conversation.get_or_create_conversation()
        m1 = conversation.add_message(c1, "assistant", "t1")
        self._insert_tool_call(c1, m1, "tu_1", "get_state", {"key": "x"}, "v", 5)

        # Create second conversation
        db = get_db()
        try:
            cursor = db.execute(
                "INSERT INTO conversations (last_message_at) VALUES (NULL)"
            )
            c2 = cursor.lastrowid
            db.commit()
            cursor = db.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
                (c2, "assistant", "t2"),
            )
            m2 = cursor.lastrowid
            db.commit()
        finally:
            db.close()
        self._insert_tool_call(c2, m2, "tu_2", "set_state", {"key": "y"}, "ok", 3)

        result = _tool_list_tool_calls({"conversation_id": c1})
        assert "get_state" in result
        assert "set_state" not in result

    def test_limit(self):
        conv_id = conversation.get_or_create_conversation()
        msg_id = conversation.add_message(conv_id, "assistant", "test")
        for i in range(5):
            self._insert_tool_call(conv_id, msg_id, f"tu_{i}", f"tool_{i}",
                                   {}, "ok", i)

        result = _tool_list_tool_calls({"limit": 2})
        # Each tool call generates 3 lines of output
        assert "tool_4" in result  # newest
        assert "tool_3" in result
        assert "tool_2" not in result


class TestListCodeExecutions:
    def test_empty(self):
        result = _tool_list_code_executions({})
        assert result == "No code executions found."

    def test_shows_executions(self):
        save_result = code_manager.save_code('print("hello")', source="chat_agent", name="test")
        exec_result = code_manager.execute(save_result["code_file_id"])

        result = _tool_list_code_executions({})
        assert "success" in result
        assert "chat_agent" in result

    def test_filter_by_status(self):
        # Create a success
        s1 = code_manager.save_code('print("ok")', source="chat_agent", name="good")
        code_manager.execute(s1["code_file_id"])
        # Create a failure
        s2 = code_manager.save_code('import sys; sys.exit(1)', source="chat_agent", name="bad")
        code_manager.execute(s2["code_file_id"])

        result = _tool_list_code_executions({"status": "failed"})
        assert "failed" in result
        assert "bad" in result

    def test_filter_by_source(self):
        code_manager.save_code('print("a")', source="agent", name="arc_step")
        s2 = code_manager.save_code('print("b")', source="chat_agent", name="chat_run")
        code_manager.execute(s2["code_file_id"])

        result = _tool_list_code_executions({"source": "chat_agent"})
        assert "chat_agent" in result


class TestGetExecutionOutput:
    def test_not_found(self):
        result = _tool_get_execution_output({"execution_id": 99999})
        assert "not found" in result

    def test_reads_log(self):
        save_result = code_manager.save_code('print("hello output")', source="chat_agent", name="out_test")
        exec_result = code_manager.execute(save_result["code_file_id"])

        result = _tool_get_execution_output({"execution_id": exec_result["execution_id"]})
        assert "hello output" in result
        assert "success" in result

    def test_shows_failure_output(self):
        save_result = code_manager.save_code(
            'raise ValueError("deliberate failure 42")',
            source="chat_agent", name="fail_test",
        )
        exec_result = code_manager.execute(save_result["code_file_id"])

        result = _tool_get_execution_output({"execution_id": exec_result["execution_id"]})
        assert "42" in result  # part of error message
        assert "deliberate failure" in result
        assert "failed" in result


class TestListConversations:
    def test_empty_initially(self):
        # Note: test_db fixture starts fresh, no conversations yet
        result = _tool_list_conversations({})
        assert result == "No conversations found."

    def test_shows_conversations(self):
        c1 = conversation.get_or_create_conversation()
        conversation.add_message(c1, "user", "Hello")
        conversation.add_message(c1, "assistant", "Hi")

        result = _tool_list_conversations({})
        assert f"conv#{c1}" in result
        assert "messages=2" in result

    def test_limit(self):
        for _ in range(3):
            db = get_db()
            try:
                cursor = db.execute(
                    "INSERT INTO conversations (last_message_at) VALUES (CURRENT_TIMESTAMP)"
                )
                db.commit()
            finally:
                db.close()

        result = _tool_list_conversations({"limit": 2})
        lines = [l for l in result.strip().split("\n") if l.strip().startswith("conv#")]
        assert len(lines) == 2


class TestGetConversationMessages:
    def test_empty_conversation(self):
        c_id = conversation.get_or_create_conversation()
        result = _tool_get_conversation_messages({"conversation_id": c_id})
        assert "No messages" in result

    def test_shows_messages(self):
        c_id = conversation.get_or_create_conversation()
        conversation.add_message(c_id, "user", "Hello world")
        conversation.add_message(c_id, "assistant", "Hi there!")

        result = _tool_get_conversation_messages({"conversation_id": c_id})
        assert f"Conversation #{c_id}" in result
        assert "[user]" in result
        assert "[assistant]" in result
        assert "Hello world" in result
        assert "Hi there!" in result

    def test_shows_structured_annotation(self):
        c_id = conversation.get_or_create_conversation()
        blocks = json.dumps([{"type": "tool_use", "id": "t1", "name": "x", "input": {}}])
        conversation.add_message(c_id, "assistant", "[tools]", content_json=blocks)

        result = _tool_get_conversation_messages({"conversation_id": c_id})
        assert "[structured]" in result

    def test_limit(self):
        c_id = conversation.get_or_create_conversation()
        for i in range(10):
            conversation.add_message(c_id, "user", f"Message {i}")

        result = _tool_get_conversation_messages({"conversation_id": c_id, "limit": 3})
        assert "Message 0" in result
        assert "Message 2" in result
        assert "Message 3" not in result

    def test_limit_zero_returns_all(self):
        c_id = conversation.get_or_create_conversation()
        for i in range(10):
            conversation.add_message(c_id, "user", f"Msg {i}")

        result = _tool_get_conversation_messages({"conversation_id": c_id, "limit": 0})
        assert "Msg 9" in result
        assert "10 messages" in result


class TestSaveApiCall:
    def test_saves_metrics(self):
        c_id = conversation.get_or_create_conversation()
        usage = {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_creation_input_tokens": 500,
            "cache_read_input_tokens": 3000,
        }
        _save_api_call(c_id, "claude-haiku-4-5-20251001", usage, "end_turn")

        db = get_db()
        try:
            row = db.execute("SELECT * FROM api_calls ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            db.close()
        assert row is not None
        assert row["conversation_id"] == c_id
        assert row["model"] == "claude-haiku-4-5-20251001"
        assert row["input_tokens"] == 1000
        assert row["output_tokens"] == 200
        assert row["cache_creation_input_tokens"] == 500
        assert row["cache_read_input_tokens"] == 3000
        assert row["stop_reason"] == "end_turn"

    def test_handles_missing_keys(self):
        c_id = conversation.get_or_create_conversation()
        _save_api_call(c_id, "test-model", {}, "tool_use")

        db = get_db()
        try:
            row = db.execute("SELECT * FROM api_calls ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            db.close()
        assert row["input_tokens"] == 0
        assert row["cache_read_input_tokens"] == 0


class TestListApiCalls:
    def test_empty(self):
        result = _tool_list_api_calls({})
        assert result == "No API calls recorded."

    def test_shows_calls(self):
        c_id = conversation.get_or_create_conversation()
        _save_api_call(c_id, "claude-haiku-4-5-20251001", {
            "input_tokens": 500, "output_tokens": 50,
            "cache_read_input_tokens": 400, "cache_creation_input_tokens": 0,
        }, "end_turn")

        result = _tool_list_api_calls({})
        assert "claude-haiku" in result
        assert "cache_read=400" in result
        assert "hit_rate=" in result

    def test_filter_by_conversation(self):
        c1 = conversation.get_or_create_conversation()
        _save_api_call(c1, "model-a", {"input_tokens": 100}, "end_turn")

        db = get_db()
        try:
            cursor = db.execute("INSERT INTO conversations (last_message_at) VALUES (NULL)")
            c2 = cursor.lastrowid
            db.commit()
        finally:
            db.close()
        _save_api_call(c2, "model-b", {"input_tokens": 200}, "end_turn")

        result = _tool_list_api_calls({"conversation_id": c1})
        assert "model-a" in result
        assert "model-b" not in result

    def test_limit(self):
        c_id = conversation.get_or_create_conversation()
        for i in range(5):
            _save_api_call(c_id, f"model-{i}", {"input_tokens": i * 100}, "end_turn")

        result = _tool_list_api_calls({"limit": 2})
        assert "model-4" in result
        assert "model-3" in result
        assert "model-2" not in result


class TestGetCacheStats:
    def test_empty(self):
        result = _tool_get_cache_stats({})
        assert "No API calls recorded" in result

    def test_aggregated_stats(self):
        c_id = conversation.get_or_create_conversation()
        # Call 1: cache creation
        _save_api_call(c_id, "haiku", {
            "input_tokens": 500, "output_tokens": 100,
            "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 0,
        }, "tool_use")
        # Call 2: cache hit
        _save_api_call(c_id, "haiku", {
            "input_tokens": 200, "output_tokens": 80,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3000,
        }, "end_turn")

        result = _tool_get_cache_stats({})
        assert "2 calls" in result
        assert "700" in result  # total input
        assert "3,000" in result  # cache_read total
        assert "savings" in result.lower()
        assert f"conv#{c_id}" in result

    def test_per_conversation_breakdown(self):
        c1 = conversation.get_or_create_conversation()
        _save_api_call(c1, "haiku", {
            "input_tokens": 100, "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 0,
        }, "end_turn")

        db = get_db()
        try:
            cursor = db.execute("INSERT INTO conversations (last_message_at) VALUES (NULL)")
            c2 = cursor.lastrowid
            db.commit()
        finally:
            db.close()
        _save_api_call(c2, "haiku", {
            "input_tokens": 200, "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 0,
        }, "end_turn")

        result = _tool_get_cache_stats({})
        assert f"conv#{c1}" in result
        assert f"conv#{c2}" in result


