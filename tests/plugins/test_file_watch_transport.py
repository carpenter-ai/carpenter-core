"""Tests for the file-watch transport."""

import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from carpenter.channels.transports.file_watch import FileWatchTransport


@pytest.fixture
def transport(tmp_path):
    """Create a FileWatchTransport with a temp shared folder."""
    shared = tmp_path / "test-plugin"
    return FileWatchTransport("test-plugin", {
        "shared_folder": str(shared),
        "timeout_seconds": 60,
    })


@pytest.fixture
def shared_folder(transport):
    return transport.shared_folder


class TestFolderStructure:
    def test_creates_shared_folder(self, transport, shared_folder):
        assert shared_folder.exists()

    def test_creates_triggered_dir(self, transport, shared_folder):
        assert (shared_folder / "triggered").exists()

    def test_creates_completed_dir(self, transport, shared_folder):
        assert (shared_folder / "completed").exists()


class TestPrepareTask:
    def test_creates_task_directory(self, transport, shared_folder):
        transport.prepare_task("task-1", "do something", None, None, None, 60)
        assert (shared_folder / "task-1").is_dir()

    def test_writes_config_json(self, transport, shared_folder):
        transport.prepare_task("task-1", "do something", None, None, None, 60)
        config_path = shared_folder / "task-1" / "config.json"
        assert config_path.exists()

        with open(config_path) as f:
            data = json.load(f)

        assert data["task_id"] == "task-1"
        assert data["timeout_seconds"] == 60
        assert data["metadata"]["plugin"] == "test-plugin"

    def test_writes_prompt_txt(self, transport, shared_folder):
        transport.prepare_task("task-1", "install nginx", None, None, None, 60)
        prompt_path = shared_folder / "task-1" / "prompt.txt"
        assert prompt_path.exists()
        assert prompt_path.read_text() == "install nginx"

    def test_prompt_not_in_config_json(self, transport, shared_folder):
        transport.prepare_task("task-1", "secret prompt", None, None, None, 60)
        config_path = shared_folder / "task-1" / "config.json"
        with open(config_path) as f:
            data = json.load(f)
        # Prompt should NOT be in config.json
        assert "prompt" not in data
        assert "secret prompt" not in json.dumps(data)

    def test_creates_workspace_directory(self, transport, shared_folder):
        transport.prepare_task("task-1", "test", None, None, None, 60)
        workspace = shared_folder / "task-1" / "workspace"
        assert workspace.is_dir()

    def test_writes_files_to_workspace(self, transport, shared_folder):
        files = {
            "src/main.py": "print('hello')",
            "README.md": "# Test",
        }
        transport.prepare_task("task-1", "test", files, None, None, 60)

        workspace = shared_folder / "task-1" / "workspace"
        assert (workspace / "src" / "main.py").read_text() == "print('hello')"
        assert (workspace / "README.md").read_text() == "# Test"

    def test_uses_working_directory(self, transport, shared_folder, tmp_path):
        work_dir = tmp_path / "my_project"
        work_dir.mkdir()
        (work_dir / "existing.txt").write_text("existing content")

        transport.prepare_task("task-1", "test", None, str(work_dir),
                               None, 60)

        config_path = shared_folder / "task-1" / "config.json"
        with open(config_path) as f:
            data = json.load(f)
        assert data["working_directory"] == str(work_dir)

    def test_writes_files_into_working_directory(self, transport, tmp_path):
        work_dir = tmp_path / "my_project"
        work_dir.mkdir()

        files = {"new_file.py": "# new"}
        transport.prepare_task("task-1", "test", files, str(work_dir),
                               None, 60)
        assert (work_dir / "new_file.py").read_text() == "# new"

    def test_includes_context(self, transport, shared_folder):
        context = {"project_type": "python", "env": "production"}
        transport.prepare_task("task-1", "test", None, None, context, 60)

        config_path = shared_folder / "task-1" / "config.json"
        with open(config_path) as f:
            data = json.load(f)
        assert data["context"] == context


