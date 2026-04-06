"""Tool definition loader for Carpenter.

Chat tool definitions are now Python-defined in chat_tool_registry.py.
This module only handles coding agent tool definitions (YAML-based).
"""

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Coding agent tool defaults ship in config_seed/coding-tools/ at repo root
_CODING_DEFAULTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config_seed", "coding-tools",
)


def _install_defaults(defaults_dir: str, target_dir: str, label: str) -> dict:
    """Copy a defaults directory to target_dir if it doesn't exist.

    Returns:
        {"status": "installed"|"exists"|"no_defaults", "copied": int}
    """
    if os.path.isdir(target_dir):
        return {"status": "exists", "copied": 0}

    if not os.path.isdir(defaults_dir):
        logger.warning("%s defaults directory not found: %s", label, defaults_dir)
        return {"status": "no_defaults", "copied": 0}

    try:
        shutil.copytree(defaults_dir, target_dir)
        count = sum(1 for _ in Path(target_dir).glob("*.yaml"))
        logger.info("Installed %s defaults: %d files to %s", label, count, target_dir)
        return {"status": "installed", "copied": count}
    except OSError as e:
        logger.error("Failed to install %s defaults: %s", label, e)
        return {"status": "error", "error": str(e), "copied": 0}


def install_coding_tool_defaults(coding_tools_dir: str) -> dict:
    """Copy config_seed/coding-tools/ to coding_tools_dir if it doesn't exist.

    Returns:
        {"status": "installed"|"exists"|"no_defaults", "copied": int}
    """
    return _install_defaults(_CODING_DEFAULTS_DIR, coding_tools_dir, "Coding tool")


def load_coding_tool_definitions(coding_tools_dir: str) -> list[dict] | None:
    """Load coding agent tool definitions from YAML files.

    Args:
        coding_tools_dir: Path to the coding tools directory.

    Returns:
        List of tool definition dicts, or None if unavailable.
    """
    if not os.path.isdir(coding_tools_dir):
        return None

    try:
        import yaml
    except ImportError:
        logger.debug("PyYAML not installed, cannot load coding tool definitions")
        return None

    yaml_files = sorted(Path(coding_tools_dir).glob("*.yaml"))
    if not yaml_files:
        return None

    all_tools = []
    for yaml_file in yaml_files:
        try:
            content = yaml_file.read_text()
            data = yaml.safe_load(content)
            if not isinstance(data, dict) or "tools" not in data:
                logger.warning(
                    "Skipping invalid coding tool file (no 'tools' key): %s",
                    yaml_file.name,
                )
                continue
            tools_list = data["tools"]
            if not isinstance(tools_list, list):
                logger.warning(
                    "Skipping invalid coding tool file ('tools' not a list): %s",
                    yaml_file.name,
                )
                continue
            for tool in tools_list:
                if not isinstance(tool, dict) or "name" not in tool:
                    logger.warning("Skipping invalid coding tool entry in %s", yaml_file.name)
                    continue
                tool_def = {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema", {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    }),
                }
                all_tools.append(tool_def)
        except Exception:
            logger.warning("Failed to parse coding tool file: %s", yaml_file.name, exc_info=True)
            continue

    return all_tools if all_tools else None
