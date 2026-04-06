"""Tests for connector retention cleanup."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from carpenter.channels.retention import (
    cleanup_old_tasks,
    _cleanup_orphaned_signals,
    create_retention_hook,
)


def _create_task_folder(shared: Path, task_id: str, age_days: int = 0):
    """Helper to create a task folder with a config.json."""
    task_dir = shared / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    created = datetime.utcnow() - timedelta(days=age_days)
    config = {"task_id": task_id, "created_at": created.isoformat() + "Z"}
    (task_dir / "config.json").write_text(json.dumps(config))
    return task_dir


class TestCleanupOldTasks:
    def test_removes_old_tasks(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "triggered").mkdir()
        (shared / "completed").mkdir()

        _create_task_folder(shared, "old-task", age_days=10)
        _create_task_folder(shared, "recent-task", age_days=1)

        removed = cleanup_old_tasks(shared, retention_days=7)
        assert removed == 1
        assert not (shared / "old-task").exists()
        assert (shared / "recent-task").exists()

    def test_skips_reserved_dirs(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "triggered").mkdir()
        (shared / "completed").mkdir()

        removed = cleanup_old_tasks(shared, retention_days=0)
        assert removed == 0
        assert (shared / "triggered").exists()

    def test_skips_folders_without_config(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "random-folder").mkdir()

        removed = cleanup_old_tasks(shared, retention_days=0)
        assert removed == 0

    def test_nonexistent_folder(self, tmp_path):
        removed = cleanup_old_tasks(tmp_path / "nonexistent")
        assert removed == 0


class TestCleanupOrphanedSignals:
    def test_removes_orphaned_trigger(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        triggered = shared / "triggered"
        triggered.mkdir()
        (triggered / "missing-task-abc123.trigger").touch()

        _cleanup_orphaned_signals(shared)
        assert not list(triggered.iterdir())

    def test_removes_orphaned_done(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        completed = shared / "completed"
        completed.mkdir()
        (completed / "missing-task.done").touch()

        _cleanup_orphaned_signals(shared)
        assert not list(completed.iterdir())

    def test_keeps_valid_signals(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        triggered = shared / "triggered"
        triggered.mkdir()
        completed = shared / "completed"
        completed.mkdir()

        # Create task folder and signal files
        (shared / "existing-task").mkdir()
        (triggered / "existing-task-abc.trigger").touch()
        (completed / "existing-task.done").touch()

        _cleanup_orphaned_signals(shared)
        assert len(list(triggered.iterdir())) == 1
        assert len(list(completed.iterdir())) == 1


class TestCreateRetentionHook:
    def test_returns_none_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr("carpenter.channels.retention.config.CONFIG", {
            "connectors": {},
            "plugin_shared_base": "",
        })
        hook = create_retention_hook()
        assert hook is None

    def test_returns_hook_with_connectors(self, monkeypatch, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        monkeypatch.setattr("carpenter.channels.retention.config.CONFIG", {
            "connectors": {"test": {"shared_folder": str(shared)}},
            "plugin_shared_base": "",
            "connector_retention_days": 7,
        })
        hook = create_retention_hook()
        assert hook is not None
        # Should be callable
        hook()

    def test_returns_hook_with_legacy_base(self, monkeypatch, tmp_path):
        monkeypatch.setattr("carpenter.channels.retention.config.CONFIG", {
            "connectors": {},
            "plugin_shared_base": str(tmp_path),
            "connector_retention_days": 7,
        })
        hook = create_retention_hook()
        assert hook is not None
