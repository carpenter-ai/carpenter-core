"""Tests for coding tool definition install/load.

Chat tool tests are in test_chat_tool_registry.py (Python-defined tools).
This file only tests the YAML-based coding agent tool loader.
"""

import os
from pathlib import Path

from carpenter.tool_loader import (
    install_coding_tool_defaults,
    load_coding_tool_definitions,
)


class TestInstallCodingToolDefaults:
    def test_installs_to_new_dir(self, tmp_path):
        coding_dir = str(tmp_path / "fresh_coding_tools")
        result = install_coding_tool_defaults(coding_dir)
        assert result["status"] == "installed"
        assert result["copied"] > 0
        assert os.path.isdir(coding_dir)

    def test_skips_existing_dir(self, tmp_path):
        coding_dir = str(tmp_path / "empty_coding_tools")
        os.makedirs(coding_dir)
        result = install_coding_tool_defaults(coding_dir)
        assert result["status"] == "exists"
        assert result["copied"] == 0


class TestLoadCodingToolDefinitions:
    def test_loads_valid_yaml(self, tmp_path):
        coding_dir = str(tmp_path / "coding_tools")
        install_coding_tool_defaults(coding_dir)
        tools = load_coding_tool_definitions(coding_dir)
        assert tools is not None
        assert len(tools) > 0
        for t in tools:
            assert "name" in t
            assert "description" in t
            assert "input_schema" in t

    def test_returns_none_for_missing_dir(self, tmp_path):
        result = load_coding_tool_definitions(str(tmp_path / "nonexistent"))
        assert result is None