class TestTriggerTask:
    def test_creates_trigger_file(self, transport, shared_folder):
        transport.prepare_task("task-1", "test", None, None, None, 60)
        transport.trigger_task("task-1")

        triggered = shared_folder / "triggered"
        trigger_files = list(triggered.iterdir())
        assert len(trigger_files) == 1
        assert trigger_files[0].name.startswith("task-1-")
        assert trigger_files[0].name.endswith(".trigger")

    def test_trigger_filename_contains_checksum(self, transport, shared_folder):
        transport.prepare_task("task-1", "test", None, None, None, 60)
        transport.trigger_task("task-1")

        # Compute expected checksum
        config_path = shared_folder / "task-1" / "config.json"
        expected_checksum = hashlib.sha256(
            config_path.read_bytes()
        ).hexdigest()[:8]

        trigger_files = list((shared_folder / "triggered").iterdir())
        assert len(trigger_files) == 1
        assert expected_checksum in trigger_files[0].name


class TestCompletion:
    def test_is_complete_false(self, transport, shared_folder):
        transport.prepare_task("task-1", "test", None, None, None, 60)
        assert transport.is_complete("task-1") is False

    def test_is_complete_true(self, transport, shared_folder):
        transport.prepare_task("task-1", "test", None, None, None, 60)
        (shared_folder / "completed" / "task-1.done").touch()
        assert transport.is_complete("task-1") is True


class TestCollectResult:
    def _setup_completed_task(self, transport, shared_folder, task_id="task-1"):
        transport.prepare_task(task_id, "test prompt", None, None, None, 60)

        task_dir = shared_folder / task_id

        # Write result.json
        result = {
            "task_id": task_id,
            "status": "completed",
            "exit_code": 0,
            "duration_seconds": 12.5,
            "error": None,
        }
        (task_dir / "result.json").write_text(json.dumps(result))
        (task_dir / "output.txt").write_text("Task completed successfully")

        # Create some workspace files
        workspace = task_dir / "workspace"
        (workspace / "output.py").write_text("print('done')")
        (workspace / "data").mkdir()
        (workspace / "data" / "results.json").write_text('{"ok": true}')

        # Signal completion
        (shared_folder / "completed" / f"{task_id}.done").touch()

        return task_dir

    def test_collect_result_success(self, transport, shared_folder):
        self._setup_completed_task(transport, shared_folder)

        result = transport.collect_result("task-1")
        assert result["status"] == "completed"
        assert result["exit_code"] == 0
        assert result["output"] == "Task completed successfully"
        assert result["duration_seconds"] == 12.5
        assert result["error"] is None

    def test_collect_result_has_manifest(self, transport, shared_folder):
        self._setup_completed_task(transport, shared_folder)

        result = transport.collect_result("task-1")
        manifest = result["file_manifest"]
        paths = [f["path"] for f in manifest]
        assert "output.py" in paths
        assert os.path.join("data", "results.json") in paths

    def test_manifest_includes_metadata(self, transport, shared_folder):
        self._setup_completed_task(transport, shared_folder)

        result = transport.collect_result("task-1")
        manifest = result["file_manifest"]
        for entry in manifest:
            assert "path" in entry
            assert "size_bytes" in entry
            assert "modified_at" in entry
            assert "is_binary" in entry

    def test_manifest_does_not_include_content(self, transport, shared_folder):
        self._setup_completed_task(transport, shared_folder)

        result = transport.collect_result("task-1")
        manifest = result["file_manifest"]
        for entry in manifest:
            assert "content" not in entry

    def test_collect_cleans_up_signals(self, transport, shared_folder):
        self._setup_completed_task(transport, shared_folder)
        transport.trigger_task("task-1")

        result = transport.collect_result("task-1")

        # Trigger and done files should be cleaned up
        assert not (shared_folder / "completed" / "task-1.done").exists()
        trigger_files = list((shared_folder / "triggered").iterdir())
        assert len(trigger_files) == 0

    def test_collect_missing_result_json(self, transport, shared_folder):
        transport.prepare_task("task-1", "test", None, None, None, 60)
        (shared_folder / "completed" / "task-1.done").touch()

        result = transport.collect_result("task-1")
        assert result["status"] == "unknown"
        assert result["exit_code"] == 1


