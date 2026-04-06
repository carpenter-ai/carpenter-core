"""Tests for the built-in coding agent."""

import os
import textwrap
import pytest
from unittest.mock import patch, MagicMock

from carpenter.agent import coding_agent


class TestValidatePath:
    @pytest.mark.parametrize("path,should_succeed,description", [
        ("file.txt", True, "valid relative path"),
        ("sub/dir/file.py", True, "nested relative path"),
        ("../../../etc/passwd", False, "path traversal blocked"),
        ("/etc/passwd", False, "absolute path blocked"),
    ])
    def test_path_validation(self, tmp_path, path, should_succeed, description):
        """Test path validation with various inputs."""
        ws = str(tmp_path / "workspace")
        os.makedirs(ws, exist_ok=True)

        if should_succeed:
            result = coding_agent._validate_path(ws, path)
            assert result.startswith(ws), f"Failed: {description}"
        else:
            with pytest.raises(ValueError, match="escapes workspace"):
                coding_agent._validate_path(ws, path)


class TestToolExecution:
    def test_read_file(self, tmp_path):
        """read_file reads file content."""
        (tmp_path / "test.txt").write_text("hello world")
        result = coding_agent._exec_read_file(str(tmp_path), {"path": "test.txt"})
        assert result == "hello world"

    def test_read_file_not_found(self, tmp_path):
        """read_file returns error for missing file."""
        result = coding_agent._exec_read_file(str(tmp_path), {"path": "missing.txt"})
        assert "Error" in result

    def test_write_file(self, tmp_path):
        """write_file creates a file."""
        result = coding_agent._exec_write_file(
            str(tmp_path), {"path": "new.txt", "content": "content"}
        )
        assert "Written" in result
        assert (tmp_path / "new.txt").read_text() == "content"

    def test_write_file_creates_dirs(self, tmp_path):
        """write_file creates parent directories."""
        coding_agent._exec_write_file(
            str(tmp_path), {"path": "sub/dir/file.txt", "content": "nested"}
        )
        assert (tmp_path / "sub" / "dir" / "file.txt").read_text() == "nested"

    def test_edit_file(self, tmp_path):
        """edit_file performs find-and-replace."""
        (tmp_path / "code.py").write_text("x = 1\ny = 2\n")
        result = coding_agent._exec_edit_file(
            str(tmp_path),
            {"path": "code.py", "old_text": "x = 1", "new_text": "x = 42"},
        )
        assert "Edited" in result
        assert (tmp_path / "code.py").read_text() == "x = 42\ny = 2\n"

    def test_edit_file_not_found(self, tmp_path):
        """edit_file returns error for missing file."""
        result = coding_agent._exec_edit_file(
            str(tmp_path),
            {"path": "missing.py", "old_text": "a", "new_text": "b"},
        )
        assert "Error" in result

    def test_edit_file_old_text_not_found(self, tmp_path):
        """edit_file returns error when old_text not in file."""
        (tmp_path / "code.py").write_text("x = 1\n")
        result = coding_agent._exec_edit_file(
            str(tmp_path),
            {"path": "code.py", "old_text": "NOT HERE", "new_text": "b"},
        )
        assert "not found" in result

    def test_edit_file_multiple_matches(self, tmp_path):
        """edit_file returns error when old_text appears multiple times."""
        (tmp_path / "code.py").write_text("x = 1\nx = 1\n")
        result = coding_agent._exec_edit_file(
            str(tmp_path),
            {"path": "code.py", "old_text": "x = 1", "new_text": "x = 2"},
        )
        assert "2 times" in result

    def test_path_traversal_in_tools(self, tmp_path):
        """Tools block path traversal attempts."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        result = coding_agent._execute_tool(
            str(ws), "read_file", {"path": "../../etc/passwd"}
        )
        assert "Error" in result


class TestListFiles:
    """Tests for the list_files tool (replacement for bash)."""

    def test_list_files_root(self, tmp_path):
        """list_files lists directory contents."""
        (tmp_path / "file1.txt").write_text("a")
        (tmp_path / "file2.py").write_text("b")
        (tmp_path / "subdir").mkdir()
        result = coding_agent._exec_list_files(str(tmp_path), {"path": "."})
        assert "file1.txt" in result
        assert "file2.py" in result
        assert "subdir/" in result

    def test_list_files_subdirectory(self, tmp_path):
        """list_files lists contents of a subdirectory."""
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "main.py").write_text("print('hello')")
        (sub / "utils.py").write_text("pass")
        result = coding_agent._exec_list_files(str(tmp_path), {"path": "src"})
        assert "main.py" in result
        assert "utils.py" in result

    def test_list_files_default_path(self, tmp_path):
        """list_files defaults to root when no path given."""
        (tmp_path / "readme.md").write_text("# README")
        result = coding_agent._exec_list_files(str(tmp_path), {})
        assert "readme.md" in result

    def test_list_files_empty_directory(self, tmp_path):
        """list_files returns message for empty directory."""
        empty = tmp_path / "empty"
        empty.mkdir()
        result = coding_agent._exec_list_files(str(tmp_path), {"path": "empty"})
        assert "empty directory" in result

    def test_list_files_not_a_directory(self, tmp_path):
        """list_files returns error for file path."""
        (tmp_path / "file.txt").write_text("data")
        result = coding_agent._exec_list_files(str(tmp_path), {"path": "file.txt"})
        assert "Error" in result
        assert "Not a directory" in result

    def test_list_files_path_traversal(self, tmp_path):
        """list_files blocks path traversal."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        result = coding_agent._exec_list_files(str(ws), {"path": "../../etc"})
        assert "Error" in result

    def test_list_files_directories_have_slash(self, tmp_path):
        """list_files marks directories with trailing slash."""
        (tmp_path / "dir1").mkdir()
        (tmp_path / "file1.txt").write_text("data")
        result = coding_agent._exec_list_files(str(tmp_path), {"path": "."})
        lines = result.strip().split("\n")
        dir_entries = [l for l in lines if l.endswith("/")]
        file_entries = [l for l in lines if not l.endswith("/")]
        assert "dir1/" in dir_entries
        assert "file1.txt" in file_entries

    def test_list_files_via_execute_tool(self, tmp_path):
        """list_files dispatches correctly through _execute_tool."""
        (tmp_path / "test.txt").write_text("data")
        result = coding_agent._execute_tool(
            str(tmp_path), "list_files", {"path": "."}
        )
        assert "test.txt" in result


