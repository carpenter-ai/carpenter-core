"""Connector task retention policy.

Cleans up old task folders from connector shared directories.
"""

import json
import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from .. import config

logger = logging.getLogger(__name__)

# Subdirectories that are part of the connector structure, not task folders
_RESERVED_DIRS = frozenset({"triggered", "completed"})

# Reserved files at the connector root level
_RESERVED_FILES = frozenset({"heartbeat.json"})


def cleanup_old_tasks(shared_folder: Path, retention_days: int = 7) -> int:
    """Remove task folders older than retention_days.

    Args:
        shared_folder: Path to a connector's shared folder
        retention_days: Number of days to retain completed task folders

    Returns:
        Number of task folders removed
    """
    if not shared_folder.exists():
        return 0

    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    removed = 0

    for entry in shared_folder.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in _RESERVED_DIRS:
            continue

        # Check if this looks like a task folder (has config.json)
        config_file = entry / "config.json"
        if not config_file.exists():
            continue

        try:
            with open(config_file) as f:
                task_config = json.load(f)

            created_str = task_config.get("created_at", "")
            if not created_str:
                continue

            # Parse ISO timestamp (handle both with and without Z suffix)
            created_str = created_str.rstrip("Z")
            created = datetime.fromisoformat(created_str)

            if created < cutoff:
                shutil.rmtree(entry)
                removed += 1
                logger.debug("Removed old task folder: %s", entry.name)

        except (json.JSONDecodeError, ValueError, OSError) as e:
            logger.warning("Could not check task folder %s: %s", entry.name, e)

    # Also clean up orphaned trigger and done files
    _cleanup_orphaned_signals(shared_folder)

    return removed


def _cleanup_orphaned_signals(shared_folder: Path) -> None:
    """Remove trigger/done files whose task folders no longer exist."""
    for signal_dir_name in ("triggered", "completed"):
        signal_dir = shared_folder / signal_dir_name
        if not signal_dir.exists():
            continue

        for signal_file in signal_dir.iterdir():
            if not signal_file.is_file():
                continue

            # Extract task_id from filename
            # Trigger files: {task_id}-{checksum}.trigger
            # Done files: {task_id}.done
            stem = signal_file.stem
            if "-" in stem and signal_dir_name == "triggered":
                task_id = stem.rsplit("-", 1)[0]
            else:
                task_id = stem

            task_dir = shared_folder / task_id
            if not task_dir.exists():
                try:
                    signal_file.unlink()
                    logger.debug("Removed orphaned signal: %s", signal_file.name)
                except OSError:
                    pass


def create_retention_hook():
    """Create a heartbeat hook for periodic retention cleanup.

    Runs cleanup once per hour (not every heartbeat tick).
    Returns None if connector system is not configured.
    """
    cfg = config.CONFIG

    # Collect shared folders from connectors config
    connectors = cfg.get("connectors", {})
    shared_folders = []
    for name, cc in connectors.items():
        sf = cc.get("shared_folder", "")
        if sf:
            shared_folders.append(Path(sf))

    # Also check legacy plugin_shared_base
    shared_base = cfg.get("plugin_shared_base", "")

    if not shared_folders and not shared_base:
        return None

    retention_days = cfg.get("connector_retention_days",
                             cfg.get("plugin_retention_days", 7))

    # Track last cleanup time
    state = {"last_cleanup": 0.0}

    def _retention_hook():
        now = time.time()
        # Run at most once per hour (3600 seconds)
        if now - state["last_cleanup"] < 3600:
            return

        state["last_cleanup"] = now

        total_removed = 0

        # Clean connector shared folders
        for sf in shared_folders:
            if sf.exists():
                removed = cleanup_old_tasks(sf, retention_days)
                total_removed += removed

        # Clean legacy plugin_shared_base
        if shared_base:
            shared_base_path = Path(shared_base)
            if shared_base_path.exists():
                for plugin_dir in shared_base_path.iterdir():
                    if plugin_dir.is_dir():
                        removed = cleanup_old_tasks(plugin_dir, retention_days)
                        total_removed += removed

        if total_removed > 0:
            logger.info("Retention cleanup: removed %d old task folder(s)",
                        total_removed)

    return _retention_hook
