"""Tests for carpenter.kb.store — KBStore CRUD, sync, and link management."""

import os
from pathlib import Path

from carpenter.kb.store import KBStore
from carpenter.db import get_db


def _make_entry(kb_dir, path, content):
    """Helper to write a KB entry file."""
    if "/" in path:
        os.makedirs(os.path.join(kb_dir, os.path.dirname(path)), exist_ok=True)
    full = os.path.join(kb_dir, path + ".md")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)


class TestKBStoreGetEntry:
    def test_get_leaf_entry(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        _make_entry(kb_dir, "test-entry", "# Test\n\nA test entry.\n\n## Details\nMore info.")
        store = KBStore(kb_dir=kb_dir)
        entry = store.get_entry("test-entry")
        assert entry is not None
        assert entry["title"] == "Test"
        assert entry["description"] == "A test entry."
        assert "More info" in entry["content"]

    def test_get_folder_entry(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(os.path.join(kb_dir, "topic"), exist_ok=True)
        with open(os.path.join(kb_dir, "topic", "_index.md"), "w") as f:
            f.write("# Topic\n\nTopic overview.")
        store = KBStore(kb_dir=kb_dir)
        entry = store.get_entry("topic")
        assert entry is not None
        assert entry["title"] == "Topic"

    def test_get_root(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        with open(os.path.join(kb_dir, "_root.md"), "w") as f:
            f.write("# Root\n\nThe root index.")
        store = KBStore(kb_dir=kb_dir)
        entry = store.get_entry("")
        assert entry is not None
        assert entry["title"] == "Root"

    def test_get_nonexistent(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        assert store.get_entry("nonexistent") is None


class TestKBStoreListChildren:
    def test_list_children(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(os.path.join(kb_dir, "topic"), exist_ok=True)
        with open(os.path.join(kb_dir, "topic", "_index.md"), "w") as f:
            f.write("# Topic\n\nOverview.")
        _make_entry(kb_dir, "topic/tools", "# Tools\n\nTool list.")
        _make_entry(kb_dir, "topic/config", "# Config\n\nConfig info.")
        store = KBStore(kb_dir=kb_dir)
        children = store.list_children("topic")
        assert len(children) == 2
        names = {c["name"] for c in children}
        assert "tools" in names
        assert "config" in names

    def test_list_root_children(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(os.path.join(kb_dir, "scheduling"), exist_ok=True)
        with open(os.path.join(kb_dir, "scheduling", "_index.md"), "w") as f:
            f.write("# Scheduling\n\nTime-based triggers.")
        _make_entry(kb_dir, "standalone", "# Standalone\n\nA standalone entry.")
        store = KBStore(kb_dir=kb_dir)
        children = store.list_children("")
        assert len(children) == 2
        paths = {c["path"] for c in children}
        assert "scheduling" in paths
        assert "standalone" in paths


class TestKBStoreWriteEntry:
    def test_write_new_entry(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        result = store.write_entry(
            path="test/new",
            content="# New Entry\n\nDescription here.",
            description="A new entry",
        )
        assert "Wrote" in result
        # Verify file exists
        assert os.path.isfile(os.path.join(kb_dir, "test", "new.md"))
        # Verify DB entry
        db = get_db()
        try:
            row = db.execute("SELECT * FROM kb_entries WHERE path = 'test/new'").fetchone()
            assert row is not None
            assert row["title"] == "New Entry"
        finally:
            db.close()

    def test_write_updates_links(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        store.write_entry(
            path="a",
            content="# A\n\nLinks to [[b]] and [[c]].",
            description="Entry A",
            validate_links=False,
        )
        db = get_db()
        try:
            links = db.execute(
                "SELECT target_path FROM kb_links WHERE source_path = 'a' ORDER BY target_path"
            ).fetchall()
            targets = [r["target_path"] for r in links]
            assert targets == ["b", "c"]
        finally:
            db.close()


class TestKBStoreDeleteEntry:
    def test_delete_entry(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        store.write_entry(path="to-delete", content="# Delete Me\n\nGoing away.", description="x")
        result = store.delete_entry("to-delete")
        assert "Deleted" in result
        assert not os.path.isfile(os.path.join(kb_dir, "to-delete.md"))

    def test_delete_auto_generated_fails(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        store.write_entry(path="auto", content="# Auto\n\nGenerated.", description="x")
        # Set auto_source
        db = get_db()
        try:
            db.execute("UPDATE kb_entries SET auto_source = 'some_file.py' WHERE path = 'auto'")
            db.commit()
        finally:
            db.close()
        result = store.delete_entry("auto")
        assert "Error" in result


class TestKBStoreSyncFromFilesystem:
    def test_sync_indexes_entries(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        _make_entry(kb_dir, "entry1", "# Entry 1\n\nFirst entry.")
        _make_entry(kb_dir, "entry2", "# Entry 2\n\nSecond entry with [[entry1]].")
        store = KBStore(kb_dir=kb_dir)
        result = store.sync_from_filesystem()
        assert result["added"] == 2
        # Verify links were extracted
        db = get_db()
        try:
            links = db.execute("SELECT * FROM kb_links").fetchall()
            assert len(links) == 1
            assert links[0]["source_path"] == "entry2"
            assert links[0]["target_path"] == "entry1"
        finally:
            db.close()


class TestKBStoreAccessLogging:
    def test_log_access(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        store.write_entry(path="test", content="# Test\n\nHello.", description="x")
        store.log_access("test", conversation_id=42)
        db = get_db()
        try:
            rows = db.execute("SELECT * FROM kb_access_log WHERE path = 'test'").fetchall()
            assert len(rows) == 1
            assert rows[0]["conversation_id"] == 42
            # Check access_count was incremented
            entry = db.execute("SELECT access_count FROM kb_entries WHERE path = 'test'").fetchone()
            assert entry["access_count"] == 1
        finally:
            db.close()


class TestKBStoreSearch:
    def test_search_finds_entry(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        store.write_entry(
            path="scheduling/tools",
            content="# Scheduling Tools\n\nTime-based triggers for cron.",
            description="Cron and one-shot trigger tools",
        )
        store.write_entry(
            path="messaging/tools",
            content="# Messaging Tools\n\nSend messages.",
            description="Send and receive messages",
        )
        results = store.search("cron trigger")
        assert len(results) >= 1
        assert results[0]["path"] == "scheduling/tools"

    def test_search_empty_query(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        results = store.search("")
        assert results == []


class TestKBStoreInboundLinks:
    def test_inbound_links(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        store.write_entry(path="target", content="# Target\n\nI am the target.", description="t")
        store.write_entry(path="source1", content="# S1\n\nLinks to [[target]].", description="s1")
        store.write_entry(path="source2", content="# S2\n\nAlso [[target]].", description="s2")
        inbound = store.get_inbound_links("target")
        assert len(inbound) == 2
        sources = {r["source_path"] for r in inbound}
        assert sources == {"source1", "source2"}


class TestSearchPathPrefix:
    """Tests for KB search path_prefix filtering (moved from test_search_path_prefix.py)."""

    def test_prefix_filters_results(self):
        from carpenter.kb import get_store
        store = get_store()
        store.write_entry("conversations/1-hello", "# Hello\n\nConversation about greetings.", "greeting conv")
        store.write_entry("reflections/daily/2025-01-01", "# Daily Reflection\n\nReflected on greetings.", "daily refl")
        store.write_entry("work/1-greet-task", "# Greet Task\n\nWork on greetings.", "greet work")

        results = store.search("greetings", path_prefix="conversations/")
        paths = [r["path"] for r in results]
        assert "conversations/1-hello" in paths
        assert all(p.startswith("conversations/") for p in paths)

    def test_no_prefix_returns_all(self):
        from carpenter.kb import get_store
        store = get_store()
        store.write_entry("conversations/2-testing", "# Testing\n\nConversation about testing.", "test conv")
        store.write_entry("reflections/daily/2025-01-02", "# Daily Reflection\n\nReflected on testing.", "daily refl")

        results = store.search("testing")
        paths = [r["path"] for r in results]
        assert len(paths) >= 2

    def test_empty_results_with_prefix(self):
        from carpenter.kb import get_store
        store = get_store()
        store.write_entry("conversations/3-code", "# Code\n\nConversation about code.", "code conv")

        results = store.search("code", path_prefix="reflections/")
        assert results == []
