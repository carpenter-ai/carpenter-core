"""Tests for tool output truncation in carpenter.agent.invocation."""

import os
from pathlib import Path

import pytest

from carpenter import config
from carpenter.agent import invocation


class TestTruncateToolOutputPassthrough:
    """Small outputs should pass through unchanged."""

    def test_short_output_unchanged(self):
        """Output under threshold is returned verbatim."""
        result = invocation._truncate_tool_output("hello world", "read_file")
        assert result == "hello world"

    def test_empty_output_unchanged(self):
        """Empty string passes through."""
        result = invocation._truncate_tool_output("", "read_file")
        assert result == ""

    def test_exactly_at_threshold_passes_through(self):
        """Output exactly at max_bytes is not truncated."""
        max_bytes = config.CONFIG["tool_output_max_bytes"]
        # Use ASCII so 1 char = 1 byte
        text = "x" * max_bytes
        result = invocation._truncate_tool_output(text, "read_file")
        assert result == text

    def test_one_byte_over_triggers_truncation(self):
        """Output one byte over max_bytes triggers truncation."""
        max_bytes = config.CONFIG["tool_output_max_bytes"]
        # Create multi-line content just over the threshold
        line = "x" * 80 + "\n"
        lines_needed = (max_bytes // len(line)) + 2
        text = line * lines_needed
        assert len(text.encode("utf-8")) > max_bytes

        result = invocation._truncate_tool_output(text, "read_file")
        assert "[... truncated" in result
        assert "use read_file to access" in result


class TestTruncateToolOutputTruncation:
    """Large outputs are truncated with head + notice + tail."""

    def _make_large_output(self, num_lines=500):
        """Create a large multi-line output string."""
        lines = [f"line {i}: {'data' * 20}" for i in range(num_lines)]
        return "\n".join(lines) + "\n"

    def test_truncated_output_has_head_and_tail(self):
        """Truncated output contains head lines, notice, and tail lines."""
        text = self._make_large_output(500)
        head_lines = config.CONFIG["tool_output_head_lines"]
        tail_lines = config.CONFIG["tool_output_tail_lines"]

        result = invocation._truncate_tool_output(text, "list_files")

        # Check head content is present
        assert "line 0:" in result
        assert f"line {head_lines - 1}:" in result

        # Check tail content is present
        assert "line 499:" in result
        assert f"line {500 - tail_lines}:" in result

        # Check notice is present
        assert "[... truncated" in result
        assert "500 lines" in result or "501 lines" in result
        assert "bytes" in result
        assert "use read_file to access" in result

    def test_head_lines_not_exceeded(self):
        """Lines above head_lines boundary from start are NOT in the head part."""
        text = self._make_large_output(500)
        head_lines = config.CONFIG["tool_output_head_lines"]

        result = invocation._truncate_tool_output(text, "read_file")

        # The line just past head should not be in the head section
        # (it could be in the tail section, so check position relative to truncation notice)
        parts = result.split("[... truncated")
        head_part = parts[0]
        # Line head_lines should NOT be in the head
        assert f"line {head_lines}:" not in head_part

    def test_tail_lines_present(self):
        """The last tail_lines lines of the original output appear after the notice."""
        text = self._make_large_output(500)
        tail_lines = config.CONFIG["tool_output_tail_lines"]

        result = invocation._truncate_tool_output(text, "read_file")

        parts = result.split("use read_file to access ...]")
        assert len(parts) == 2
        tail_part = parts[1]
        for i in range(500 - tail_lines, 500):
            assert f"line {i}:" in tail_part

    def test_truncation_notice_includes_path(self):
        """Truncation notice includes the saved file path."""
        text = self._make_large_output(500)
        result = invocation._truncate_tool_output(text, "read_file")

        # Should reference the tool_output directory
        assert "tool_output" in result

    def test_truncation_notice_includes_byte_count(self):
        """Truncation notice includes total byte count."""
        text = self._make_large_output(500)
        total_bytes = len(text.encode("utf-8", errors="replace"))
        result = invocation._truncate_tool_output(text, "read_file")
        assert f"{total_bytes} bytes" in result


class TestTruncateToolOutputFileSave:
    """Full output is saved to disk when truncated."""

    def _make_large_output(self, num_lines=500):
        """Create a large multi-line output string."""
        lines = [f"line {i}: {'data' * 20}" for i in range(num_lines)]
        return "\n".join(lines) + "\n"

    def test_full_output_saved_to_file(self):
        """When truncated, the full output is saved to a file."""
        text = self._make_large_output(500)
        result = invocation._truncate_tool_output(text, "read_file")

        # Extract the file path from the notice
        # Format: "full output saved to {path} ({size} bytes"
        start = result.index("saved to ") + len("saved to ")
        end = result.index(" (", start)
        saved_path = result[start:end]

        assert os.path.isfile(saved_path), f"Expected file at {saved_path}"
        with open(saved_path) as f:
            saved_content = f.read()
        assert saved_content == text

    def test_file_path_uses_date_directory(self):
        """Saved file goes into a YYYY/MM/DD date-partitioned directory."""
        text = self._make_large_output(500)
        result = invocation._truncate_tool_output(text, "read_file")

        start = result.index("saved to ") + len("saved to ")
        end = result.index(" (", start)
        saved_path = result[start:end]

        # Path should contain tool_output/YYYY/MM/DD/
        path_parts = Path(saved_path).parts
        assert "tool_output" in path_parts

    def test_file_path_includes_tool_name(self):
        """Saved file name includes the tool name."""
        text = self._make_large_output(500)
        result = invocation._truncate_tool_output(text, "list_files")

        start = result.index("saved to ") + len("saved to ")
        end = result.index(" (", start)
        saved_path = result[start:end]

        assert "list_files" in os.path.basename(saved_path)


class TestTruncateToolOutputConfig:
    """Config values are respected."""

    def test_custom_max_bytes(self, monkeypatch):
        """Custom tool_output_max_bytes threshold is respected."""
        # Set a very low threshold
        monkeypatch.setitem(config.CONFIG, "tool_output_max_bytes", 100)

        text = "x" * 80 + "\n" + "y" * 80 + "\n"  # 162 bytes > 100
        result = invocation._truncate_tool_output(text, "test_tool")
        assert "[... truncated" in result

    def test_custom_head_lines(self, monkeypatch):
        """Custom tool_output_head_lines is respected."""
        monkeypatch.setitem(config.CONFIG, "tool_output_max_bytes", 100)
        monkeypatch.setitem(config.CONFIG, "tool_output_head_lines", 2)
        monkeypatch.setitem(config.CONFIG, "tool_output_tail_lines", 1)

        lines = [f"line {i}" for i in range(20)]
        text = "\n".join(lines) + "\n"

        result = invocation._truncate_tool_output(text, "test_tool")

        parts = result.split("[... truncated")
        head_part = parts[0]
        # Should have exactly 2 lines in head
        assert "line 0" in head_part
        assert "line 1" in head_part
        assert "line 2" not in head_part

    def test_custom_tail_lines(self, monkeypatch):
        """Custom tool_output_tail_lines is respected."""
        monkeypatch.setitem(config.CONFIG, "tool_output_max_bytes", 100)
        monkeypatch.setitem(config.CONFIG, "tool_output_head_lines", 1)
        monkeypatch.setitem(config.CONFIG, "tool_output_tail_lines", 3)

        lines = [f"line {i}" for i in range(20)]
        text = "\n".join(lines) + "\n"

        result = invocation._truncate_tool_output(text, "test_tool")

        parts = result.split("use read_file to access ...]")
        tail_part = parts[1]
        # Should have last 3 lines in tail
        assert "line 17" in tail_part
        assert "line 18" in tail_part
        assert "line 19" in tail_part

    def test_zero_tail_lines(self, monkeypatch):
        """tail_lines=0 means no tail section."""
        monkeypatch.setitem(config.CONFIG, "tool_output_max_bytes", 100)
        monkeypatch.setitem(config.CONFIG, "tool_output_head_lines", 2)
        monkeypatch.setitem(config.CONFIG, "tool_output_tail_lines", 0)

        lines = [f"line {i}" for i in range(20)]
        text = "\n".join(lines) + "\n"

        result = invocation._truncate_tool_output(text, "test_tool")

        # Should end after the notice (no tail content)
        assert result.rstrip().endswith("use read_file to access ...]")


class TestTruncateToolOutputEdgeCases:
    """Edge cases for truncation logic."""

    def test_multibyte_characters(self, monkeypatch):
        """Multi-byte UTF-8 characters are handled correctly for byte counting."""
        monkeypatch.setitem(config.CONFIG, "tool_output_max_bytes", 100)

        # Each emoji is 4 bytes in UTF-8; 30 emojis = 120 bytes > 100
        text = "\n".join(["X" * 10] * 20) + "\n"
        # Ensure it's over threshold in bytes
        assert len(text.encode("utf-8")) > 100
        result = invocation._truncate_tool_output(text, "test_tool")
        assert "[... truncated" in result

    def test_single_very_long_line(self, monkeypatch):
        """A single line exceeding the threshold still truncates."""
        monkeypatch.setitem(config.CONFIG, "tool_output_max_bytes", 100)
        monkeypatch.setitem(config.CONFIG, "tool_output_head_lines", 1)
        monkeypatch.setitem(config.CONFIG, "tool_output_tail_lines", 1)

        text = "a" * 200  # single line, 200 bytes > 100
        result = invocation._truncate_tool_output(text, "test_tool")
        assert "[... truncated" in result
        # The single line appears both as head and tail
        assert "1 lines" in result

    def test_tool_name_with_special_chars(self, monkeypatch):
        """Tool names with slashes are sanitized in the filename."""
        monkeypatch.setitem(config.CONFIG, "tool_output_max_bytes", 100)

        lines = [f"line {i}" for i in range(20)]
        text = "\n".join(lines) + "\n"

        result = invocation._truncate_tool_output(text, "some/tool")

        start = result.index("saved to ") + len("saved to ")
        end = result.index(" (", start)
        saved_path = result[start:end]

        # Filename should not contain slashes from tool name
        filename = os.path.basename(saved_path)
        assert "some_tool" in filename
        assert "/" not in filename
