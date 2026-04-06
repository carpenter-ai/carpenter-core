"""Tests for KB link validation — write guard + tool backend error propagation."""

import os

from carpenter.kb.store import KBStore
from carpenter.tool_backends.kb import handle_add, handle_edit
from carpenter import kb as kb_module


def _init_store(tmp_path):
    """Create a KBStore, set as singleton."""
    kb_dir = str(tmp_path / "kb")
    os.makedirs(kb_dir, exist_ok=True)
    store = KBStore(kb_dir=kb_dir)
    kb_module._store = store
    return store


class TestWriteEntryLinkValidation:
    def test_valid_links_accepted(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        # Create targets first
        store.write_entry(path="target-a", content="# A\n\nTarget A.", description="a")
        store.write_entry(path="target-b", content="# B\n\nTarget B.", description="b")
        # Now write entry with valid links
        result = store.write_entry(
            path="source",
            content="# Source\n\nSee [[target-a]] and [[target-b]].",
            description="source entry",
        )
        assert result.startswith("Wrote")

    def test_broken_links_rejected(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        result = store.write_entry(
            path="source",
            content="# Source\n\nSee [[nonexistent]].",
            description="source entry",
        )
        assert result.startswith("Error")
        assert "nonexistent" in result

    def test_broken_links_no_file_written(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        store.write_entry(
            path="no-write",
            content="# No Write\n\nSee [[missing]].",
            description="should not be written",
        )
        assert not os.path.isfile(os.path.join(kb_dir, "no-write.md"))

    def test_validate_links_false_bypasses_check(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        result = store.write_entry(
            path="bypassed",
            content="# Bypassed\n\nSee [[nonexistent]].",
            description="bypassed validation",
            validate_links=False,
        )
        assert result.startswith("Wrote")
        assert os.path.isfile(os.path.join(kb_dir, "bypassed.md"))

    def test_no_links_passes_trivially(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        result = store.write_entry(
            path="no-links",
            content="# No Links\n\nPlain content without any links.",
            description="no links",
        )
        assert result.startswith("Wrote")

    def test_mixed_valid_and_invalid_lists_only_invalid(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        # Create one valid target
        store.write_entry(path="exists", content="# Exists\n\nI exist.", description="e")
        # Try to write with one valid and one invalid link
        result = store.write_entry(
            path="mixed",
            content="# Mixed\n\nSee [[exists]] and [[missing]].",
            description="mixed links",
        )
        assert result.startswith("Error")
        assert "missing" in result
        assert "exists" not in result

    def test_entry_exists_method(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        store.write_entry(path="real", content="# Real\n\nExists.", description="r")
        assert store.entry_exists("real") is True
        assert store.entry_exists("fake") is False


class TestToolBackendErrorPropagation:
    def test_handle_add_returns_error_on_broken_links(self, tmp_path):
        _init_store(tmp_path)
        result = handle_add({
            "path": "new/entry",
            "content": "# New\n\nSee [[nonexistent]].",
            "description": "broken link entry",
        })
        assert "error" in result
        assert "nonexistent" in result["error"]

    def test_handle_add_accepts_valid_links(self, tmp_path):
        store = _init_store(tmp_path)
        store.write_entry(path="target", content="# Target\n\nHere.", description="t")
        result = handle_add({
            "path": "new/entry",
            "content": "# New\n\nSee [[target]].",
            "description": "valid link entry",
        })
        assert "error" not in result
        assert "Wrote" in result["status"]

    def test_handle_edit_returns_error_on_broken_links(self, tmp_path):
        store = _init_store(tmp_path)
        store.write_entry(
            path="editable",
            content="# Editable\n\nOriginal.",
            description="editable",
        )
        result = handle_edit({
            "path": "editable",
            "content": "# Editable\n\nNow links to [[ghost]].",
            "description": "updated",
        })
        assert "error" in result
        assert "ghost" in result["error"]

    def test_handle_edit_accepts_valid_links(self, tmp_path):
        store = _init_store(tmp_path)
        store.write_entry(
            path="editable",
            content="# Editable\n\nOriginal.",
            description="editable",
        )
        store.write_entry(
            path="valid-target",
            content="# Valid\n\nI exist.",
            description="valid",
        )
        result = handle_edit({
            "path": "editable",
            "content": "# Editable\n\nNow links to [[valid-target]].",
            "description": "updated",
        })
        assert "error" not in result
        assert "Wrote" in result["status"]

    def test_handle_add_no_change_queue_on_error(self, tmp_path):
        _init_store(tmp_path)
        from carpenter.db import get_db
        handle_add({
            "path": "queued/bad",
            "content": "# Bad\n\nSee [[no-such-thing]].",
            "description": "should not queue",
        })
        db = get_db()
        try:
            rows = db.execute(
                "SELECT * FROM kb_change_queue WHERE file_path = 'queued/bad'"
            ).fetchall()
            assert len(rows) == 0
        finally:
            db.close()