class TestReadWorkspaceFile:
    def test_read_file(self, transport, shared_folder):
        transport.prepare_task("task-1", "test", None, None, None, 60)
        workspace = shared_folder / "task-1" / "workspace"
        (workspace / "test.py").write_text("print('hello')")

        content = transport.read_workspace_file("task-1", "test.py")
        assert content == "print('hello')"

    def test_read_nested_file(self, transport, shared_folder):
        transport.prepare_task("task-1", "test", None, None, None, 60)
        workspace = shared_folder / "task-1" / "workspace"
        (workspace / "src").mkdir()
        (workspace / "src" / "main.py").write_text("# main")

        content = transport.read_workspace_file("task-1", "src/main.py")
        assert content == "# main"

    def test_path_traversal_blocked(self, transport, shared_folder):
        transport.prepare_task("task-1", "test", None, None, None, 60)
        # Create a file outside workspace
        (shared_folder / "secret.txt").write_text("secret data")

        with pytest.raises(ValueError, match="Path traversal"):
            transport.read_workspace_file("task-1", "../../secret.txt")

    def test_file_not_found(self, transport, shared_folder):
        transport.prepare_task("task-1", "test", None, None, None, 60)

        with pytest.raises(FileNotFoundError):
            transport.read_workspace_file("task-1", "nonexistent.py")

    def test_missing_config(self, transport, shared_folder):
        with pytest.raises(FileNotFoundError):
            transport.read_workspace_file("no-such-task", "file.py")


class TestGetTaskStatus:
    def test_not_found(self, transport):
        status = transport.get_task_status("nonexistent")
        assert status["status"] == "not_found"

    def test_pending(self, transport, shared_folder):
        transport.prepare_task("task-1", "test", None, None, None, 60)
        status = transport.get_task_status("task-1")
        assert status["status"] == "pending"

    def test_running(self, transport, shared_folder):
        transport.prepare_task("task-1", "test", None, None, None, 60)
        transport.trigger_task("task-1")
        status = transport.get_task_status("task-1")
        assert status["status"] == "running"

    def test_completed(self, transport, shared_folder):
        transport.prepare_task("task-1", "test", None, None, None, 60)
        (shared_folder / "completed" / "task-1.done").touch()
        # Write a result.json
        result = {"status": "completed", "exit_code": 0}
        (shared_folder / "task-1" / "result.json").write_text(json.dumps(result))

        status = transport.get_task_status("task-1")
        assert status["status"] == "completed"


class TestCheckHealth:
    def test_no_heartbeat(self, transport):
        health = transport.check_health()
        assert health["healthy"] is False
        assert health["last_heartbeat"] is None

    def test_recent_heartbeat(self, transport, shared_folder):
        heartbeat = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "pid": 12345,
        }
        (shared_folder / "heartbeat.json").write_text(json.dumps(heartbeat))

        health = transport.check_health()
        assert health["healthy"] is True
        assert health["age_seconds"] is not None
        assert health["age_seconds"] < 5

    def test_stale_heartbeat(self, transport, shared_folder):
        old_time = datetime.utcnow() - timedelta(minutes=5)
        heartbeat = {
            "timestamp": old_time.isoformat() + "Z",
            "pid": 12345,
        }
        (shared_folder / "heartbeat.json").write_text(json.dumps(heartbeat))

        health = transport.check_health()
        assert health["healthy"] is False
        assert health["age_seconds"] > 200

    def test_corrupt_heartbeat(self, transport, shared_folder):
        (shared_folder / "heartbeat.json").write_text("not json")
        health = transport.check_health()
        assert health["healthy"] is False


class TestBinaryDetection:
    def test_text_file(self, transport, shared_folder):
        transport.prepare_task("task-1", "test", None, None, None, 60)
        workspace = shared_folder / "task-1" / "workspace"
        (workspace / "readme.txt").write_text("Hello world")

        result = transport.collect_result("task-1")
        manifest = result["file_manifest"]
        txt_entry = [e for e in manifest if e["path"] == "readme.txt"][0]
        assert txt_entry["is_binary"] is False

    def test_binary_file(self, transport, shared_folder):
        transport.prepare_task("task-1", "test", None, None, None, 60)
        workspace = shared_folder / "task-1" / "workspace"
        (workspace / "image.bin").write_bytes(b"\x00\x01\x02\xff" * 100)

        result = transport.collect_result("task-1")
        manifest = result["file_manifest"]
        bin_entry = [e for e in manifest if e["path"] == "image.bin"][0]
        assert bin_entry["is_binary"] is True
