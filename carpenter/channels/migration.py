"""Migration from plugins.json to connectors config format.

Reads the old plugins.json file and converts to the connectors dict
format used by ConnectorRegistry.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def migrate_plugins_json(config_dict: dict) -> dict:
    """Read plugins.json and convert to connectors config format.

    Looks up plugins_config path from config_dict, reads the JSON file,
    and converts each plugin entry to a connector entry.

    Args:
        config_dict: The current CONFIG dict (for path resolution).

    Returns:
        Dict of connector configs, keyed by name. Empty dict if no
        plugins.json found or empty.
    """
    plugins_config_path = config_dict.get("plugins_config", "")
    if not plugins_config_path:
        base_dir = config_dict.get("base_dir", "")
        if base_dir:
            plugins_config_path = str(Path(base_dir) / "config" / "plugins.json")
        else:
            return {}

    path = Path(plugins_config_path)
    if not path.exists():
        return {}

    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read plugins.json for migration: %s", e)
        return {}

    plugin_defs = data.get("plugins", {})
    if not plugin_defs:
        return {}

    connectors = {}
    for name, plugin_config in plugin_defs.items():
        transport_type = plugin_config.get("transport", "")
        transport_config = plugin_config.get("transport_config", {})

        connector_entry = {
            "kind": "tool",
            "enabled": plugin_config.get("enabled", False),
            "description": plugin_config.get("description", ""),
        }

        if transport_type == "file-watch":
            connector_entry["transport"] = "file_watch"
            connector_entry["shared_folder"] = transport_config.get("shared_folder", "")
            connector_entry["timeout_seconds"] = transport_config.get("timeout_seconds", 600)
        else:
            connector_entry["transport"] = transport_type
            connector_entry.update(transport_config)

        connectors[name] = connector_entry

    if connectors:
        logger.info(
            "Migrated %d plugin(s) from plugins.json to connector format",
            len(connectors),
        )

    return connectors
