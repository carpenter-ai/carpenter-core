"""Root arc failure handling and escalation.

Handles what happens when a root arc (no parent) fails: tries
policy-aware escalation, falls back to legacy stack-based escalation,
and notifies humans when no escalation path exists.
"""

import json
import logging
import sqlite3

from ...db import db_transaction

logger = logging.getLogger(__name__)


def _escalate_arc(arc_id: int, next_model: str) -> int | None:
    """Create an escalated sibling arc with a stronger model.

    - If root arc (no parent): creates a new root arc with next_model
    - If child arc: creates a sibling with same step_order + parent_id
    - Marks original arc as 'escalated'
    - Stores _escalated_from metadata

    Returns the new arc ID, or None on failure.
    """
    from . import manager as arc_manager

    arc = arc_manager.get_arc(arc_id)
    if arc is None:
        return None

    # Build a hard-pinned model policy for the escalated model
    new_policy_id = arc_manager.get_or_create_model_policy(model=next_model)

    # Create the escalated arc
    new_arc_id = arc_manager.create_arc(
        name=f"{arc['name']} (escalated)",
        goal=arc["goal"],
        parent_id=arc["parent_id"],
        step_order=arc["step_order"],
        model_policy_id=new_policy_id,
        agent_type=arc["agent_type"],
        integrity_level=arc["integrity_level"],
        output_type=arc["output_type"],
    )

    # Mark original as escalated
    try:
        arc_manager.update_status(arc_id, "escalated")
    except ValueError:
        logger.warning("Could not transition arc %d to escalated", arc_id)

    # Store escalation metadata
    with db_transaction() as db:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
            (new_arc_id, "_escalated_from", json.dumps(arc_id)),
        )

    # Grant read access so escalated arc can inspect predecessor
    try:
        arc_manager.grant_read_access(
            new_arc_id, arc_id,
            depth="subtree",
            reason="Platform escalation",
            granted_by="platform",
        )
    except (ValueError, sqlite3.Error) as _exc:
        logger.exception("Failed to grant read access during escalation %d -> %d", arc_id, new_arc_id)

    logger.info(
        "Escalated arc %d -> %d (model: %s)",
        arc_id, new_arc_id, next_model,
    )
    return new_arc_id


def escalate_to_next_model(arc_id: int) -> int | None:
    """Escalate an arc to the next model in its task-type chain.

    Looks up the arc's current model (from its model policy), finds the
    next model in the "general" escalation stack via the model resolver,
    and creates an escalated sibling via :func:`_escalate_arc`.

    Returns:
        The new escalated arc ID, or ``None`` if no next model is
        available (top of stack), the arc cannot be loaded, or escalation
        otherwise fails. Callers should treat ``None`` as "no escalation
        possible" and fall through to their normal failure handling.
    """
    from ...agent.model_resolver import get_model_for_role, get_next_model
    from . import manager as arc_manager

    arc = arc_manager.get_arc(arc_id)
    if arc is None:
        return None

    current_model = None
    policy_id = arc.get("model_policy_id")
    if policy_id:
        policy = arc_manager.get_model_policy(policy_id)
        if policy:
            current_model = policy.get("model")
    if not current_model:
        current_model = get_model_for_role("default_step")

    next_model = get_next_model(current_model, "general")
    if not next_model:
        return None

    return _escalate_arc(arc_id, next_model)


