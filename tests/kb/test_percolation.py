"""Tests for Phase 7: KB percolation via queue_source_changes."""

import os

import pytest

import carpenter.config
import carpenter.kb
from carpenter.kb.autogen import queue_source_changes, _REPO_ROOT
from carpenter.kb.store import KBStore
from carpenter.db import get_db


class TestQueueSourceChanges:
    """7D: queue_source_changes queues KB updates for modified tool files."""

    def test_queue_source_changes_tool_file(self, tmp_path, monkeypatch):
        """Tool module change is queued."""
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)

        # queue_source_changes uses _collect_source_files() which references
        # the repo-root carpenter_tools. We pass the actual repo root as
        # source_dir so normpath matching works.
        source_dir = str(_REPO_ROOT)
        # Files relative to source_dir that map to known KB paths
        applied = ["carpenter_tools/act/scheduling.py"]

        # Patch the kb package's get_store to return our test store
        monkeypatch.setattr(carpenter.kb, "get_store", lambda **kw: store)

        count = queue_source_changes(applied, source_dir)
        assert count >= 1

        # Verify queued in DB
        db = get_db()
        try:
            rows = db.execute(
                "SELECT file_path FROM kb_change_queue WHERE processed_at IS NULL"
            ).fetchall()
        finally:
            db.close()
        assert len(rows) >= 1

    def test_queue_source_changes_config(self, tmp_path, monkeypatch):
        """config.py change is queued."""
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)

        source_dir = str(_REPO_ROOT)
        applied = ["carpenter/config.py"]

        monkeypatch.setattr(carpenter.kb, "get_store", lambda **kw: store)
        count = queue_source_changes(applied, source_dir)
        assert count >= 1

    def test_queue_source_changes_irrelevant(self, tmp_path, monkeypatch):
        """Non-tool file is not queued."""
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)

        source_dir = str(_REPO_ROOT)
        applied = ["README.md"]

        monkeypatch.setattr(carpenter.kb, "get_store", lambda **kw: store)
        count = queue_source_changes(applied, source_dir)
        assert count == 0

    def test_queue_source_changes_empty_list(self, tmp_path, monkeypatch):
        """Empty applied_files list returns 0 and queues nothing."""
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)

        source_dir = str(_REPO_ROOT)
        monkeypatch.setattr(carpenter.kb, "get_store", lambda **kw: store)
        count = queue_source_changes([], source_dir)
        assert count == 0

        # Verify nothing queued in DB
        db = get_db()
        try:
            rows = db.execute(
                "SELECT file_path FROM kb_change_queue WHERE processed_at IS NULL"
            ).fetchall()
        finally:
            db.close()
        assert len(rows) == 0

    def test_queue_source_changes_multiple_files(self, tmp_path, monkeypatch):
        """Multiple relevant files are each queued individually."""
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)

        source_dir = str(_REPO_ROOT)
        # Two tool files that should map to known KB paths
        applied = [
            "carpenter_tools/act/scheduling.py",
            "carpenter/config.py",
        ]

        monkeypatch.setattr(carpenter.kb, "get_store", lambda **kw: store)
        count = queue_source_changes(applied, source_dir)
        assert count >= 2

        # Verify queued in DB
        db = get_db()
        try:
            rows = db.execute(
                "SELECT file_path FROM kb_change_queue WHERE processed_at IS NULL"
            ).fetchall()
        finally:
            db.close()
        assert len(rows) >= 2

    def test_queue_source_changes_mixed_relevant_and_irrelevant(self, tmp_path, monkeypatch):
        """Mix of mapped and unmapped files — only mapped ones are queued."""
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)

        source_dir = str(_REPO_ROOT)
        applied = [
            "carpenter_tools/act/scheduling.py",  # mapped
            "README.md",                           # not mapped
            "some/random/file.txt",                # not mapped
        ]

        monkeypatch.setattr(carpenter.kb, "get_store", lambda **kw: store)
        count = queue_source_changes(applied, source_dir)
        assert count == 1

    def test_queue_source_changes_nonexistent_file(self, tmp_path, monkeypatch):
        """A file that doesn't exist on disk but isn't in source_map returns 0."""
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)

        source_dir = str(_REPO_ROOT)
        applied = ["totally/fake/nonexistent.py"]

        monkeypatch.setattr(carpenter.kb, "get_store", lambda **kw: store)
        count = queue_source_changes(applied, source_dir)
        assert count == 0
