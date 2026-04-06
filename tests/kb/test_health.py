"""Tests for Phase 7: KB health metrics."""

import os

import pytest

import carpenter.config
from carpenter.kb.health import graph_metrics
from carpenter.kb.store import KBStore
from carpenter.db import get_db


def _make_entry(kb_dir, path, content):
    """Write a KB entry file."""
    if path == "_root":
        full = os.path.join(kb_dir, "_root.md")
    elif path.endswith("/"):
        # Folder index
        full = os.path.join(kb_dir, path, "_index.md")
    else:
        full = os.path.join(kb_dir, path + ".md")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)


def _make_store(kb_dir):
    """Create a KBStore and sync from filesystem."""
    store = KBStore(kb_dir=kb_dir)
    store.sync_from_filesystem()
    return store


class TestGraphMetrics:
    """7A: graph_metrics() computes correct metrics."""

    def test_graph_metrics_empty_kb(self, tmp_path):
        """Empty KB returns zeros."""
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = _make_store(kb_dir)

        metrics = graph_metrics(store)
        assert metrics["total_entries"] == 0
        assert metrics["total_links"] == 0
        assert metrics["avg_links_per_entry"] == 0.0
        assert metrics["orphan_entries"] == []
        assert metrics["broken_links"] == []

    def test_graph_metrics_counts(self, tmp_path):
        """Entries + links counted correctly."""
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        _make_entry(kb_dir, "_root", "# Root\n\n[[topic-a]] [[topic-b]]")
        _make_entry(kb_dir, "topic-a", "# Topic A\n\nSee [[topic-b]]")
        _make_entry(kb_dir, "topic-b", "# Topic B\n\nContent")

        store = _make_store(kb_dir)
        metrics = graph_metrics(store)
        assert metrics["total_entries"] == 3
        assert metrics["total_links"] >= 2  # _root -> a, _root -> b, a -> b

    def test_orphan_detection(self, tmp_path):
        """Unlinked entry detected as orphan."""
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        _make_entry(kb_dir, "_root", "# Root\n\nNo links here.")
        _make_entry(kb_dir, "orphan", "# Orphan\n\nNo links either.")

        store = _make_store(kb_dir)
        metrics = graph_metrics(store)
        assert "orphan" in metrics["orphan_entries"]

    def test_broken_link_detection(self, tmp_path):
        """Link to nonexistent path detected."""
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        _make_entry(kb_dir, "_root", "# Root\n\n[[nonexistent]]")

        store = _make_store(kb_dir)
        metrics = graph_metrics(store)
        assert len(metrics["broken_links"]) > 0
        assert any("nonexistent" in bl for bl in metrics["broken_links"])

    def test_stale_entry_detection(self, tmp_path):
        """Old last_accessed flagged as stale."""
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        _make_entry(kb_dir, "_root", "# Root\n\nContent")

        store = _make_store(kb_dir)

        # Manually set last_accessed to 60 days ago
        db = get_db()
        try:
            db.execute(
                "UPDATE kb_entries SET last_accessed = datetime('now', '-60 days') "
                "WHERE path = '_root'"
            )
            db.commit()
        finally:
            db.close()

        metrics = graph_metrics(store)
        assert "_root" in metrics["stale_entries"]

    def test_oversized_detection(self, monkeypatch, tmp_path):
        """Large entry flagged as oversized."""
        current = dict(carpenter.config.CONFIG)
        current["kb"] = dict(current.get("kb", {}))
        current["kb"]["max_entry_bytes"] = 100  # Very low threshold
        monkeypatch.setattr("carpenter.config.CONFIG", current)

        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        _make_entry(kb_dir, "_root", "# Root\n\n" + "x" * 200)

        store = _make_store(kb_dir)
        metrics = graph_metrics(store)
        assert "_root" in metrics["oversized_entries"]

    def test_unreachable_detection(self, tmp_path):
        """Entry not reachable from root via BFS."""
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        _make_entry(kb_dir, "_root", "# Root\n\n[[linked]]")
        _make_entry(kb_dir, "linked", "# Linked\n\nContent")
        _make_entry(kb_dir, "island", "# Island\n\nNot reachable from root.")

        store = _make_store(kb_dir)
        metrics = graph_metrics(store)
        assert "island" in metrics["unreachable_entries"]
        assert "linked" not in metrics["unreachable_entries"]
