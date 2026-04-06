"""Tests for arc performance counters (descendant_tokens, descendant_executions, descendant_arc_count)."""

import json

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.db import get_db


class TestCounterDefaults:
    """New arcs should have all counters at 0."""

    def test_new_arc_counters_default_zero(self):
        arc_id = arc_manager.create_arc("root", goal="test counters")
        arc = arc_manager.get_arc(arc_id)

        assert arc["descendant_tokens"] == 0
        assert arc["descendant_executions"] == 0
        assert arc["descendant_arc_count"] == 0


class TestDescendantArcCount:
    """Adding child arcs should increment ancestor descendant_arc_count."""

    def test_add_child_increments_parent_count(self):
        parent_id = arc_manager.create_arc("parent", goal="parent")
        arc_manager.add_child(parent_id, "child-1")

        parent = arc_manager.get_arc(parent_id)
        assert parent["descendant_arc_count"] == 1

    def test_add_multiple_children_increments(self):
        parent_id = arc_manager.create_arc("parent", goal="parent")
        arc_manager.add_child(parent_id, "child-1")
        arc_manager.add_child(parent_id, "child-2")
        arc_manager.add_child(parent_id, "child-3")

        parent = arc_manager.get_arc(parent_id)
        assert parent["descendant_arc_count"] == 3

    def test_grandchild_increments_both_ancestors(self):
        root_id = arc_manager.create_arc("root", goal="root")
        child_id = arc_manager.add_child(root_id, "child")
        arc_manager.add_child(child_id, "grandchild")

        root = arc_manager.get_arc(root_id)
        child = arc_manager.get_arc(child_id)

        # Root has 2 descendants (child + grandchild)
        assert root["descendant_arc_count"] == 2
        # Child has 1 descendant (grandchild)
        assert child["descendant_arc_count"] == 1

    def test_deep_nesting_updates_all_ancestors(self):
        """Counters propagate up through a deeply nested chain."""
        root_id = arc_manager.create_arc("root", goal="root")
        level1_id = arc_manager.add_child(root_id, "level-1")
        level2_id = arc_manager.add_child(level1_id, "level-2")
        arc_manager.add_child(level2_id, "level-3")

        root = arc_manager.get_arc(root_id)
        level1 = arc_manager.get_arc(level1_id)
        level2 = arc_manager.get_arc(level2_id)

        assert root["descendant_arc_count"] == 3
        assert level1["descendant_arc_count"] == 2
        assert level2["descendant_arc_count"] == 1


class TestDescendantExecutions:
    """Code executions should increment ancestor descendant_executions."""

    def test_execution_increments_ancestors(self):
        parent_id = arc_manager.create_arc("parent", goal="parent")
        child_id = arc_manager.add_child(parent_id, "child")

        # Simulate a code execution for the child arc
        arc_manager.increment_ancestor_executions(child_id)

        parent = arc_manager.get_arc(parent_id)
        assert parent["descendant_executions"] == 1

    def test_multiple_executions_accumulate(self):
        parent_id = arc_manager.create_arc("parent", goal="parent")
        child_id = arc_manager.add_child(parent_id, "child")

        arc_manager.increment_ancestor_executions(child_id)
        arc_manager.increment_ancestor_executions(child_id)
        arc_manager.increment_ancestor_executions(child_id)

        parent = arc_manager.get_arc(parent_id)
        assert parent["descendant_executions"] == 3

    def test_execution_propagates_up_chain(self):
        root_id = arc_manager.create_arc("root", goal="root")
        child_id = arc_manager.add_child(root_id, "child")
        grandchild_id = arc_manager.add_child(child_id, "grandchild")

        arc_manager.increment_ancestor_executions(grandchild_id)

        root = arc_manager.get_arc(root_id)
        child = arc_manager.get_arc(child_id)

        # Execution in grandchild increments both child and root
        assert root["descendant_executions"] == 1
        assert child["descendant_executions"] == 1


