"""Shared utilities for the engine package."""

import json


def filter_matches(event_filter, payload: dict) -> bool:
    """Check if a filter matches an event payload.

    A filter matches if all key-value pairs in the filter are present
    in the payload. None filter matches everything.

    Values may be scalars (checked for equality) or dict operators:

    - ``{"$ne": value}`` — match when the payload's value is not equal
      to ``value``. Also matches when the key is absent from the
      payload (absent ≠ present).
    - ``{"$starts_with": prefix}`` — match when the payload's value is
      a string that begins with ``prefix``. Missing keys or non-string
      values never match. Useful for KB / repo / URL path filters.
    - ``{"$is_null": true}`` — match when the key is absent or its
      value is ``None``. ``{"$is_null": false}`` matches when the key
      is present with a non-``None`` value. Useful for distinguishing
      "agent wrote this" from "autogen wrote this" without needing a
      separate boolean field.

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
        if isinstance(value, dict) and len(value) == 1:
            (op_name, op_val), = value.items()
            if op_name == "$ne":
                # Not-equals operator. Absent key counts as not-equal.
                if payload.get(key) == op_val:
                    return False
                continue
            if op_name == "$starts_with":
                payload_val = payload.get(key)
                if not isinstance(payload_val, str):
                    return False
                if not payload_val.startswith(op_val):
                    return False
                continue
            if op_name == "$is_null":
                is_null = payload.get(key) is None
                if bool(op_val) != is_null:
                    return False
                continue
            # Unknown operator — treat as a literal equality comparison
            # to stay backward compatible with filters we haven't seen.
        if key not in payload or payload[key] != value:
            return False
    return True
