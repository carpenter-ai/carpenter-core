"""Abstract base class for connector transports."""

from abc import ABC, abstractmethod
from pathlib import Path


class Transport(ABC):
    """Base class for transport implementations.

    A transport handles the IPC mechanism between Carpenter and an
    external tool. Different transports use different communication
    methods (file watching, sockets, HTTP, etc.).
    """

    @abstractmethod
    def prepare_task(self, task_id: str, prompt: str, files: dict | None,
                     working_directory: str | None, context: dict | None,
                     timeout_seconds: int) -> None:
        """Prepare a task for execution by the external tool."""

    @abstractmethod
    def trigger_task(self, task_id: str) -> None:
        """Signal the external tool that a task is ready for execution."""

    @abstractmethod
    def is_complete(self, task_id: str) -> bool:
        """Check whether a task has completed."""

    @abstractmethod
    def collect_result(self, task_id: str) -> dict:
        """Collect results from a completed task."""

    @abstractmethod
    def read_workspace_file(self, task_id: str, file_path: str) -> str:
        """Read a specific file from a task's workspace."""

    @abstractmethod
    def get_task_status(self, task_id: str) -> dict:
        """Get current status of a task."""

    @abstractmethod
    def check_health(self) -> dict:
        """Check health of the external watcher."""

    @abstractmethod
    def get_task_dir(self, task_id: str) -> Path:
        """Return the task directory path."""
