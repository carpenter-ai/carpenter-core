"""Platform abstraction — detect and provide OS-specific implementations.

Use get_platform() to obtain the singleton for the current platform.
"""

import sys

from .base import Platform


def detect_platform() -> str:
    """Return a platform identifier string."""
    if sys.platform == "linux":
        return "linux"
    elif sys.platform == "darwin":
        return "darwin"
    elif sys.platform == "win32":
        return "windows"
    else:
        return sys.platform


_instance: Platform | None = None


def set_platform(platform: Platform) -> None:
    """Inject a platform implementation. Must be called before server starts."""
    global _instance
    _instance = platform


def get_platform() -> Platform:
    """Return the Platform singleton.

    A platform must be injected via set_platform() before calling this.
    Platform packages (e.g. carpenter-linux) call set_platform() at startup.

    Raises:
        RuntimeError: If no platform has been registered.
    """
    if _instance is not None:
        return _instance

    raise RuntimeError(
        "No platform registered. Install a platform package "
        "(e.g. carpenter-linux) and run via its entry point."
    )


__all__ = ["Platform", "detect_platform", "get_platform", "set_platform"]
