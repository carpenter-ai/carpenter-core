"""Tests for KB tool backend: edit operations."""

import os

from carpenter.kb.store import KBStore
from carpenter.tool_backends.kb import handle_edit
from carpenter.db import get_db
from carpenter import kb as kb_module


def _init_store(tmp_path):
    """Create a KBStore with one entry, set as singleton."""
    kb_dir = str(tmp_path / "kb")
    os.makedirs(kb_dir, exist_ok=True)
    store = KBStore(kb_dir=kb_dir)
    store.write_entry(
        path="test/other",
        content="# Other\n\nOther content.",
        description="Another entry",
    )
    store.write_entry(
        path="test/entry",
        content="# Test Entry\n\nOriginal content.\n\n## Related\n[[test/other]]",
        description="A test entry",
    )
    # Set as singleton so handle_edit() can find it
    kb_module._store = store
    return store


class TestHandleEdit:
    def test_edit_updates_content(self, tmp_path):
        store = _init_store(tmp_path)
        result = handle_edit({
            "path": "test/entry",
            "content": "# Test Entry\n\nUpdated content.",
            "description": "Updated description",
        })
        assert "error" not in result
        assert "Wrote" in result["status"]

        # Verify file updated
        entry = store.get_entry("test/entry")
        assert "Updated content" in entry["content"]

    def test_edit_updates_links(self, tmp_path):
        store = _init_store(tmp_path)
        # Create the link target first
        store.write_entry(
            path="test/new",
            content="# New\n\nNew content.",
            description="new entry",
        )
        # Original links to [[test/other]]; new content links to [[test/new]]
        handle_edit({
            "path": "test/entry",
            "content": "# Test Entry\n\nNow links to [[test/new]].",
            "description": "Updated",
        })
        db = get_db()
        try:
            links = db.execute(
                "SELECT target_path FROM kb_links WHERE source_path = 'test/entry'"
            ).fetchall()
            targets = {r["target_path"] for r in links}
            assert "test/new" in targets
            assert "test/other" not in targets
        finally:
            db.close()

    def test_edit_queues_change(self, tmp_path):
        _init_store(tmp_path)
        handle_edit({
            "path": "test/entry",
            "content": "# Test Entry\n\nQueued.",
            "description": "q",
        })
        db = get_db()
        try:
            rows = db.execute(
                "SELECT * FROM kb_change_queue WHERE file_path = 'test/entry'"
            ).fetchall()
            assert len(rows) >= 1
            assert rows[0]["change_type"] == "modified"
        finally:
            db.close()

    def test_edit_reindexes_fts(self, tmp_path):
        store = _init_store(tmp_path)
        handle_edit({
            "path": "test/entry",
            "content": "# Test Entry\n\nUniquexyzword in FTS.",
            "description": "fts test",
        })
        results = store.search("Uniquexyzword")
        assert len(results) >= 1
        assert results[0]["path"] == "test/entry"


class TestHandleEditValidation:
    def test_rejects_empty_path(self, tmp_path):
        _init_store(tmp_path)
        result = handle_edit({"path": "", "content": "x"})
        assert "error" in result

    def test_rejects_traversal(self, tmp_path):
        _init_store(tmp_path)
        result = handle_edit({"path": "../escape", "content": "x"})
        assert "error" in result

    def test_rejects_absolute_path(self, tmp_path):
        _init_store(tmp_path)
        result = handle_edit({"path": "/etc/passwd", "content": "x"})
        assert "error" in result

    def test_rejects_empty_content(self, tmp_path):
        _init_store(tmp_path)
        result = handle_edit({"path": "test/entry", "content": ""})
        assert "error" in result

    def test_rejects_nonexistent_entry(self, tmp_path):
        _init_store(tmp_path)
        result = handle_edit({"path": "does/not/exist", "content": "# X\n\nY."})
        assert "error" in result
        assert "not found" in result["error"]
