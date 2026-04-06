"""Tests for FileWatchToolConnector."""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from carpenter.channels.tool_connector import FileWatchToolConnector


class TestFileWatchToolConnector:
    def test_kind_is_tool(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        c = FileWatchToolConnector("test", {
            "enabled": True,
            "shared_folder": str(shared),
        })
        assert c.kind == "tool"
        assert c.name == "test"

    def test_enabled_creates_transport(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        c = FileWatchToolConnector("test", {
            "enabled": True,
            "shared_folder": str(shared),
        })
        assert c.transport is not None

    def test_disabled_no_transport(self, tmp_path):
        c = FileWatchToolConnector("test", {
            "enabled": False,
            "shared_folder": str(tmp_path),
        })
        assert c.transport is None
        assert c.enabled is False

    def test_start_creates_folder(self, tmp_path):
        shared = tmp_path / "new_shared"
        c = FileWatchToolConnector("test", {
            "enabled": True,
            "shared_folder": str(shared),
        })
        asyncio.get_event_loop().run_until_complete(c.start({}))
        assert shared.exists()

    def test_stop_is_noop(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        c = FileWatchToolConnector("test", {
            "enabled": True,
            "shared_folder": str(shared),
        })
        # Should not raise
        asyncio.get_event_loop().run_until_complete(c.stop())

    def test_health_check_no_heartbeat(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        c = FileWatchToolConnector("test", {
            "enabled": True,
            "shared_folder": str(shared),
        })
        result = asyncio.get_event_loop().run_until_complete(c.health_check())
        assert result.healthy is False

    def test_health_check_with_heartbeat(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "triggered").mkdir()
        (shared / "completed").mkdir()
        heartbeat = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        (shared / "heartbeat.json").write_text(json.dumps(heartbeat))

        c = FileWatchToolConnector("test", {
            "enabled": True,
            "shared_folder": str(shared),
        })
        result = asyncio.get_event_loop().run_until_complete(c.health_check())
        assert result.healthy is True

    def test_health_check_disabled(self, tmp_path):
        c = FileWatchToolConnector("test", {
            "enabled": False,
            "shared_folder": str(tmp_path),
        })
        result = asyncio.get_event_loop().run_until_complete(c.health_check())
        assert result.healthy is False
        assert "not configured" in result.detail