class TestDescendantTokens:
    """Token usage should increment ancestor descendant_tokens."""

    def test_tokens_increment_ancestors(self):
        parent_id = arc_manager.create_arc("parent", goal="parent")
        child_id = arc_manager.add_child(parent_id, "child")

        arc_manager.increment_ancestor_tokens(child_id, 1500)

        parent = arc_manager.get_arc(parent_id)
        assert parent["descendant_tokens"] == 1500

    def test_tokens_accumulate(self):
        parent_id = arc_manager.create_arc("parent", goal="parent")
        child_id = arc_manager.add_child(parent_id, "child")

        arc_manager.increment_ancestor_tokens(child_id, 500)
        arc_manager.increment_ancestor_tokens(child_id, 800)

        parent = arc_manager.get_arc(parent_id)
        assert parent["descendant_tokens"] == 1300

    def test_tokens_propagate_up_chain(self):
        root_id = arc_manager.create_arc("root", goal="root")
        child_id = arc_manager.add_child(root_id, "child")
        grandchild_id = arc_manager.add_child(child_id, "grandchild")

        arc_manager.increment_ancestor_tokens(grandchild_id, 2000)

        root = arc_manager.get_arc(root_id)
        child = arc_manager.get_arc(child_id)

        assert root["descendant_tokens"] == 2000
        assert child["descendant_tokens"] == 2000

    def test_zero_tokens_no_op(self):
        parent_id = arc_manager.create_arc("parent", goal="parent")
        child_id = arc_manager.add_child(parent_id, "child")

        arc_manager.increment_ancestor_tokens(child_id, 0)

        parent = arc_manager.get_arc(parent_id)
        assert parent["descendant_tokens"] == 0

    def test_negative_tokens_no_op(self):
        parent_id = arc_manager.create_arc("parent", goal="parent")
        child_id = arc_manager.add_child(parent_id, "child")

        arc_manager.increment_ancestor_tokens(child_id, -100)

        parent = arc_manager.get_arc(parent_id)
        assert parent["descendant_tokens"] == 0


class TestUpdateArcCounters:
    """Full recount via update_arc_counters."""

    def test_recount_arc_count(self):
        root_id = arc_manager.create_arc("root", goal="root")
        arc_manager.add_child(root_id, "c1")
        arc_manager.add_child(root_id, "c2")

        # Manually reset counters to simulate corruption
        db = get_db()
        try:
            db.execute(
                "UPDATE arcs SET descendant_arc_count = 0 WHERE id = ?",
                (root_id,),
            )
            db.commit()
        finally:
            db.close()

        # Recount should restore correct values
        arc_manager.update_arc_counters(root_id)
        root = arc_manager.get_arc(root_id)
        assert root["descendant_arc_count"] == 2

    def test_recount_executions(self):
        root_id = arc_manager.create_arc("root", goal="root")
        child_id = arc_manager.add_child(root_id, "child")

        # Insert a code file and execution linked to the child arc
        db = get_db()
        try:
            cursor = db.execute(
                "INSERT INTO code_files (file_path, source, arc_id) VALUES (?, ?, ?)",
                ("/tmp/test.py", "test", child_id),
            )
            cf_id = cursor.lastrowid
            db.execute(
                "INSERT INTO code_executions (code_file_id, execution_status) VALUES (?, ?)",
                (cf_id, "success"),
            )
            db.commit()
        finally:
            db.close()

        arc_manager.update_arc_counters(root_id)
        root = arc_manager.get_arc(root_id)
        assert root["descendant_executions"] == 1


class TestCountersInGetArcDetail:
    """Performance counters should be visible in get_arc_detail output."""

    def test_counters_shown_when_nonzero(self):
        from carpenter.chat_tool_loader import get_handler

        parent_id = arc_manager.create_arc("parent", goal="parent")
        arc_manager.add_child(parent_id, "child-1")
        arc_manager.add_child(parent_id, "child-2")

        # Also add some token usage
        child3_id = arc_manager.add_child(parent_id, "child-3")
        arc_manager.increment_ancestor_tokens(child3_id, 5000)
        arc_manager.increment_ancestor_executions(child3_id)

        result = get_handler("get_arc_detail")({"arc_id": parent_id})
        assert "Counters:" in result
        assert "tokens=5000" in result
        assert "executions=1" in result
        assert "child_arcs=3" in result

    def test_counters_hidden_when_zero(self):
        from carpenter.chat_tool_loader import get_handler

        arc_id = arc_manager.create_arc("leaf", goal="no children")
        result = get_handler("get_arc_detail")({"arc_id": arc_id})
        assert "Counters:" not in result


class TestCountersInPlanFields:
    """Counters should be included in plan field responses."""

    def test_plan_fields_include_counters(self):
        from carpenter.tool_backends.arc import handle_get_plan

        parent_id = arc_manager.create_arc("parent", goal="parent")
        arc_manager.add_child(parent_id, "child")

        result = handle_get_plan({"arc_id": parent_id})
        arc = result["arc"]
        assert "descendant_arc_count" in arc
        assert arc["descendant_arc_count"] == 1
        assert "descendant_tokens" in arc
        assert "descendant_executions" in arc