def _handle_root_failure(arc_id: int) -> None:
    """Handle failure of a root arc (no parent).

    Two escalation paths:
    1. Policy-aware: If arc has model_policy_id with policy_json, creates
       escalated sibling with min_quality bumped by 1.
    2. Legacy stack: Checks escalation.stacks config for hardcoded model chains.

    Falls back to notifying human if no escalation path exists.

    Note: Coding-change arcs are excluded from escalation because they have
    specialized workflow requirements (workspace, review, apply) that don't
    transfer to escalated arcs. A failed coding-change indicates workflow
    failure, not insufficient model quality.
    """
    from ... import config as _config
    from . import manager as arc_manager

    arc = arc_manager.get_arc(arc_id)
    if arc is None:
        return

    # Skip escalation for coding-change arcs — they have specialized workflows
    # that don't benefit from model escalation. If a coding-change fails, it
    # indicates a workflow problem (missing workspace, dirty tree, etc.), not
    # an AI quality issue.
    from . import CODING_CHANGE_PREFIX
    arc_name = arc.get("name", "")
    if arc_name.startswith(CODING_CHANGE_PREFIX):
        logger.info(
            "Skipping escalation for coding-change arc %d (workflow-specific failure)",
            arc_id,
        )
        try:
            from .. import notifications
            notifications.notify(
                f"Coding-change arc #{arc_id} failed. "
                f"This typically indicates a workflow issue rather than model quality. "
                f"Check logs for details.",
                priority="normal",
                category="coding_change_failure",
            )
        except Exception:  # broad catch: notification delivery may raise anything
            logger.exception("Failed to send coding-change failure notification")
        return

    # Try policy-aware escalation first
    policy_id = arc.get("model_policy_id")
    if policy_id is not None:
        policy_row = arc_manager.get_model_policy(policy_id)
        if policy_row and policy_row.get("policy_json"):
            try:
                escalated = _policy_aware_escalation(arc_id, arc, policy_row)
                if escalated:
                    return
            except (ImportError, KeyError, ValueError, sqlite3.Error) as _exc:
                logger.exception("Policy-aware escalation failed for arc %d, trying legacy", arc_id)

    # Legacy escalation via stacks config
    escalation_config = _config.CONFIG.get("escalation", {})
    stacks = escalation_config.get("stacks", {})

    if not stacks:
        try:
            from .. import notifications
            notifications.notify(
                f"Root arc #{arc_id} '{arc['name']}' failed with no escalation path.",
                priority="normal",
                category="root_failure",
            )
        except Exception:  # broad catch: notification delivery may raise anything
            logger.exception("Failed to send root failure notification for arc %d", arc_id)
        return

    # Find current model from model policy
    current_model = None
    policy_id = arc.get("model_policy_id")
    if policy_id:
        policy = arc_manager.get_model_policy(policy_id)
        if policy:
            current_model = policy.get("model")

    if not current_model:
        from ...agent.model_resolver import get_model_for_role
        current_model = get_model_for_role("chat")

    # Find a stack that contains the current model
    next_model = None
    for stack_name, stack_models in stacks.items():
        if current_model in stack_models:
            idx = stack_models.index(current_model)
            if idx + 1 < len(stack_models):
                next_model = stack_models[idx + 1]
            break

    if next_model is None:
        try:
            from .. import notifications
            notifications.notify(
                f"Root arc #{arc_id} '{arc['name']}' failed at top of escalation stack.",
                priority="urgent",
                category="root_failure",
            )
        except Exception:  # broad catch: notification delivery may raise anything
            logger.exception("Failed to send escalation notification for arc %d", arc_id)
        return

    new_arc_id = _escalate_arc(arc_id, next_model)
    if new_arc_id is None:
        logger.error("Failed to escalate arc %d", arc_id)


def _policy_aware_escalation(arc_id: int, arc: dict, policy_row: dict) -> bool:
    """Escalate an arc by bumping min_quality in its model policy.

    Creates an escalated sibling with min_quality incremented by 1
    in the policy constraints. If already at max quality (5), returns False.

    Args:
        arc_id: Original arc ID.
        arc: Arc dict.
        policy_row: Model policy dict with policy_json.

    Returns:
        True if escalation succeeded, False if no escalation possible.
    """
    from ..models.selector import ModelPolicy, select_model
    from . import manager as arc_manager

    policy = ModelPolicy.from_db_row(policy_row)
    constraints = policy.constraints
    if constraints is None:
        from ..models.selector import PolicyConstraints
        constraints = PolicyConstraints()

    current_min = constraints.min_quality
    if current_min >= 5:
        return False  # Already at max quality

    # Bump min_quality
    new_min = current_min + 1
    constraints.min_quality = new_min
    policy.constraints = constraints

    # Check if any model qualifies with the new constraints
    result = select_model(policy)
    if result is None:
        return False  # No model available at higher quality

    # Create escalated policy
    new_policy_json = policy.to_policy_json()
    new_policy_id = arc_manager.get_or_create_model_policy(
        model=None,
        agent_role=policy_row.get("agent_role"),
        temperature=policy_row.get("temperature"),
        max_tokens=policy_row.get("max_tokens"),
        policy_json=new_policy_json,
        name=f"{policy_row.get('name', '')} (escalated q>={new_min})",
    )

    # Create the escalated arc
    new_arc_id = arc_manager.create_arc(
        name=f"{arc['name']} (escalated q>={new_min})",
        goal=arc["goal"],
        parent_id=arc["parent_id"],
        step_order=arc["step_order"],
        model_policy_id=new_policy_id,
        agent_type=arc["agent_type"],
        integrity_level=arc["integrity_level"],
        output_type=arc["output_type"],
    )

    # Mark original as escalated
    try:
        arc_manager.update_status(arc_id, "escalated")
    except ValueError:
        logger.warning("Could not transition arc %d to escalated", arc_id)

    # Store escalation metadata
    with db_transaction() as db:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
            (new_arc_id, "_escalated_from", json.dumps(arc_id)),
        )

    # Grant read access so escalated arc can inspect predecessor
    try:
        arc_manager.grant_read_access(
            new_arc_id, arc_id,
            depth="subtree",
            reason="Policy-aware escalation",
            granted_by="platform",
        )
    except (ValueError, sqlite3.Error) as _exc:
        logger.exception("Failed to grant read access during policy escalation %d -> %d", arc_id, new_arc_id)

    logger.info(
        "Policy-aware escalation: arc %d -> %d (min_quality: %d -> %d, selected: %s)",
        arc_id, new_arc_id, current_min, new_min, result.model_key,
    )
    return True
