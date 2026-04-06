"""Shared utilities for the engine package."""

import json


def filter_matches(event_filter, payload: dict) -> bool:
    """Check if a filter matches an event payload.

    A filter matches if all key-value pairs in the filter are present
    in the payload. None filter matches everything.

    Accepts either a dict or a JSON string (for event_bus compatibility
    where filters are stored as JSON in the database).
    """
    if event_filter is None:
        return True

    # If it's a JSON string, parse it first
    if isinstance(event_filter, str):
        try:
            event_filter = json.loads(event_filter)
        except (json.JSONDecodeError, TypeError):
            return True

    for key, value in event_filter.items():
        if key not in payload or payload[key] != value:
            return False
    return True
