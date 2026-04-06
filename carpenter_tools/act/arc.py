"""Arc mutation tools. Tier 1: callback to platform."""
from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_types={"name": "Label", "goal": "UnstructuredText", "integrity_level": "Label", "output_type": "Label", "agent_type": "Label", "agent_role": "Label"})
def create(
    name: str,
    goal: str | None = None,
    parent_id: int | None = None,
    integrity_level: str | None = None,
    output_type: str | None = None,
    agent_type: str | None = None,
    model: str | None = None,
    model_role: str | None = None,
    agent_role: str | None = None,
    wait_until: str | None = None,
    output_contract: str | None = None,
) -> int:
    """Create a new arc. Returns arc ID.

    Args:
        integrity_level: 'trusted' (default), 'constrained', or 'untrusted'.
        output_type: 'python' (default), 'text', 'json', or 'unknown'.
        agent_type: 'EXECUTOR' (default), 'PLANNER', 'REVIEWER', or 'CHAT'.
        model: Explicit model string (e.g. 'anthropic:claude-sonnet-4-20250514').
        model_role: Named role slot to resolve model from (e.g. 'default_step').
        agent_role: Named agent role for system prompt (e.g. 'security-reviewer').
        wait_until: ISO datetime string; heartbeat won't dispatch until this time.
        output_contract: Pydantic model reference 'module:ClassName' for output schema.
    """
    params = {"name": name, "goal": goal, "parent_id": parent_id}
    for key, val in [("integrity_level", integrity_level), ("output_type", output_type),
                     ("agent_type", agent_type), ("model", model),
                     ("model_role", model_role), ("agent_role", agent_role),
                     ("wait_until", wait_until), ("output_contract", output_contract)]:
        if val is not None:
            params[key] = val
    result = callback("arc.create", params)
    return result["arc_id"]


@tool(local=True, readonly=False, side_effects=True,
      param_types={"name": "Label", "goal": "UnstructuredText", "integrity_level": "Label", "output_type": "Label", "agent_type": "Label", "agent_role": "Label"})
def add_child(
    parent_id: int,
    name: str,
    goal: str | None = None,
    integrity_level: str | None = None,
    output_type: str | None = None,
    agent_type: str | None = None,
    model: str | None = None,
    model_role: str | None = None,
    agent_role: str | None = None,
    wait_until: str | None = None,
    output_contract: str | None = None,
) -> int:
    """Add a child arc. Returns child arc ID.

    Args:
        integrity_level: 'trusted' (default), 'constrained', or 'untrusted'.
        output_type: 'python' (default), 'text', 'json', or 'unknown'.
        agent_type: 'EXECUTOR' (default), 'PLANNER', 'REVIEWER', or 'CHAT'.
        model: Explicit model string (e.g. 'anthropic:claude-sonnet-4-20250514').
        model_role: Named role slot to resolve model from (e.g. 'default_step').
        agent_role: Named agent role for system prompt (e.g. 'security-reviewer').
        wait_until: ISO datetime string; heartbeat won't dispatch until this time.
        output_contract: Pydantic model reference 'module:ClassName' for output schema.
    """
    params = {"parent_id": parent_id, "name": name, "goal": goal}
    for key, val in [("integrity_level", integrity_level), ("output_type", output_type),
                     ("agent_type", agent_type), ("model", model),
                     ("model_role", model_role), ("agent_role", agent_role),
                     ("wait_until", wait_until), ("output_contract", output_contract)]:
        if val is not None:
            params[key] = val
    result = callback("arc.add_child", params)
    if "hint" in result:
        print(f"[platform hint] {result['hint']}")
    return result["arc_id"]


@tool(local=True, readonly=False, side_effects=True)
def cancel(arc_id: int) -> int:
    """Cancel an arc and descendants. Returns count cancelled."""
    result = callback("arc.cancel", {"arc_id": arc_id})
    return result["cancelled_count"]


@tool(local=True, readonly=False, side_effects=True,
      param_types={"status": "Label"})
def update_status(arc_id: int, status: str) -> None:
    """Update arc status."""
    callback("arc.update_status", {"arc_id": arc_id, "status": status})


@tool(local=True, readonly=False, side_effects=True,
      param_types={"source_dir": "WorkspacePath", "prompt": "UnstructuredText", "coding_agent": "Label"})
def invoke_coding_change(
    source_dir: str = "platform",
    prompt: str = "",
    coding_agent: str | None = None,
) -> int:
    """Start the coding-change workflow on a source directory.

    Creates a coding-change arc, runs the built-in coding agent in an isolated
    workspace, generates a unified diff, and waits for human review before
    applying the changes.

    Args:
        source_dir: Path to the source directory to modify. Use "platform"
            (default) to target the platform's own source code. An absolute
            path can also be provided for external repositories.
        prompt: Description of the coding task / changes required.
        coding_agent: Optional coding agent profile name (default: platform default).

    Returns:
        The new arc ID (int).
    """
    params: dict = {"source_dir": source_dir, "prompt": prompt}
    if coding_agent is not None:
        params["coding_agent"] = coding_agent
    result = callback("arc.invoke_coding_change", params)
    return result["arc_id"]


@tool(local=True, readonly=False, side_effects=True,
      param_types={"model": "Label"})
def request_ai_review(
    target_arc_id: int,
    model: str,
    focus_areas: str | None = None,
) -> int:
    """Request an ad-hoc AI review of a coding-change arc's diff.

    Creates an informational REVIEWER arc that examines the diff and
    workspace, stores findings in arc_state.  Does not trigger automated
    acceptance — the human always makes the final call.

    Args:
        target_arc_id: The coding-change root arc ID.
        model: Short model name ("sonnet", "opus", "haiku").
        focus_areas: Optional focus areas (e.g. "security", "performance").
    Returns: The new reviewer arc ID.
    """
    params = {"target_arc_id": target_arc_id, "model": model}
    if focus_areas is not None:
        params["focus_areas"] = focus_areas
    result = callback("arc.request_ai_review", params)
    return result["arc_id"]


@tool(local=True, readonly=False, side_effects=True,
      param_types={"depth": "Label"})
def grant_read_access(reader_arc_id: int, target_arc_id: int, depth: str = "subtree") -> dict:
    """Grant one arc read access to another arc's state, bypassing ancestor-descendant restriction.

    Args:
        reader_arc_id: The arc that will gain read access.
        target_arc_id: The arc whose state will become readable.
        depth: 'self' for exact arc only, 'subtree' for arc and all descendants.
    """
    return callback("arc.grant_read_access", {
        "reader_arc_id": reader_arc_id,
        "target_arc_id": target_arc_id,
        "depth": depth,
    })


@tool(local=True, readonly=False, side_effects=True)
def create_batch(arcs: list[dict]) -> dict:
    """Create multiple arcs atomically with validation.

    Each arc dict can have: name, goal, parent_id, integrity_level, output_type,
    agent_type, step_order, reviewer_profile.

    Validation rules:
    - All arcs must have same parent_id (or all None)
    - Reviewer/Judge arcs must reference valid reviewer_profile from config
    - Untrusted arcs must have at least one REVIEWER or JUDGE arc in batch
    - Maximum one JUDGE arc per batch
    - JUDGE must have highest step_order among reviewers

    Returns:
        {"arc_ids": [list of created arc IDs]} or {"error": "message"}
    """
    result = callback("arc.create_batch", {"arcs": arcs})
    if "hint" in result:
        print(f"[platform hint] {result['hint']}")
    return result
