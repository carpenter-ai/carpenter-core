"""Platform time tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""

from datetime import datetime, timezone
from carpenter_tools.tool_meta import tool


@tool(local=True, readonly=True, side_effects=False)
def current_time():
    """Return current UTC time and platform identifier.

    Returns:
        dict: Contains 'timestamp' (ISO 8601 UTC string) and 'platform' (Carpenter)
    """
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": "Carpenter"
    }
