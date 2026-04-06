"""Read-only arc tools. Tier 1: callback to platform."""
from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=True, side_effects=False)
def get(arc_id: int) -> dict | None:
    """Get an arc by ID."""
    result = callback("arc.get", {"arc_id": arc_id})
    return result.get("arc")


@tool(local=True, readonly=True, side_effects=False)
def get_children(arc_id: int) -> list[dict]:
    """Get children of an arc."""
    result = callback("arc.get_children", {"arc_id": arc_id})
    return result["children"]


@tool(local=True, readonly=True, side_effects=False)
def get_history(arc_id: int) -> list[dict]:
    """Get history log of an arc."""
    result = callback("arc.get_history", {"arc_id": arc_id})
    return result["history"]


@tool(local=True, readonly=True, side_effects=False)
def get_plan(arc_id: int) -> dict | None:
    """Get structural-only arc data (safe for planners). No execution data."""
    result = callback("arc.get_plan", {"arc_id": arc_id})
    return result.get("arc")


@tool(local=True, readonly=True, side_effects=False)
def get_children_plan(arc_id: int) -> list[dict]:
    """Get structural-only data for children (safe for planners)."""
    result = callback("arc.get_children_plan", {"arc_id": arc_id})
    return result["children"]


@tool(local=True, readonly=True, side_effects=False, trusted_output=False)
def read_output_UNTRUSTED(arc_id: int) -> dict:
    """Read full arc data + history + state. Taint-gated: only tainted/review arcs."""
    return callback("arc.read_output_UNTRUSTED", {"arc_id": arc_id})


@tool(local=True, readonly=True, side_effects=False, trusted_output=False,
      param_types={"key": "Label"})
def read_state_UNTRUSTED(arc_id: int, key: str, default=None):
    """Cross-arc state read. Taint-gated: only tainted/review arcs."""
    result = callback("arc.read_state_UNTRUSTED", {"arc_id": arc_id, "key": key, "default": default})
    return result.get("value", default)
