"""Tests for submit_task_async (non-blocking plugin submission)."""

from unittest.mock import patch

import pytest


def test_submit_task_async_returns_immediately_with_task_id():
    """submit_task_async returns task_id without polling."""
    from carpenter_tools.act.plugin import submit_task_async

    mock_response = {"task_id": "test-uuid-1234", "plugin_name": "test-plugin"}

    with patch("carpenter_tools.act.plugin.callback", return_value=mock_response) as mock_cb:
        result = submit_task_async(
            plugin_name="test-plugin",
            prompt="do something",
        )

    assert result["task_id"] == "test-uuid-1234"
    assert result["plugin_name"] == "test-plugin"
    assert "error" not in result

    # Verify callback was called exactly once (submit only, no polling)
    mock_cb.assert_called_once_with("plugin.submit_task", {
        "plugin_name": "test-plugin",
        "prompt": "do something",
        "files": None,
        "working_directory": None,
        "context": None,
        "timeout_seconds": 600,
    })


def test_submit_task_async_returns_error_on_failure():
    """submit_task_async propagates submission errors."""
    from carpenter_tools.act.plugin import submit_task_async

    mock_response = {"error": "Plugin not found"}

    with patch("carpenter_tools.act.plugin.callback", return_value=mock_response):
        result = submit_task_async(
            plugin_name="nonexistent",
            prompt="do something",
        )

    assert result["error"] == "Plugin not found"
    assert result["task_id"] is None
    assert result["plugin_name"] == "nonexistent"


def test_submit_task_async_passes_all_params():
    """submit_task_async forwards all parameters to the callback."""
    from carpenter_tools.act.plugin import submit_task_async

    mock_response = {"task_id": "uuid-123", "plugin_name": "my-plugin"}

    with patch("carpenter_tools.act.plugin.callback", return_value=mock_response) as mock_cb:
        result = submit_task_async(
            plugin_name="my-plugin",
            prompt="build feature",
            files={"main.py": "print('hi')"},
            working_directory="/tmp/work",
            context={"repo": "test"},
            timeout_seconds=1200,
        )

    assert result["task_id"] == "uuid-123"
    mock_cb.assert_called_once_with("plugin.submit_task", {
        "plugin_name": "my-plugin",
        "prompt": "build feature",
        "files": {"main.py": "print('hi')"},
        "working_directory": "/tmp/work",
        "context": {"repo": "test"},
        "timeout_seconds": 1200,
    })
