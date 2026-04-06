"""Tests for KB tool backend: add and delete operations."""

import os

from carpenter.kb.store import KBStore
from carpenter.tool_backends.kb import handle_add, handle_delete
from carpenter.db import get_db
from carpenter import kb as kb_module


def _init_store(tmp_path):
    """Create a KBStore, set as singleton."""
    kb_dir = str(tmp_path / "kb")
    os.makedirs(kb_dir, exist_ok=True)
    store = KBStore(kb_dir=kb_dir)
    kb_module._store = store
    return store


class TestHandleAdd:
    def test_add_creates_entry(self, tmp_path):
        store = _init_store(tmp_path)
        result = handle_add({
            "path": "new/entry",
            "content": "# New Entry\n\nFresh content.",
            "description": "A brand new entry",
        })
        assert "error" not in result
        assert "Wrote" in result["status"]

        entry = store.get_entry("new/entry")
        assert entry is not None
        assert entry["title"] == "New Entry"

    def test_add_creates_file(self, tmp_path):
        store = _init_store(tmp_path)
        handle_add({
            "path": "new/leaf",
            "content": "# Leaf\n\nContent here.",
            "description": "A leaf",
        })
        assert os.path.isfile(os.path.join(store.kb_dir, "new", "leaf.md"))

    def test_add_indexes_in_fts(self, tmp_path):
        store = _init_store(tmp_path)
        handle_add({
            "path": "fts/test",
            "content": "# FTS Test\n\nSupercalifragilistic content.",
            "description": "fts test",
        })
        results = store.search("Supercalifragilistic")
        assert len(results) >= 1
        assert results[0]["path"] == "fts/test"

    def test_add_queues_change(self, tmp_path):
        _init_store(tmp_path)
        handle_add({
            "path": "queued/entry",
            "content": "# Queued\n\nFor the queue.",
            "description": "queue test",
        })
        db = get_db()
        try:
            rows = db.execute(
                "SELECT * FROM kb_change_queue WHERE file_path = 'queued/entry'"
            ).fetchall()
            assert len(rows) >= 1
            assert rows[0]["change_type"] == "added"
        finally:
            db.close()

    def test_add_extracts_links(self, tmp_path):
        store = _init_store(tmp_path)
        # Create link targets first so validation passes
        store.write_entry(path="target/a", content="# A\n\nTarget A.", description="a")
        store.write_entry(path="target/b", content="# B\n\nTarget B.", description="b")
        handle_add({
            "path": "linked/entry",
            "content": "# Linked\n\nSee [[target/a]] and [[target/b]].",
            "description": "links",
        })
        db = get_db()
        try:
            links = db.execute(
                "SELECT target_path FROM kb_links WHERE source_path = 'linked/entry' "
                "ORDER BY target_path"
            ).fetchall()
            targets = [r["target_path"] for r in links]
            assert targets == ["target/a", "target/b"]
        finally:
            db.close()


class TestHandleAddValidation:
    def test_rejects_empty_path(self, tmp_path):
        _init_store(tmp_path)
        result = handle_add({"path": "", "content": "x", "description": "d"})
        assert "error" in result

    def test_rejects_traversal(self, tmp_path):
        _init_store(tmp_path)
        result = handle_add({"path": "../escape", "content": "x", "description": "d"})
        assert "error" in result

    def test_rejects_empty_content(self, tmp_path):
        _init_store(tmp_path)
        result = handle_add({"path": "valid/path", "content": "", "description": "d"})
        assert "error" in result

    def test_autogenerates_missing_description(self, tmp_path):
        _init_store(tmp_path)
        result = handle_add({"path": "valid/path", "content": "# X\n\nY."})
        assert "error" not in result
        assert "Wrote" in result["status"]

    def test_rejects_duplicate(self, tmp_path):
        store = _init_store(tmp_path)
        store.write_entry(
            path="existing", content="# Existing\n\nAlready here.",
            description="exists",
        )
        result = handle_add({
            "path": "existing",
            "content": "# Dupe\n\nShould fail.",
            "description": "dupe",
        })
        assert "error" in result
        assert "already exists" in result["error"]


class TestHandleDelete:
    def test_delete_removes_entry(self, tmp_path):
        store = _init_store(tmp_path)
        store.write_entry(
            path="to-delete", content="# Delete Me\n\nBye.",
            description="deletable",
        )
        result = handle_delete({"path": "to-delete"})
        assert "error" not in result
        assert "Deleted" in result["status"]

        # File gone
        assert not os.path.isfile(os.path.join(store.kb_dir, "to-delete.md"))

        # DB entry gone
        db = get_db()
        try:
            row = db.execute(
                "SELECT * FROM kb_entries WHERE path = 'to-delete'"
            ).fetchone()
            assert row is None
        finally:
            db.close()

    def test_delete_removes_from_fts(self, tmp_path):
        store = _init_store(tmp_path)
        store.write_entry(
            path="fts-delete", content="# FTS Delete\n\nUniquewordftsdelete.",
            description="fts",
        )
        # Verify searchable first
        assert len(store.search("Uniquewordftsdelete")) >= 1

        handle_delete({"path": "fts-delete"})
        assert len(store.search("Uniquewordftsdelete")) == 0

    def test_delete_queues_change(self, tmp_path):
        store = _init_store(tmp_path)
        store.write_entry(
            path="q-delete", content="# Q\n\nQueued for delete.",
            description="q",
        )
        handle_delete({"path": "q-delete"})
        db = get_db()
        try:
            rows = db.execute(
                "SELECT * FROM kb_change_queue WHERE file_path = 'q-delete'"
            ).fetchall()
            assert len(rows) >= 1
            assert rows[0]["change_type"] == "deleted"
        finally:
            db.close()

    def test_delete_rejects_auto_generated(self, tmp_path):
        store = _init_store(tmp_path)
        store.write_entry(
            path="auto-gen", content="# Auto\n\nGenerated.",
            description="auto",
        )
        # Set auto_source in DB
        db = get_db()
        try:
            db.execute(
                "UPDATE kb_entries SET auto_source = 'source.py' WHERE path = 'auto-gen'"
            )
            db.commit()
        finally:
            db.close()

        result = handle_delete({"path": "auto-gen"})
        assert "error" in result


class TestHandleDeleteValidation:
    def test_rejects_empty_path(self, tmp_path):
        _init_store(tmp_path)
        result = handle_delete({"path": ""})
        assert "error" in result

    def test_rejects_traversal(self, tmp_path):
        _init_store(tmp_path)
        result = handle_delete({"path": "../escape"})
        assert "error" in result
