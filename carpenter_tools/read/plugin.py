"""Plugin read-only tools. Tier 1: callback to platform.

Safe, read-only access to plugin information and workspace files.
These tools do not require reviewed code.
"""

from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=True, side_effects=False)
def list_plugins() -> list[dict]:
    """List all configured and enabled plugins."""
    result = callback("plugin.list_plugins", {})
    return result.get("plugins", [])


@tool(local=True, readonly=True, side_effects=False,
      param_types={"plugin_name": "Label", "task_id": "Label"})
def get_task_status(plugin_name: str, task_id: str) -> dict:
    """Check the current status of a plugin task."""
    return callback("plugin.get_task_status", {
        "plugin_name": plugin_name,
        "task_id": task_id,
    })


@tool(local=True, readonly=True, side_effects=False,
      param_types={"plugin_name": "Label", "task_id": "Label", "file_path": "WorkspacePath"})
def read_workspace_file(plugin_name: str, task_id: str, file_path: str) -> str:
    """Read a specific file from a completed plugin task workspace."""
    result = callback("plugin.read_workspace_file", {
        "plugin_name": plugin_name,
        "task_id": task_id,
        "file_path": file_path,
    })
    if "error" in result:
        raise FileNotFoundError(result["error"])
    return result.get("content", "")


@tool(local=True, readonly=True, side_effects=False,
      param_types={"plugin_name": "Label"})
def check_health(plugin_name: str) -> dict:
    """Check whether a plugin's external watcher is running."""
    return callback("plugin.check_health", {
        "plugin_name": plugin_name,
    })