class TestNoBashAccess:
    """Tests verifying that bash/shell access has been removed."""

    def test_no_bash_in_fallback_tools(self):
        """Fallback tool definitions do not include bash."""
        names = [t["name"] for t in coding_agent._FALLBACK_TOOL_DEFINITIONS]
        assert "bash" not in names

    def test_no_bash_in_tool_handlers(self):
        """Tool handlers dict does not include bash."""
        assert "bash" not in coding_agent._TOOL_HANDLERS

    def test_bash_tool_returns_unknown(self, tmp_path):
        """Attempting to use bash tool returns unknown tool error."""
        result = coding_agent._execute_tool(
            str(tmp_path), "bash", {"command": "echo hello"}
        )
        assert "Unknown tool" in result

    def test_no_subprocess_import(self):
        """coding_agent module does not import subprocess."""
        import carpenter.agent.coding_agent as mod
        import inspect
        source = inspect.getsource(mod)
        # Check that subprocess is not imported at the module level
        assert "import subprocess" not in source


class TestExecuteTool:
    def test_unknown_tool(self, tmp_path):
        """Unknown tool returns error."""
        result = coding_agent._execute_tool(str(tmp_path), "unknown_tool", {})
        assert "Unknown tool" in result

    def test_dispatches_correctly(self, tmp_path):
        """Tools dispatch to correct handlers."""
        (tmp_path / "file.txt").write_text("data")
        result = coding_agent._execute_tool(
            str(tmp_path), "read_file", {"path": "file.txt"}
        )
        assert result == "data"


