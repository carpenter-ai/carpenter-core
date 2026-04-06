"""Tests for the file operations tool backend."""
import os

from carpenter.tool_backends.files import handle_read, handle_write, handle_list


def test_read_file(tmp_path):
    path = str(tmp_path / "hello.txt")
    with open(path, "w") as f:
        f.write("hello world")
    result = handle_read({"path": path})
    assert result["content"] == "hello world"


def test_write_file(tmp_path):
    path = str(tmp_path / "output.txt")
    handle_write({"path": path, "content": "written by backend"})
    with open(path, "r") as f:
        assert f.read() == "written by backend"


def test_write_creates_dirs(tmp_path):
    path = str(tmp_path / "nested" / "deep" / "file.txt")
    handle_write({"path": path, "content": "deep content"})
    assert os.path.exists(path)
    with open(path, "r") as f:
        assert f.read() == "deep content"


def test_list_dir(tmp_path):
    listing_dir = tmp_path / "listing"
    listing_dir.mkdir()
    (listing_dir / "a.txt").write_text("a")
    (listing_dir / "b.txt").write_text("b")
    (listing_dir / "c.txt").write_text("c")
    result = handle_list({"dir": str(listing_dir)})
    assert sorted(result["files"]) == ["a.txt", "b.txt", "c.txt"]
