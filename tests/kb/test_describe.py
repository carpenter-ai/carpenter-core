"""Tests for KB describe behavior (folder vs leaf, access logging, footer)."""

import os

from carpenter.kb.store import KBStore
from carpenter.db import get_db


def _setup_kb(tmp_path):
    """Create a KB with test entries."""
    kb_dir = str(tmp_path / "kb")
    os.makedirs(os.path.join(kb_dir, "topic"), exist_ok=True)
    with open(os.path.join(kb_dir, "_root.md"), "w") as f:
        f.write("# Root\n\nThe root index.\n\n## Topics\n- [[topic]]")
    with open(os.path.join(kb_dir, "topic", "_index.md"), "w") as f:
        f.write("# Topic\n\nA test topic.")
    with open(os.path.join(kb_dir, "topic", "tools.md"), "w") as f:
        f.write("# Topic Tools\n\nTool descriptions.\n\n## Related\n[[topic]]")
    with open(os.path.join(kb_dir, "topic", "config.md"), "w") as f:
        f.write("# Topic Config\n\nConfig info.\n\n## Related\n[[topic/tools]]")

    store = KBStore(kb_dir=kb_dir)
    store.sync_from_filesystem()
    return store


class TestDescribeRoot:
    def test_root_returns_content(self, tmp_path):
        store = _setup_kb(tmp_path)
        entry = store.get_entry("")
        assert entry is not None
        assert "Root" in entry["title"]
        assert "[[topic]]" in entry["content"]


class TestDescribeFolder:
    def test_folder_has_children(self, tmp_path):
        store = _setup_kb(tmp_path)
        children = store.list_children("topic")
        assert len(children) == 2
        names = {c["name"] for c in children}
        assert "tools" in names
        assert "config" in names


class TestDescribeLeaf:
    def test_leaf_returns_content(self, tmp_path):
        store = _setup_kb(tmp_path)
        entry = store.get_entry("topic/tools")
        assert entry is not None
        assert entry["title"] == "Topic Tools"
        assert "Tool descriptions" in entry["content"]


class TestInboundLinks:
    def test_inbound_links_found(self, tmp_path):
        store = _setup_kb(tmp_path)
        inbound = store.get_inbound_links("topic")
        assert len(inbound) >= 1
        sources = {r["source_path"] for r in inbound}
        # _root and topic/tools both link to topic
        assert "_root" in sources or "topic/tools" in sources

    def test_inbound_links_for_tools(self, tmp_path):
        store = _setup_kb(tmp_path)
        inbound = store.get_inbound_links("topic/tools")
        assert len(inbound) >= 1
        sources = {r["source_path"] for r in inbound}
        assert "topic/config" in sources


class TestAccessLogging:
    def test_access_logged(self, tmp_path):
        store = _setup_kb(tmp_path)
        store.log_access("topic/tools", conversation_id=1)
        db = get_db()
        try:
            rows = db.execute(
                "SELECT * FROM kb_access_log WHERE path = 'topic/tools'"
            ).fetchall()
            assert len(rows) == 1
            entry = db.execute(
                "SELECT access_count FROM kb_entries WHERE path = 'topic/tools'"
            ).fetchone()
            assert entry["access_count"] == 1
        finally:
            db.close()
