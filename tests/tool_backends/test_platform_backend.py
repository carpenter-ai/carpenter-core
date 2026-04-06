"""Tests for platform tool backend."""
import pytest
from unittest.mock import patch

from carpenter.tool_backends.platform import handle_request_restart


class TestPlatformBackend:

    @patch("carpenter.tool_backends.platform.work_queue")
    def test_request_restart_opportunistic(self, mock_wq):
        result = handle_request_restart({"mode": "opportunistic"})
        assert result["status"] == "queued"
        mock_wq.enqueue.assert_called_once()
        call_args = mock_wq.enqueue.call_args
        assert call_args[0][0] == "platform.restart"
        assert call_args[0][1]["mode"] == "opportunistic"

    @patch("carpenter.tool_backends.platform.work_queue")
    def test_request_restart_urgent(self, mock_wq):
        result = handle_request_restart({"mode": "urgent"})
        assert result["status"] == "initiated"
        call_args = mock_wq.enqueue.call_args
        assert call_args[0][1]["mode"] == "graceful"

    @patch("carpenter.tool_backends.platform.work_queue")
    def test_request_restart_default_mode(self, mock_wq):
        result = handle_request_restart({})
        assert result["status"] == "queued"
        call_args = mock_wq.enqueue.call_args
        assert call_args[0][1]["mode"] == "opportunistic"
