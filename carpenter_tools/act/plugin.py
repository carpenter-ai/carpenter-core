"""Plugin action tools. Tier 1: callback to platform.

These tools delegate tasks to external tools via the plugin system.
They can ONLY be called from reviewed Python code — the human reviewer
sees every prompt before it reaches the external tool.

Two submission patterns are available:

Pattern A (blocking): submit_task() — submits and polls until completion.
    Use for quick tasks in interactive sessions. Fragile on crash: the
    blocking poll loop loses state on restart.

Pattern B (resilient): submit_task_async() — submits and returns immediately
    with a task_id. The caller should create a child arc with an
    arc_activation waiting for the plugin completion event, then complete.
    When the plugin finishes, the event fires, the child arc activates,
    and picks up the result via plugin.check_task(). This pattern survives
    platform restarts.
"""

import time

from .._callback import callback
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
    # Submit task via callback to platform
    submission = callback("plugin.submit_task", {
        "plugin_name": plugin_name,
        "prompt": prompt,
        "files": files,
        "working_directory": working_directory,
        "context": context,
        "timeout_seconds": timeout_seconds,
    })

    if "error" in submission:
        return {
            "status": "failed",
            "error": submission["error"],
            "output": "",
            "task_id": None,
        }

    task_id = submission["task_id"]

    # Poll for completion
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(2)

        status = callback("plugin.check_task", {
            "plugin_name": plugin_name,
            "task_id": task_id,
        })

        if status.get("completed"):
            result = status["result"]
            result["task_id"] = task_id
            return result

        # If watcher is unhealthy, keep polling but at a slower rate
        if not status.get("watcher_healthy", True):
            time.sleep(3)  # 5 seconds total between checks

    return {
        "status": "timeout",
        "task_id": task_id,
        "error": f"Task timed out after {timeout_seconds}s",
        "output": "",
    }


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
    submission = callback("plugin.submit_task", {
        "plugin_name": plugin_name,
        "prompt": prompt,
        "files": files,
        "working_directory": working_directory,
        "context": context,
        "timeout_seconds": timeout_seconds,
    })

    if "error" in submission:
        return {
            "error": submission["error"],
            "task_id": None,
            "plugin_name": plugin_name,
        }

    return {
        "task_id": submission["task_id"],
        "plugin_name": submission.get("plugin_name", plugin_name),
    }