class TestRun:
    def test_end_turn_no_tools(self, tmp_path):
        """Agent that returns text without writing completes after nudge attempts.

        The coding agent nudges the model up to MAX_NUDGES times when it
        responds with text but has not written any files.  With MAX_NUDGES=2
        this means 3 total iterations (2 nudges + 1 final break).
        """
        ws = str(tmp_path)
        mock_response = {
            "content": [{"type": "text", "text": "Done, no changes needed."}],
            "stop_reason": "end_turn",
        }

        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_response
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}

        with patch("httpx.post", return_value=mock_resp):
            result = coding_agent.run(
                ws, "Do nothing",
                {"type": "builtin", "model": "test-model", "max_tokens": 100, "max_iterations": 5},
            )

        assert result["exit_code"] == 0
        # 2 nudges + 1 final = 3 iterations
        assert result["iterations"] == 3
        assert "no changes" in result["stdout"].lower()

    def test_tool_use_loop(self, tmp_path):
        """Agent that uses a write tool then returns text."""
        ws = str(tmp_path)

        # First response: tool_use (write_file so files_modified is set)
        tool_response = {
            "content": [
                {"type": "tool_use", "id": "tool_1", "name": "write_file",
                 "input": {"path": "output.txt", "content": "hello"}}
            ],
            "stop_reason": "tool_use",
        }
        # Second response: end_turn
        final_response = {
            "content": [{"type": "text", "text": "I wrote the file."}],
            "stop_reason": "end_turn",
        }

        mock_resp_1 = MagicMock()
        mock_resp_1.json.return_value = tool_response
        mock_resp_1.raise_for_status = MagicMock()
        mock_resp_1.headers = {}

        mock_resp_2 = MagicMock()
        mock_resp_2.json.return_value = final_response
        mock_resp_2.raise_for_status = MagicMock()
        mock_resp_2.headers = {}

        with patch("httpx.post", side_effect=[mock_resp_1, mock_resp_2]):
            result = coding_agent.run(
                ws, "Write output.txt",
                {"type": "builtin", "model": "test-model", "max_tokens": 100, "max_iterations": 10},
            )

        assert result["exit_code"] == 0
        assert result["iterations"] == 2

    def test_max_iterations_reached(self, tmp_path):
        """Agent stops after max_iterations."""
        ws = str(tmp_path)
        (tmp_path / "file.txt").write_text("data")

        tool_response = {
            "content": [
                {"type": "tool_use", "id": "tool_1", "name": "read_file",
                 "input": {"path": "file.txt"}}
            ],
            "stop_reason": "tool_use",
        }

        mock_resp = MagicMock()
        mock_resp.json.return_value = tool_response
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=mock_resp):
            result = coding_agent.run(
                ws, "Loop forever",
                {"type": "builtin", "model": "test-model", "max_tokens": 100, "max_iterations": 3},
            )

        assert result["iterations"] == 3
        assert result["exit_code"] == 0

    def test_api_error(self, tmp_path):
        """API error returns exit_code 1."""
        ws = str(tmp_path)

        with patch("carpenter.agent.invocation._call_with_retries", return_value=None), \
             patch("carpenter.agent.invocation._get_client"):
            result = coding_agent.run(
                ws, "test",
                {"type": "builtin", "model": "test-model", "max_tokens": 100, "max_iterations": 5},
            )

        assert result["exit_code"] == 1
        assert "retries exhausted" in result["stdout"].lower()

    def test_uses_profile_system_prompt(self, tmp_path):
        """run() uses system_prompt from profile when provided."""
        ws = str(tmp_path)
        custom_prompt = "You are a custom coding agent."
        end_response = {
            "content": [{"type": "text", "text": "Done."}],
            "stop_reason": "end_turn",
        }

        with patch("carpenter.agent.invocation._call_with_retries",
                    return_value=end_response) as mock_call, \
             patch("carpenter.agent.invocation._get_client"):
            coding_agent.run(
                ws, "Do nothing",
                {"type": "builtin", "model": "test-model", "max_tokens": 100,
                 "max_iterations": 1, "system_prompt": custom_prompt},
            )

        # First positional arg to _call_with_retries is the system prompt
        assert mock_call.call_args[0][0] == custom_prompt

    def test_falls_back_to_default_system_prompt(self, tmp_path):
        """run() uses loaded system prompt when profile has no system_prompt."""
        ws = str(tmp_path)
        end_response = {
            "content": [{"type": "text", "text": "Done."}],
            "stop_reason": "end_turn",
        }

        with patch("carpenter.agent.invocation._call_with_retries",
                    return_value=end_response) as mock_call, \
             patch("carpenter.agent.invocation._get_client"):
            coding_agent.run(
                ws, "Do nothing",
                {"type": "builtin", "model": "test-model", "max_tokens": 100,
                 "max_iterations": 1},
            )

        # The prompt should come from either template files or the fallback.
        # Both contain this key phrase.
        used_prompt = mock_call.call_args[0][0]
        assert "You are a coding agent" in used_prompt
        assert "read_file" in used_prompt

    def test_loads_prompt_from_templates(self, tmp_path):
        """run() loads system prompt from coding prompt templates."""
        ws = str(tmp_path)
        end_response = {
            "content": [{"type": "text", "text": "Done."}],
            "stop_reason": "end_turn",
        }

        with patch("carpenter.agent.invocation._call_with_retries",
                    return_value=end_response) as mock_call, \
             patch("carpenter.agent.invocation._get_client"), \
             patch("carpenter.agent.coding_agent._load_system_prompt",
                   return_value="Custom template prompt"):
            coding_agent.run(
                ws, "Do nothing",
                {"type": "builtin", "model": "test-model", "max_tokens": 100,
                 "max_iterations": 1},
            )

        assert mock_call.call_args[0][0] == "Custom template prompt"

    def test_loads_tools_from_templates(self, tmp_path):
        """run() loads tool definitions from coding tool templates."""
        ws = str(tmp_path)
        custom_tools = [{"name": "custom_tool", "description": "test",
                         "input_schema": {"type": "object", "properties": {}, "required": []}}]
        end_response = {
            "content": [{"type": "text", "text": "Done."}],
            "stop_reason": "end_turn",
        }

        with patch("carpenter.agent.invocation._call_with_retries",
                    return_value=end_response) as mock_call, \
             patch("carpenter.agent.invocation._get_client"), \
             patch("carpenter.agent.coding_agent._load_tool_definitions",
                   return_value=custom_tools):
            coding_agent.run(
                ws, "Do nothing",
                {"type": "builtin", "model": "test-model", "max_tokens": 100,
                 "max_iterations": 1},
            )

        # Check that the custom tools were passed to _call_with_retries
        call_kwargs = mock_call.call_args
        assert call_kwargs[1]["tools"] == custom_tools


