"""Tests for the arc self-escalation tool."""

import json
from unittest.mock import patch

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.agent.invocation import _execute_chat_tool


def _create_arc(name, parent_id=None, **kwargs):
    return arc_manager.create_arc(name=name, parent_id=parent_id, **kwargs)


class TestSelfEscalation:

    def test_escalate_creates_stronger_sibling(self):
        """New arc has next model."""
        parent = _create_arc("parent")
        config_id = arc_manager.get_or_create_agent_config(model="anthropic:claude-haiku-4.5")
        child = arc_manager.add_child(parent, "worker", agent_config_id=config_id)
        arc_manager.update_status(child, "active")

        with patch("carpenter.agent.model_resolver.get_next_model", return_value="anthropic:claude-sonnet-4.5"):
            result = _execute_chat_tool("escalate", {}, executor_arc_id=child)

        assert "Escalated to anthropic:claude-sonnet-4.5" in result
        assert "Arc #" in result

    def test_escalate_freezes_original(self):
        """Original status is 'escalated'."""
        config_id = arc_manager.get_or_create_agent_config(model="anthropic:claude-haiku-4.5")
        arc_id = _create_arc("worker", agent_config_id=config_id)
        arc_manager.update_status(arc_id, "active")

        with patch("carpenter.agent.model_resolver.get_next_model", return_value="anthropic:claude-sonnet-4.5"):
            _execute_chat_tool("escalate", {}, executor_arc_id=arc_id)

        arc = arc_manager.get_arc(arc_id)
        assert arc["status"] == "escalated"

    def test_escalate_grants_subtree_read(self):
        """Grant exists from new arc to original."""
        config_id = arc_manager.get_or_create_agent_config(model="anthropic:claude-haiku-4.5")
        arc_id = _create_arc("worker", agent_config_id=config_id)
        arc_manager.update_status(arc_id, "active")

        with patch("carpenter.agent.model_resolver.get_next_model", return_value="anthropic:claude-sonnet-4.5"):
            result = _execute_chat_tool("escalate", {}, executor_arc_id=arc_id)

        # Extract new arc ID from result
        import re
        match = re.search(r"Arc #(\d+)", result)
        assert match, f"Could not find new arc ID in result: {result}"
        new_arc_id = int(match.group(1))

        # Check grant exists
        assert arc_manager.has_read_grant(new_arc_id, arc_id)

    def test_escalate_enhanced_goal(self):
        """Goal contains escalation context."""
        parent = _create_arc("parent")
        config_id = arc_manager.get_or_create_agent_config(model="anthropic:claude-haiku-4.5")
        child = arc_manager.add_child(parent, "worker", goal="Fix the login bug", agent_config_id=config_id)
        arc_manager.update_status(child, "active")
        # Add a sub-child so child summary is populated
        grandchild = arc_manager.add_child(child, "sub-task", goal="Write tests")

        with patch("carpenter.agent.model_resolver.get_next_model", return_value="anthropic:claude-sonnet-4.5"):
            result = _execute_chat_tool("escalate", {}, executor_arc_id=child)

        import re
        match = re.search(r"Arc #(\d+)", result)
        new_arc_id = int(match.group(1))
        new_arc = arc_manager.get_arc(new_arc_id)

        assert "Escalation Context" in new_arc["goal"]
        assert f"Arc #{child}" in new_arc["goal"]
        assert "sub-task" in new_arc["goal"]

    def test_escalate_new_arc_is_planner(self):
        """agent_type is PLANNER."""
        config_id = arc_manager.get_or_create_agent_config(model="anthropic:claude-haiku-4.5")
        arc_id = _create_arc("worker", agent_config_id=config_id)
        arc_manager.update_status(arc_id, "active")

        with patch("carpenter.agent.model_resolver.get_next_model", return_value="anthropic:claude-sonnet-4.5"):
            result = _execute_chat_tool("escalate", {}, executor_arc_id=arc_id)

        import re
        match = re.search(r"Arc #(\d+)", result)
        new_arc_id = int(match.group(1))
        new_arc = arc_manager.get_arc(new_arc_id)
        assert new_arc["agent_type"] == "PLANNER"

    def test_escalate_at_top_tier_returns_error(self):
        """'Already at highest' message."""
        config_id = arc_manager.get_or_create_agent_config(model="anthropic:claude-opus-4.5")
        arc_id = _create_arc("worker", agent_config_id=config_id)
        arc_manager.update_status(arc_id, "active")

        with patch("carpenter.agent.model_resolver.get_next_model", return_value=None):
            result = _execute_chat_tool("escalate", {}, executor_arc_id=arc_id)

        assert "highest" in result.lower()

    def test_escalate_from_chat_returns_error(self):
        """Error when no executor_arc_id."""
        result = _execute_chat_tool("escalate", {}, executor_arc_id=None)
        assert "Error" in result
        assert "arc execution context" in result

    def test_escalate_frozen_arc_returns_error(self):
        """Error for already-frozen arcs."""
        config_id = arc_manager.get_or_create_agent_config(model="anthropic:claude-haiku-4.5")
        arc_id = _create_arc("worker", agent_config_id=config_id)
        arc_manager.update_status(arc_id, "active")
        arc_manager.update_status(arc_id, "completed")

        result = _execute_chat_tool("escalate", {}, executor_arc_id=arc_id)
        assert "Error" in result
        assert "frozen" in result.lower()
