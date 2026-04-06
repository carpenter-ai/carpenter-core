"""Platform protocol — unified interface for OS-specific operations.

Each platform (Linux, macOS, Windows, iOS) implements this protocol.
The default is deny/refuse when a capability is unavailable.
"""

from typing import Protocol


class Platform(Protocol):
    """Protocol for platform-specific operations."""

    name: str

    def restart_process(self) -> None:
        """Replace the current process with a fresh copy, or equivalent."""
        ...

    def protect_file(self, path: str) -> None:
        """Make a file owner-readable only (credentials, .env)."""
        ...

    def generate_service(self, name: str, command: list[str],
                         description: str, *, working_dir: str = "",
                         env_file: str = "") -> str | None:
        """Generate a service definition for the host's service manager.

        Returns file contents as a string, or None if not supported.
        """
        ...

    def install_service(self, name: str, service_content: str) -> bool:
        """Install and optionally enable a service definition.

        Returns True if the service was installed successfully.
        """
        ...

    def graceful_kill(self, proc, grace_seconds: int = 5) -> None:
        """Terminate a subprocess with escalation.

        On Unix: SIGTERM -> wait -> SIGKILL.
        On Windows: TerminateProcess.
        """
        ...
