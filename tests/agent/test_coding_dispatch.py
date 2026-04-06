"""Tests for the coding agent dispatcher."""

import pytest
from unittest.mock import patch, MagicMock

from carpenter.agent import coding_dispatch


class TestGetProfile:
    def test_default_profile(self, monkeypatch):
        """Default profile is used when no name specified."""
        monkeypatch.setattr("carpenter.config.CONFIG", {
            "default_coding_agent": "builtin",
            "coding_agents": {
                "builtin": {"type": "builtin", "model": "test-model"},
            },
        })
        name, profile = coding_dispatch.get_profile()
        assert name == "builtin"
        assert profile["type"] == "builtin"

    def test_named_profile(self, monkeypatch):
        """Named profile is returned correctly."""
        monkeypatch.setattr("carpenter.config.CONFIG", {
            "default_coding_agent": "builtin",
            "coding_agents": {
                "builtin": {"type": "builtin"},
                "claude-code": {"type": "external", "command": "claude"},
            },
        })
        name, profile = coding_dispatch.get_profile("claude-code")
        assert name == "claude-code"
        assert profile["type"] == "external"

    def test_missing_profile(self, monkeypatch):
        """Raises ValueError for unknown profile name."""
        monkeypatch.setattr("carpenter.config.CONFIG", {
            "default_coding_agent": "builtin",
            "coding_agents": {
                "builtin": {"type": "builtin"},
            },
        })
        with pytest.raises(ValueError, match="not found"):
            coding_dispatch.get_profile("nonexistent")


class TestInvokeCodingAgent:
    def test_routes_to_builtin(self, monkeypatch):
        """Builtin type routes to coding_agent.run."""
        monkeypatch.setattr("carpenter.config.CONFIG", {
            "default_coding_agent": "builtin",
            "coding_agents": {
                "builtin": {"type": "builtin", "model": "test"},
            },
        })

        with patch("carpenter.agent.coding_agent.run") as mock_run:
            mock_run.return_value = {"stdout": "done", "exit_code": 0, "iterations": 1}
            result = coding_dispatch.invoke_coding_agent("/ws", "prompt")

        mock_run.assert_called_once_with(
            "/ws", "prompt", {"type": "builtin", "model": "test"}
        )
        assert result["exit_code"] == 0

    def test_routes_to_external(self, monkeypatch):
        """External type routes to external_coding_agent.run."""
        monkeypatch.setattr("carpenter.config.CONFIG", {
            "default_coding_agent": "ext",
            "coding_agents": {
                "ext": {"type": "external", "command": "test-cmd"},
            },
        })

        with patch("carpenter.agent.external_coding_agent.run") as mock_run:
            mock_run.return_value = {"stdout": "done", "stderr": "", "exit_code": 0}
            result = coding_dispatch.invoke_coding_agent("/ws", "prompt")

        mock_run.assert_called_once_with(
            "/ws", "prompt", {"type": "external", "command": "test-cmd"}
        )
        assert result["exit_code"] == 0

    def test_unknown_type_raises(self, monkeypatch):
        """Unknown agent type raises ValueError."""
        monkeypatch.setattr("carpenter.config.CONFIG", {
            "default_coding_agent": "bad",
            "coding_agents": {
                "bad": {"type": "unknown_type"},
            },
        })

        with pytest.raises(ValueError, match="Unknown coding agent type"):
            coding_dispatch.invoke_coding_agent("/ws", "prompt")

    def test_explicit_agent_name(self, monkeypatch):
        """Explicit agent_name overrides default."""
        monkeypatch.setattr("carpenter.config.CONFIG", {
            "default_coding_agent": "builtin",
            "coding_agents": {
                "builtin": {"type": "builtin"},
                "special": {"type": "builtin", "model": "special-model"},
            },
        })

        with patch("carpenter.agent.coding_agent.run") as mock_run:
            mock_run.return_value = {"stdout": "done", "exit_code": 0, "iterations": 1}
            coding_dispatch.invoke_coding_agent("/ws", "prompt", agent_name="special")

        _, _, profile = mock_run.call_args[0]
        assert profile["model"] == "special-model"
