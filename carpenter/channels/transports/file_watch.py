"""File-watch transport for connector IPC.

Uses atomic trigger files with checksum-in-filename to safely signal
task readiness. The prompt is passed as a file (prompt.txt), never
as a CLI argument.

This transport is the only code that writes to the shared plugin folder.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .base import Transport

logger = logging.getLogger(__name__)


class FileWatchTransport(Transport):
    """File-watch based transport for connector IPC.

    Protocol:
    1. Create task folder: {shared_folder}/{task_id}/
    2. Prepare workspace with input files
    3. Write config.json with task metadata
    4. Write prompt.txt with the task prompt
    5. fsync config.json and prompt.txt
    6. Touch trigger file: triggered/{task_id}-{sha256[:8]}.trigger
    7. External watcher detects trigger, validates checksum, executes
    8. Watcher writes result.json + output.txt, touches completed/{task_id}.done
    9. Transport detects .done, collects manifest + metadata
    """

    def __init__(self, plugin_name: str, transport_config: dict):
        self.plugin_name = plugin_name
        self.shared_folder = Path(transport_config.get("shared_folder", ""))
        self.default_timeout = transport_config.get("timeout_seconds", 600)

        self._ensure_structure()

    def _ensure_structure(self) -> None:
        """Create the shared folder structure if it doesn't exist."""
        if not self.shared_folder:
            return
        try:
            self.shared_folder.mkdir(parents=True, exist_ok=True)
            (self.shared_folder / "triggered").mkdir(exist_ok=True)
            (self.shared_folder / "completed").mkdir(exist_ok=True)
        except OSError:
            logger.exception("Failed to create plugin folder structure: %s",
                             self.shared_folder)

    def prepare_task(self, task_id: str, prompt: str, files: dict | None,
                     working_directory: str | None, context: dict | None,
                     timeout_seconds: int) -> None:
        """Create task folder, write config.json, prompt.txt, and workspace files."""
        task_dir = self.shared_folder / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        # Determine workspace path
        if working_directory:
            workspace_path = working_directory
        else:
            workspace = task_dir / "workspace"
            workspace.mkdir(exist_ok=True)
            workspace_path = str(workspace)

        # Write individual files into workspace
        if files:
            ws = Path(workspace_path)
            for rel_path, content in files.items():
                file_path = ws / rel_path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    file_path.write_bytes(content)
                else:
                    file_path.write_text(str(content))

        # Write config.json
        config_data = {
            "task_id": task_id,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "timeout_seconds": timeout_seconds,
            "working_directory": workspace_path,
            "context": context or {},
            "metadata": {
                "initiated_by": "carpenter",
                "plugin": self.plugin_name,
            },
        }

        config_path = task_dir / "config.json"
        self._write_and_sync(config_path, json.dumps(config_data, indent=2))

        # Write prompt as separate file (not CLI arg, not in config.json)
        prompt_path = task_dir / "prompt.txt"
        self._write_and_sync(prompt_path, prompt)

        logger.info("Prepared task %s for plugin %s", task_id, self.plugin_name)

    def trigger_task(self, task_id: str) -> None:
        """Create trigger file with checksum of config.json in filename."""
        task_dir = self.shared_folder / task_id
        config_path = task_dir / "config.json"

        # Compute checksum of config.json
        config_bytes = config_path.read_bytes()
        checksum = hashlib.sha256(config_bytes).hexdigest()[:8]

        # Create trigger file: {task_id}-{checksum}.trigger
        trigger_name = f"{task_id}-{checksum}.trigger"
        trigger_path = self.shared_folder / "triggered" / trigger_name
        trigger_path.touch()

        # fsync the triggered directory to ensure the entry is visible
        self._sync_directory(self.shared_folder / "triggered")

        logger.info("Triggered task %s (checksum=%s)", task_id, checksum)

    def is_complete(self, task_id: str) -> bool:
        """Check if the .done file exists for this task."""
        done_path = self.shared_folder / "completed" / f"{task_id}.done"
        return done_path.exists()

    def collect_result(self, task_id: str) -> dict:
        """Collect results from a completed task.

        Returns manifest (file metadata) instead of file contents.
        Use read_workspace_file() to access individual files on demand.
        """
        task_dir = self.shared_folder / task_id

        result = {
            "task_id": task_id,
            "status": "unknown",
            "exit_code": 1,
            "output": "",
            "file_manifest": [],
            "workspace_path": "",
            "duration_seconds": None,
            "error": None,
        }

        # Read result.json
        result_file = task_dir / "result.json"
        if result_file.exists():
            try:
                with open(result_file) as f:
                    result_data = json.load(f)
                result["status"] = result_data.get("status", "unknown")
                result["exit_code"] = result_data.get("exit_code", 1)
                result["duration_seconds"] = result_data.get("duration_seconds")
                result["error"] = result_data.get("error")
            except (json.JSONDecodeError, OSError) as e:
                result["error"] = f"Failed to read result.json: {e}"

        # Read output.txt
        output_file = task_dir / "output.txt"
        if output_file.exists():
            try:
                result["output"] = output_file.read_text()
            except OSError as e:
                result["error"] = f"Failed to read output.txt: {e}"

        # Build file manifest from workspace
        config_path = task_dir / "config.json"
        workspace_path = ""
        if config_path.exists():
            try:
                with open(config_path) as f:
                    task_config = json.load(f)
                workspace_path = task_config.get("working_directory", "")
            except (json.JSONDecodeError, OSError):
                pass

        if workspace_path:
            result["workspace_path"] = workspace_path
            result["file_manifest"] = self._build_manifest(Path(workspace_path))

        # Clean up signal files
        self._cleanup_signals(task_id)

        return result

    def read_workspace_file(self, task_id: str, file_path: str) -> str:
        """Read a specific file from a task's workspace.

        Includes path traversal protection — the resolved path must
        remain under the workspace root.
        """
        task_dir = self.shared_folder / task_id

        # Get workspace path from config
        config_path = task_dir / "config.json"
        if not config_path.exists():
            raise FileNotFoundError(f"No config.json for task {task_id}")

        with open(config_path) as f:
            task_config = json.load(f)

        workspace = Path(task_config.get("working_directory", ""))
        if not workspace.exists():
            raise FileNotFoundError(f"Workspace not found: {workspace}")

        # Resolve and check for path traversal
        target = (workspace / file_path).resolve()
        workspace_resolved = workspace.resolve()

        if not str(target).startswith(str(workspace_resolved) + os.sep) and \
           target != workspace_resolved:
            raise ValueError(f"Path traversal detected: {file_path}")

        if not target.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        if not target.is_file():
            raise ValueError(f"Not a file: {file_path}")

        return target.read_text()

    def get_task_status(self, task_id: str) -> dict:
        """Get current status of a task."""
        task_dir = self.shared_folder / task_id

        if not task_dir.exists():
            return {"task_id": task_id, "status": "not_found"}

        # Check if completed
        done_path = self.shared_folder / "completed" / f"{task_id}.done"
        if done_path.exists():
            result_file = task_dir / "result.json"
            if result_file.exists():
                try:
                    with open(result_file) as f:
                        data = json.load(f)
                    return {
                        "task_id": task_id,
                        "status": data.get("status", "completed"),
                        "exit_code": data.get("exit_code"),
                    }
                except (json.JSONDecodeError, OSError):
                    pass
            return {"task_id": task_id, "status": "completed"}

        # Check if triggered
        triggered_dir = self.shared_folder / "triggered"
        if triggered_dir.exists():
            for f in triggered_dir.iterdir():
                if f.name.startswith(task_id):
                    return {"task_id": task_id, "status": "running"}

        return {"task_id": task_id, "status": "pending"}

    def check_health(self) -> dict:
        """Check watcher health via heartbeat.json."""
        heartbeat_path = self.shared_folder / "heartbeat.json"

        if not heartbeat_path.exists():
            return {"healthy": False, "last_heartbeat": None, "age_seconds": None}

        try:
            with open(heartbeat_path) as f:
                data = json.load(f)

            timestamp_str = data.get("timestamp", "")
            if not timestamp_str:
                return {"healthy": False, "last_heartbeat": None, "age_seconds": None}

            # Strip trailing Z for Python < 3.11 fromisoformat compat
            timestamp_str = timestamp_str.rstrip("Z")
            last_heartbeat = datetime.fromisoformat(timestamp_str)
            # Ensure both sides are comparable: treat naive timestamps as UTC
            if last_heartbeat.tzinfo is None:
                last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - last_heartbeat).total_seconds()

            return {
                "healthy": age < 30,
                "last_heartbeat": timestamp_str,
                "age_seconds": int(age),
            }

        except (json.JSONDecodeError, ValueError, OSError):
            return {"healthy": False, "last_heartbeat": None, "age_seconds": None}

    def get_task_dir(self, task_id: str) -> Path:
        """Return the task directory path."""
        return self.shared_folder / task_id

    def _build_manifest(self, workspace: Path) -> list[dict]:
        """Build a file manifest (metadata only, not contents)."""
        manifest = []
        if not workspace.exists():
            return manifest

        for file_path in workspace.rglob("*"):
            if not file_path.is_file():
                continue
            try:
                stat = file_path.stat()
                manifest.append({
                    "path": str(file_path.relative_to(workspace)),
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime
                    ).isoformat(),
                    "is_binary": self._is_binary(file_path),
                })
            except OSError:
                pass

        return manifest

    def _cleanup_signals(self, task_id: str) -> None:
        """Remove trigger and done files for a task."""
        # Remove trigger file(s)
        triggered_dir = self.shared_folder / "triggered"
        if triggered_dir.exists():
            for f in triggered_dir.iterdir():
                if f.name.startswith(task_id):
                    f.unlink(missing_ok=True)

        # Remove done file
        done_path = self.shared_folder / "completed" / f"{task_id}.done"
        done_path.unlink(missing_ok=True)

    @staticmethod
    def _write_and_sync(path: Path, content: str) -> None:
        """Write a file and fsync to ensure durability."""
        with open(path, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())

    @staticmethod
    def _sync_directory(dir_path: Path) -> None:
        """fsync a directory to ensure new entries are durable."""
        try:
            fd = os.open(str(dir_path), os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)
        except OSError:
            pass  # Best effort — some filesystems don't support dir fsync

    @staticmethod
    def _is_binary(file_path: Path) -> bool:
        """Heuristic check for binary files."""
        try:
            with open(file_path, "rb") as f:
                chunk = f.read(8192)
                return b"\x00" in chunk
        except OSError:
            return False
