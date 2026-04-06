"""Tests for the plugin retention policy."""

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from carpenter.channels.retention import cleanup_old_tasks, _cleanup_orphaned_signals


@pytest.fixture
def plugin_dir(tmp_path):
    """Create a mock plugin shared folder."""
    shared = tmp_path / "test-plugin"
    shared.mkdir()
    (shared / "triggered").mkdir()
    (shared / "completed").mkdir()
    return shared


def _create_task(plugin_dir, task_id, days_ago=0):
    """Create a mock task folder with config.json."""
    task_dir = plugin_dir / task_id
    task_dir.mkdir()
    (task_dir / "workspace").mkdir()

    created = datetime.utcnow() - timedelta(days=days_ago)
    config = {
        "task_id": task_id,
        "created_at": created.isoformat() + "Z",
        "timeout_seconds": 60,
    }
    (task_dir / "config.json").write_text(json.dumps(config))
    return task_dir


class TestCleanupOldTasks:
    def test_removes_old_tasks(self, plugin_dir):
        _create_task(plugin_dir, "old-task", days_ago=10)
        _create_task(plugin_dir, "new-task", days_ago=1)

        removed = cleanup_old_tasks(plugin_dir, retention_days=7)

        assert removed == 1
        assert not (plugin_dir / "old-task").exists()
        assert (plugin_dir / "new-task").exists()

    def test_keeps_recent_tasks(self, plugin_dir):
        _create_task(plugin_dir, "task-1", days_ago=3)
        _create_task(plugin_dir, "task-2", days_ago=5)

        removed = cleanup_old_tasks(plugin_dir, retention_days=7)

        assert removed == 0
        assert (plugin_dir / "task-1").exists()
        assert (plugin_dir / "task-2").exists()

    def test_skips_reserved_dirs(self, plugin_dir):
        # triggered and completed should never be removed
        removed = cleanup_old_tasks(plugin_dir, retention_days=0)
        assert removed == 0
        assert (plugin_dir / "triggered").exists()
        assert (plugin_dir / "completed").exists()

    def test_skips_dirs_without_config(self, plugin_dir):
        random_dir = plugin_dir / "some-random-dir"
        random_dir.mkdir()

        removed = cleanup_old_tasks(plugin_dir, retention_days=0)
        assert removed == 0
        assert random_dir.exists()

    def test_handles_invalid_config_json(self, plugin_dir):
        task_dir = plugin_dir / "bad-task"
        task_dir.mkdir()
        (task_dir / "config.json").write_text("not valid json")

        removed = cleanup_old_tasks(plugin_dir, retention_days=0)
        assert removed == 0  # Should not crash, just skip

    def test_handles_missing_created_at(self, plugin_dir):
        task_dir = plugin_dir / "no-date-task"
        task_dir.mkdir()
        (task_dir / "config.json").write_text('{"task_id": "no-date-task"}')

        removed = cleanup_old_tasks(plugin_dir, retention_days=0)
        assert removed == 0  # Skipped, not removed

    def test_nonexistent_folder(self):
        removed = cleanup_old_tasks(Path("/nonexistent/path"), retention_days=7)
        assert removed == 0

    def test_zero_retention_removes_old(self, plugin_dir):
        _create_task(plugin_dir, "old-task", days_ago=1)
        removed = cleanup_old_tasks(plugin_dir, retention_days=0)
        assert removed == 1
        assert not (plugin_dir / "old-task").exists()


class TestCleanupOrphanedSignals:
    def test_removes_orphaned_trigger(self, plugin_dir):
        # Create trigger file but no task folder
        (plugin_dir / "triggered" / "dead-task-abc12345.trigger").touch()

        _cleanup_orphaned_signals(plugin_dir)

        assert not (plugin_dir / "triggered" / "dead-task-abc12345.trigger").exists()

    def test_removes_orphaned_done(self, plugin_dir):
        (plugin_dir / "completed" / "dead-task.done").touch()

        _cleanup_orphaned_signals(plugin_dir)

        assert not (plugin_dir / "completed" / "dead-task.done").exists()

    def test_keeps_signals_with_task_folders(self, plugin_dir):
        _create_task(plugin_dir, "live-task", days_ago=1)
        (plugin_dir / "triggered" / "live-task-abc12345.trigger").touch()
        (plugin_dir / "completed" / "live-task.done").touch()

        _cleanup_orphaned_signals(plugin_dir)

        assert (plugin_dir / "triggered" / "live-task-abc12345.trigger").exists()
        assert (plugin_dir / "completed" / "live-task.done").exists()
