"""Read-only plugin tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""

from ..tool_meta import tool


@tool(local=True, readonly=True, side_effects=False)
def list_plugins() -> list[dict]:
    """List all configured and enabled plugins."""
    ...


@tool(local=True, readonly=True, side_effects=False,
      param_types={"plugin_name": "Label", "task_id": "Label"})
def get_task_status(plugin_name: str, task_id: str) -> dict:
    """Check the current status of a plugin task."""
    ...


@tool(local=True, readonly=True, side_effects=False,
      param_types={"plugin_name": "Label", "task_id": "Label", "file_path": "WorkspacePath"})
def read_workspace_file(plugin_name: str, task_id: str, file_path: str) -> str:
    """Read a specific file from a completed plugin task workspace."""
    ...


@tool(local=True, readonly=True, side_effects=False,
      param_types={"plugin_name": "Label"})
def check_health(plugin_name: str) -> dict:
    """Check whether a plugin's external watcher is running."""
    ...