class TestToolDefinitionLoading:
    """Tests for YAML-based tool definition loading."""

    def test_load_from_yaml_file(self, tmp_path):
        """_load_tool_definitions loads tools from user config YAML file."""
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        yaml_content = textwrap.dedent("""\
            tools:
              - name: read_file
                description: Read a file.
                input_schema:
                  type: object
                  properties:
                    path:
                      type: string
                      description: Path to read.
                  required:
                    - path
              - name: list_files
                description: List directory contents.
                input_schema:
                  type: object
                  properties:
                    path:
                      type: string
                      description: Directory to list.
                  required: []
        """)
        (config_dir / "00-tools.yaml").write_text(yaml_content)

        with patch.dict(coding_agent.config.CONFIG,
                        {"coding_tools_dir": str(config_dir)}):
            tools = coding_agent._load_tool_definitions()
            assert len(tools) == 2
            assert tools[0]["name"] == "read_file"
            assert tools[1]["name"] == "list_files"
            assert "input_schema" in tools[0]

    def test_fallback_when_no_yaml(self, tmp_path):
        """_load_tool_definitions falls back when YAML file is missing."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        with patch.dict(coding_agent.config.CONFIG,
                        {"coding_tools_dir": str(empty_dir)}):
            tools = coding_agent._load_tool_definitions()
            assert tools == coding_agent._FALLBACK_TOOL_DEFINITIONS

    def test_fallback_when_yaml_import_fails(self):
        """_load_tool_definitions falls back when PyYAML is unavailable."""
        with patch.dict("sys.modules", {"yaml": None}):
            import importlib
            # Can't easily unload yaml, so test the ImportError branch
            # by patching the import
            original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

            def mock_import(name, *args, **kwargs):
                if name == "yaml":
                    raise ImportError("no yaml")
                return original_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=mock_import):
                tools = coding_agent._load_tool_definitions()
                assert tools is coding_agent._FALLBACK_TOOL_DEFINITIONS

    def test_fallback_definitions_have_five_tools(self):
        """Fallback definitions contain all five expected tools (no bash)."""
        names = [t["name"] for t in coding_agent._FALLBACK_TOOL_DEFINITIONS]
        assert names == ["read_file", "write_file", "edit_file", "delete_file", "list_files"]

    def test_yaml_file_in_config_seed(self):
        """The YAML tool definitions file exists in config_seed/coding-tools/."""
        config_seed_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "config_seed", "coding-tools",
        )
        yaml_path = os.path.join(config_seed_dir, "00-tools.yaml")
        assert os.path.isfile(yaml_path), f"Expected {yaml_path} to exist"

    def test_yaml_definitions_match_fallback(self):
        """YAML definitions match the fallback definitions."""
        config_seed_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "config_seed", "coding-tools",
        )
        yaml_path = os.path.join(config_seed_dir, "00-tools.yaml")

        import yaml
        with open(yaml_path) as f:
            data = yaml.safe_load(f.read())

        yaml_tools = data["tools"]
        fallback = coding_agent._FALLBACK_TOOL_DEFINITIONS

        assert len(yaml_tools) == len(fallback)
        for yt, ft in zip(yaml_tools, fallback):
            assert yt["name"] == ft["name"]
            assert yt["description"] == ft["description"]
            assert yt["input_schema"] == ft["input_schema"]

    def test_load_tool_definitions_returns_consistent_results(self):
        """_load_tool_definitions returns consistent results across calls."""
        result1 = coding_agent._load_tool_definitions()
        result2 = coding_agent._load_tool_definitions()
        assert result1 == result2
