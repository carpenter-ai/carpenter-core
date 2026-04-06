"""Tests for file_count functionality in files tool backend."""
import os
import tempfile
from pathlib import Path

from carpenter.tool_backends import files


def test_handle_file_count_basic():
    """Test basic file counting functionality."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create some files
        Path(tmpdir, "file1.txt").write_text("content1")
        Path(tmpdir, "file2.txt").write_text("content2")
        Path(tmpdir, "file3.py").write_text("print('hello')")

        result = files.handle_file_count({"directory": tmpdir})
        assert isinstance(result, dict)
        assert "file_count" in result
        assert result["file_count"] == 3
        assert "error" not in result


def test_handle_file_count_empty_directory():
    """Test file counting in an empty directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = files.handle_file_count({"directory": tmpdir})
        assert result["file_count"] == 0
        assert "error" not in result


def test_handle_file_count_with_subdirectories():
    """Test file counting excludes subdirectories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create files and subdirectories
        Path(tmpdir, "file1.txt").write_text("content1")
        Path(tmpdir, "file2.txt").write_text("content2")
        Path(tmpdir, "subdir1").mkdir()
        Path(tmpdir, "subdir2").mkdir()
        Path(tmpdir, "subdir1", "nested_file.txt").write_text("nested")

        result = files.handle_file_count({"directory": tmpdir})
        assert result["file_count"] == 2  # Only counts files, not subdirectories
        assert "error" not in result


def test_handle_file_count_nonexistent_directory():
    """Test file counting with non-existent directory."""
    nonexistent_path = "/path/that/does/not/exist"
    result = files.handle_file_count({"directory": nonexistent_path})
    assert result["file_count"] == 0
    assert "error" in result
    assert "does not exist" in result["error"]


def test_handle_file_count_not_a_directory():
    """Test file counting when path is not a directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a file (not a directory)
        file_path = Path(tmpdir, "not_a_directory.txt")
        file_path.write_text("content")

        result = files.handle_file_count({"directory": str(file_path)})
        assert result["file_count"] == 0
        assert "error" in result
        assert "not a directory" in result["error"]
