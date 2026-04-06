"""Tests for the plugin tool backend."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from carpenter.channels.registry import ConnectorRegistry
from carpenter.tool_backends import plugin as plugin_backend


def _make_connectors_config(shared_folder):
    return {
        "test-plugin": {
            "kind": "tool",
            "enabled": True,
            "transport": "file_watch",
            "description": "Test plugin",
            "shared_folder": str(shared_folder),
            "timeout_seconds": 60,
        },
    }


@pytest.fixture
def plugin_setup(tmp_path):
    """Set up a connector registry with a test plugin."""
    shared = tmp_path / "shared" / "test-plugin"
    shared.mkdir(parents=True)
    connectors_config = _make_connectors_config(shared)

    registry = ConnectorRegistry(connectors_config)

    with patch("carpenter.tool_backends.plugin.get_connector_registry",
               return_value=registry):
        yield {
            "registry": registry,
            "shared": shared,
        }


class TestHandleSubmitTask:
    def test_submit_creates_task(self, plugin_setup):
        result = plugin_backend.handle_submit_task({
            "plugin_name": "test-plugin",
            "prompt": "do something useful",
        })

        assert "task_id" in result
        assert result["plugin_name"] == "test-plugin"

        # Verify task folder was created
        shared = plugin_setup["shared"]
        task_id = result["task_id"]
        assert (shared / task_id / "config.json").exists()
        assert (shared / task_id / "prompt.txt").exists()
        assert (shared / task_id / "prompt.txt").read_text() == "do something useful"

    def test_submit_creates_trigger(self, plugin_setup):
        result = plugin_backend.handle_submit_task({
            "plugin_name": "test-plugin",
            "prompt": "test",
        })

        shared = plugin_setup["shared"]
        trigger_files = list((shared / "triggered").iterdir())
        assert len(trigger_files) == 1
        assert trigger_files[0].name.startswith(result["task_id"])

    def test_submit_missing_plugin_name(self, plugin_setup):
        result = plugin_backend.handle_submit_task({
            "prompt": "test",
        })
        assert "error" in result

    def test_submit_missing_prompt(self, plugin_setup):
        result = plugin_backend.handle_submit_task({
            "plugin_name": "test-plugin",
        })
        assert "error" in result

    def test_submit_unknown_plugin(self, plugin_setup):
        with pytest.raises(ValueError, match="not found"):
            plugin_backend.handle_submit_task({
                "plugin_name": "nonexistent-plugin",
                "prompt": "test",
            })

    def test_submit_with_files(self, plugin_setup):
        result = plugin_backend.handle_submit_task({
            "plugin_name": "test-plugin",
            "prompt": "process files",
            "files": {"input.txt": "hello world"},
        })

        shared = plugin_setup["shared"]
        task_id = result["task_id"]
        workspace = shared / task_id / "workspace"
        assert (workspace / "input.txt").read_text() == "hello world"

    def test_submit_with_context(self, plugin_setup):
        result = plugin_backend.handle_submit_task({
            "plugin_name": "test-plugin",
            "prompt": "test",
            "context": {"env": "staging"},
        })

        shared = plugin_setup["shared"]
        task_id = result["task_id"]
        config = json.loads((shared / task_id / "config.json").read_text())
        assert config["context"] == {"env": "staging"}


class TestHandleCheckTask:
    def test_check_not_complete(self, plugin_setup):
        submit = plugin_backend.handle_submit_task({
            "plugin_name": "test-plugin",
            "prompt": "test",
        })

        result = plugin_backend.handle_check_task({
            "plugin_name": "test-plugin",
            "task_id": submit["task_id"],
        })

        assert result["completed"] is False

    def test_check_complete(self, plugin_setup):
        submit = plugin_backend.handle_submit_task({
            "plugin_name": "test-plugin",
            "prompt": "test",
        })
        task_id = submit["task_id"]
        shared = plugin_setup["shared"]

        # Simulate watcher completing the task
        task_dir = shared / task_id
        result_data = {
            "task_id": task_id,
            "status": "completed",
            "exit_code": 0,
            "duration_seconds": 5.2,
            "error": None,
        }
        (task_dir / "result.json").write_text(json.dumps(result_data))
        (task_dir / "output.txt").write_text("Done!")
        (shared / "completed" / f"{task_id}.done").touch()

        result = plugin_backend.handle_check_task({
            "plugin_name": "test-plugin",
            "task_id": task_id,
        })

        assert result["completed"] is True
        assert result["result"]["status"] == "completed"
        assert result["result"]["output"] == "Done!"


class TestHandleCheckHealth:
    def test_no_heartbeat(self, plugin_setup):
        result = plugin_backend.handle_check_health({
            "plugin_name": "test-plugin",
        })
        assert result["healthy"] is False

    def test_missing_plugin_name(self, plugin_setup):
        result = plugin_backend.handle_check_health({})
        assert "error" in result


class TestHandleReadWorkspaceFile:
    def test_read_file(self, plugin_setup):
        submit = plugin_backend.handle_submit_task({
            "plugin_name": "test-plugin",
            "prompt": "test",
            "files": {"data.txt": "file content"},
        })

        result = plugin_backend.handle_read_workspace_file({
            "plugin_name": "test-plugin",
            "task_id": submit["task_id"],
            "file_path": "data.txt",
        })

        assert result["content"] == "file content"

    def test_read_nonexistent_file(self, plugin_setup):
        submit = plugin_backend.handle_submit_task({
            "plugin_name": "test-plugin",
            "prompt": "test",
        })

        result = plugin_backend.handle_read_workspace_file({
            "plugin_name": "test-plugin",
            "task_id": submit["task_id"],
            "file_path": "nope.txt",
        })

        assert "error" in result


class TestHandleListPlugins:
    def test_list_plugins(self, plugin_setup):
        result = plugin_backend.handle_list_plugins({})
        assert len(result["plugins"]) == 1
        assert result["plugins"][0]["name"] == "test-plugin"

    def test_list_when_no_registry(self):
        with patch("carpenter.tool_backends.plugin.get_connector_registry",
                    return_value=None):
            result = plugin_backend.handle_list_plugins({})
            assert result["plugins"] == []


class TestHandleGetTaskStatus:
    def test_task_not_found(self, plugin_setup):
        result = plugin_backend.handle_get_task_status({
            "plugin_name": "test-plugin",
            "task_id": "nonexistent",
        })
        assert result["status"] == "not_found"

    def test_task_pending(self, plugin_setup):
        submit = plugin_backend.handle_submit_task({
            "plugin_name": "test-plugin",
            "prompt": "test",
        })
        # Remove trigger to make it "pending" again
        shared = plugin_setup["shared"]
        for f in (shared / "triggered").iterdir():
            f.unlink()

        result = plugin_backend.handle_get_task_status({
            "plugin_name": "test-plugin",
            "task_id": submit["task_id"],
        })
        assert result["status"] == "pending"
