"""System information utilities - read-only tool."""

import socket
import sys
from carpenter_tools.tool_meta import tool


@tool(local=True, readonly=True, side_effects=False)
def system_info():
    """Return system information including hostname and Python version.

    Returns:
        dict: Contains 'hostname' (system hostname) and 'python_version' (Python version string)
    """
    return {
        "hostname": socket.gethostname(),
        "python_version": sys.version
    }
