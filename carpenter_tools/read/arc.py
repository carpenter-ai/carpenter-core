"""Read-only arc tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""
from ..tool_meta import tool


@tool(local=True, readonly=True, side_effects=False)
def get(arc_id: int) -> dict | None:
    """Get an arc by ID."""
    ...


@tool(local=True, readonly=True, side_effects=False)
def get_children(arc_id: int) -> list[dict]:
    """Get children of an arc."""
    ...


@tool(local=True, readonly=True, side_effects=False)
def get_history(arc_id: int) -> list[dict]:
    """Get history log of an arc."""
    ...


@tool(local=True, readonly=True, side_effects=False)
def get_plan(arc_id: int) -> dict | None:
    """Get structural-only arc data (safe for planners). No execution data."""
    ...


@tool(local=True, readonly=True, side_effects=False)
def get_children_plan(arc_id: int) -> list[dict]:
    """Get structural-only data for children (safe for planners)."""
    ...
