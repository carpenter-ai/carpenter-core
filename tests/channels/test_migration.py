"""Tests for plugins.json migration to connectors format."""

import json
from pathlib import Path

import pytest

from carpenter.channels.migration import migrate_plugins_json


class TestMigratePluginsJson:
    def test_no_plugins_config(self):
        result = migrate_plugins_json({})
        assert result == {}

    def test_nonexistent_file(self, tmp_path):
        result = migrate_plugins_json({
            "plugins_config": str(tmp_path / "nonexistent.json"),
        })
        assert result == {}

    def test_empty_plugins(self, tmp_path):
        path = tmp_path / "plugins.json"
        path.write_text(json.dumps({"plugins": {}}))
        result = migrate_plugins_json({"plugins_config": str(path)})
        assert result == {}

    def test_file_watch_plugin(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        path = tmp_path / "plugins.json"
        data = {
            "plugins": {
                "claude-code": {
                    "enabled": True,
                    "description": "Claude Code integration",
                    "transport": "file-watch",
                    "transport_config": {
                        "shared_folder": str(shared),
                        "timeout_seconds": 300,
                    },
                },
            },
        }
        path.write_text(json.dumps(data))

        result = migrate_plugins_json({"plugins_config": str(path)})

        assert "claude-code" in result
        cc = result["claude-code"]
        assert cc["kind"] == "tool"
        assert cc["enabled"] is True
        assert cc["transport"] == "file_watch"
        assert cc["shared_folder"] == str(shared)
        assert cc["timeout_seconds"] == 300

    def test_disabled_plugin(self, tmp_path):
        path = tmp_path / "plugins.json"
        data = {
            "plugins": {
                "disabled-plugin": {
                    "enabled": False,
                    "transport": "file-watch",
                    "transport_config": {
                        "shared_folder": "/tmp/shared",
                    },
                },
            },
        }
        path.write_text(json.dumps(data))

        result = migrate_plugins_json({"plugins_config": str(path)})
        assert result["disabled-plugin"]["enabled"] is False

    def test_multiple_plugins(self, tmp_path):
        path = tmp_path / "plugins.json"
        data = {
            "plugins": {
                "plugin-a": {
                    "enabled": True,
                    "transport": "file-watch",
                    "transport_config": {"shared_folder": "/a"},
                },
                "plugin-b": {
                    "enabled": True,
                    "transport": "file-watch",
                    "transport_config": {"shared_folder": "/b"},
                },
            },
        }
        path.write_text(json.dumps(data))

        result = migrate_plugins_json({"plugins_config": str(path)})
        assert len(result) == 2

    def test_fallback_to_base_dir(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        path = config_dir / "plugins.json"
        data = {"plugins": {"test": {
            "enabled": True,
            "transport": "file-watch",
            "transport_config": {"shared_folder": "/tmp"},
        }}}
        path.write_text(json.dumps(data))

        result = migrate_plugins_json({"base_dir": str(tmp_path)})
        assert "test" in result

    def test_invalid_json(self, tmp_path):
        path = tmp_path / "plugins.json"
        path.write_text("not valid json")
        result = migrate_plugins_json({"plugins_config": str(path)})
        assert result == {}
