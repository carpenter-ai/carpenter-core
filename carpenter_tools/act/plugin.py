"""Plugin action tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""
from ..tool_meta import tool


@tool(local=False, readonly=False, side_effects=True,
      param_types={"plugin_name": "Label", "prompt": "UnstructuredText", "working_directory": "WorkspacePath"})
def submit_task(plugin_name: str, prompt: str, files: dict | None = None,
                working_directory: str | None = None,
                context: dict | None = None,
                timeout_seconds: int = 600) -> dict:
    """Submit a task to an external plugin and wait for completion.

    This tool sends a prompt to an external tool (e.g. a coding agent)
    and waits for the result. The prompt is visible to the code reviewer.

    WARNING: This is the blocking pattern (Pattern A). If the platform
    crashes while polling, the poll state is lost. For long-running or
    critical tasks, prefer submit_task_async() (Pattern B).

    Args:
        plugin_name: Name of the plugin (must be in plugins.json)
        prompt: The task prompt — visible in reviewed code
        files: Optional dict of {relative_path: content} for workspace
        working_directory: Optional existing dir to use as workspace
        context: Optional dict of additional context for the tool
        timeout_seconds: Maximum wait time (default 600)

    Returns:
        dict with:
            - status: 'completed' | 'failed' | 'timeout'
            - output: Main text response from the tool
            - file_manifest: List of files in workspace [{path, size_bytes, ...}]
            - workspace_path: Path to workspace directory
            - task_id: Unique task identifier
            - duration_seconds: Execution time
            - exit_code: Process exit code
            - error: Error message if failed
    """
    ...


@tool(local=False, readonly=False, side_effects=True,
      param_types={"plugin_name": "Label", "prompt": "UnstructuredText", "working_directory": "WorkspacePath"})
def submit_task_async(plugin_name: str, prompt: str,
                      files: dict | None = None,
                      working_directory: str | None = None,
                      context: dict | None = None,
                      timeout_seconds: int = 600) -> dict:
    """Submit a task to an external plugin and return immediately.

    This is the restart-resilient pattern (Pattern B). Returns as soon as
    the task is submitted, with a task_id that can be used to check status
    later via plugin.check_task().

    Recommended usage for long-running background work:
        1. Call submit_task_async() → get task_id
        2. Create a child arc with arc_activation waiting for the plugin
           completion event (event_type='plugin.task_completed',
           filter={'task_id': task_id})
        3. Complete the current arc
        4. When the plugin finishes, the child arc activates and retrieves
           the result via plugin.check_task()

    Args:
        plugin_name: Name of the plugin (must be in plugins.json)
        prompt: The task prompt — visible in reviewed code
        files: Optional dict of {relative_path: content} for workspace
        working_directory: Optional existing dir to use as workspace
        context: Optional dict of additional context for the tool
        timeout_seconds: Timeout hint passed to the plugin (default 600)

    Returns:
        dict with:
            - task_id: Unique task identifier for status checking
            - plugin_name: Name of the plugin the task was submitted to
            - error: Error message if submission failed
    """
    ...
