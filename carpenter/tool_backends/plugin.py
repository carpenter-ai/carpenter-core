"""Plugin tool backend — handles plugin callbacks from executors.

Manages plugin task lifecycle: submission, status checking, health
monitoring, and workspace file access.
"""

import logging
import uuid

from ..channels.registry import get_connector_registry

logger = logging.getLogger(__name__)


def _get_transport(plugin_name: str):
    """Look up plugin transport via connector registry, raising clear errors on failure."""
    registry = get_connector_registry()
    if registry is None:
        raise RuntimeError(
            "Connector system not initialized. Add connectors to "
            "config.yaml or create plugins.json for auto-migration."
        )

    connector = registry.get(plugin_name)
    if connector is None:
        available = [c.name for c in registry.list_connectors(kind="tool")]
        raise ValueError(
            f"Plugin '{plugin_name}' not found. "
            f"Available: {available}"
        )

    if not hasattr(connector, "transport") or connector.transport is None:
        raise RuntimeError(
            f"Plugin '{plugin_name}' has no transport configured"
        )

    return connector.transport


def handle_submit_task(params: dict) -> dict:
    """Create workspace, write config + prompt, and touch trigger file.

    Params:
        plugin_name (str): Name of the target plugin
        prompt (str): Task prompt (visible to code reviewer)
        files (dict, optional): {relative_path: content} for workspace
        working_directory (str, optional): Existing dir to use as workspace
        context (dict, optional): Additional context
        timeout_seconds (int, optional): Task timeout (default from config)

    Returns:
        dict with task_id and plugin_name
    """
    plugin_name = params.get("plugin_name")
    prompt = params.get("prompt")

    if not plugin_name:
        return {"error": "Missing required parameter: plugin_name"}
    if not prompt:
        return {"error": "Missing required parameter: prompt"}

    transport = _get_transport(plugin_name)

    # Check watcher health before submitting
    health = transport.check_health()
    if not health.get("healthy", False):
        age = health.get("age_seconds")
        if age is not None:
            msg = (f"Watcher for plugin '{plugin_name}' appears to be down "
                   f"(last heartbeat: {age}s ago). Task submitted anyway — "
                   f"it will execute when the watcher comes back online.")
            logger.warning(msg)
        else:
            msg = (f"No heartbeat found for plugin '{plugin_name}'. "
                   f"The watcher may not be installed or running.")
            logger.warning(msg)

    task_id = str(uuid.uuid4())
    timeout = params.get("timeout_seconds", transport.default_timeout)

    transport.prepare_task(
        task_id=task_id,
        prompt=prompt,
        files=params.get("files"),
        working_directory=params.get("working_directory"),
        context=params.get("context"),
        timeout_seconds=timeout,
    )
    transport.trigger_task(task_id)

    logger.info("Submitted task %s to plugin %s", task_id, plugin_name)

    return {"task_id": task_id, "plugin_name": plugin_name}


def handle_check_task(params: dict) -> dict:
    """Check if a task is complete and return results if so.

    Params:
        plugin_name (str): Name of the plugin
        task_id (str): Task identifier

    Returns:
        dict with completed (bool) and result (dict, if completed)
    """
    plugin_name = params.get("plugin_name")
    task_id = params.get("task_id")

    if not plugin_name or not task_id:
        return {"error": "Missing plugin_name or task_id"}

    transport = _get_transport(plugin_name)

    if transport.is_complete(task_id):
        result = transport.collect_result(task_id)
        return {"completed": True, "result": result}

    health = transport.check_health()
    return {
        "completed": False,
        "watcher_healthy": health.get("healthy", False),
    }


def handle_check_health(params: dict) -> dict:
    """Check watcher health for a plugin.

    Params:
        plugin_name (str): Name of the plugin

    Returns:
        dict with healthy, last_heartbeat, age_seconds
    """
    plugin_name = params.get("plugin_name")
    if not plugin_name:
        return {"error": "Missing plugin_name"}

    transport = _get_transport(plugin_name)
    return transport.check_health()


def handle_read_workspace_file(params: dict) -> dict:
    """Read a specific file from a plugin task workspace.

    Params:
        plugin_name (str): Name of the plugin
        task_id (str): Task identifier
        file_path (str): Relative path within workspace

    Returns:
        dict with content (str)
    """
    plugin_name = params.get("plugin_name")
    task_id = params.get("task_id")
    file_path = params.get("file_path")

    if not all([plugin_name, task_id, file_path]):
        return {"error": "Missing plugin_name, task_id, or file_path"}

    transport = _get_transport(plugin_name)

    try:
        content = transport.read_workspace_file(task_id, file_path)
        return {"content": content}
    except FileNotFoundError as e:
        return {"error": str(e)}
    except ValueError as e:
        return {"error": str(e)}


def handle_list_plugins(params: dict) -> dict:
    """List all configured and enabled plugins.

    Returns:
        dict with plugins list
    """
    registry = get_connector_registry()
    if registry is None:
        return {"plugins": []}

    result = []
    for connector in registry.list_connectors(kind="tool"):
        info = {
            "name": connector.name,
            "enabled": connector.enabled,
            "description": getattr(connector, "_config", {}).get("description", ""),
            "transport": getattr(connector, "_config", {}).get("transport", ""),
        }
        if hasattr(connector, "transport") and connector.transport:
            health = connector.transport.check_health()
            info["watcher_healthy"] = health.get("healthy", False)
        result.append(info)

    return {"plugins": result}


def handle_get_task_status(params: dict) -> dict:
    """Get current status of a plugin task.

    Params:
        plugin_name (str): Name of the plugin
        task_id (str): Task identifier

    Returns:
        dict with task_id and status
    """
    plugin_name = params.get("plugin_name")
    task_id = params.get("task_id")

    if not plugin_name or not task_id:
        return {"error": "Missing plugin_name or task_id"}

    transport = _get_transport(plugin_name)
    return transport.get_task_status(task_id)
