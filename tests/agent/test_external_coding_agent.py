"""Tests for the external coding agent runner."""

import os
from unittest.mock import patch, MagicMock

import pytest

from carpenter.agent import external_coding_agent


class TestSubstitute:
    def test_simple_substitution(self):
        """Substitutes known variables."""
        result = external_coding_agent._substitute(
            "echo {workspace}", {"workspace": "/tmp/ws"}
        )
        assert result == "echo /tmp/ws"

    def test_multiple_variables(self):
        """Substitutes multiple variables."""
        result = external_coding_agent._substitute(
            "{workspace}/{prompt_file}",
            {"workspace": "/ws", "prompt_file": "prompt.txt"},
        )
        assert result == "/ws/prompt.txt"

    def test_unknown_placeholder_preserved(self):
        """Unknown placeholders are left as-is."""
        result = external_coding_agent._substitute(
            "echo {unknown}", {"workspace": "/ws"}
        )
        assert result == "echo {unknown}"


class TestRun:
    def test_successful_command(self, tmp_path):
        """Runs a command and captures output."""
        ws = str(tmp_path)
        profile = {
            "command": "echo 'hello from agent'",
            "timeout": 10,
            "env": {},
        }

        result = external_coding_agent.run(ws, "test prompt", profile)
        assert result["exit_code"] == 0
        assert "hello from agent" in result["stdout"]

    def test_prompt_file_written(self, tmp_path):
        """Prompt is written to a temp file in workspace."""
        ws = str(tmp_path)
        prompt_content = "Make the changes"

        # Use a command that reads the prompt file
        profile = {
            "command": "cat {prompt_file}",
            "timeout": 10,
            "env": {},
        }

        result = external_coding_agent.run(ws, prompt_content, profile)
        assert result["exit_code"] == 0
        assert "Make the changes" in result["stdout"]

    def test_prompt_file_cleaned_up(self, tmp_path):
        """Prompt file is removed after execution."""
        ws = str(tmp_path)
        profile = {
            "command": "true",
            "timeout": 10,
            "env": {},
        }

        external_coding_agent.run(ws, "test", profile)
        assert not os.path.exists(os.path.join(ws, ".tc_prompt.txt"))

    def test_workspace_substitution(self, tmp_path):
        """Workspace path is substituted in command."""
        ws = str(tmp_path)
        profile = {
            "command": "echo {workspace}",
            "timeout": 10,
            "env": {},
        }

        result = external_coding_agent.run(ws, "test", profile)
        assert ws in result["stdout"]

    def test_environment_variables(self, tmp_path):
        """Profile env vars are set for subprocess."""
        ws = str(tmp_path)
        profile = {
            "command": "echo $TEST_VAR",
            "timeout": 10,
            "env": {"TEST_VAR": "test_value"},
        }

        result = external_coding_agent.run(ws, "test", profile)
        assert "test_value" in result["stdout"]

    def test_env_variable_substitution(self, tmp_path, monkeypatch):
        """Config values are substituted in env vars."""
        monkeypatch.setattr(
            "carpenter.config.CONFIG",
            {"claude_api_key": "sk-test-key"},
        )
        ws = str(tmp_path)
        profile = {
            "command": "echo $MY_KEY",
            "timeout": 10,
            "env": {"MY_KEY": "{claude_api_key}"},
        }

        result = external_coding_agent.run(ws, "test", profile)
        assert "sk-test-key" in result["stdout"]

    def test_timeout_handling(self, tmp_path):
        """Timed-out command returns exit_code -1."""
        ws = str(tmp_path)
        profile = {
            "command": "sleep 2",  # Reduced from 60s
            "timeout": 0.1,  # Reduced from 1s
            "env": {},
        }

        result = external_coding_agent.run(ws, "test", profile)
        assert result["exit_code"] == -1
        assert "Timed out" in result["stderr"]

    def test_command_not_found(self, tmp_path):
        """Missing command returns exit_code -1."""
        ws = str(tmp_path)
        profile = {
            "command": "/nonexistent/binary",
            "timeout": 10,
            "env": {},
        }

        result = external_coding_agent.run(ws, "test", profile)
        # Shell will report error; exit code will be non-zero
        assert result["exit_code"] != 0

    def test_nonzero_exit_code(self, tmp_path):
        """Nonzero exit code is captured."""
        ws = str(tmp_path)
        profile = {
            "command": "exit 42",
            "timeout": 10,
            "env": {},
        }

        result = external_coding_agent.run(ws, "test", profile)
        assert result["exit_code"] == 42

    def test_stderr_captured(self, tmp_path):
        """Stderr output is captured."""
        ws = str(tmp_path)
        profile = {
            "command": "echo error_msg >&2",
            "timeout": 10,
            "env": {},
        }

        result = external_coding_agent.run(ws, "test", profile)
        assert "error_msg" in result["stderr"]
