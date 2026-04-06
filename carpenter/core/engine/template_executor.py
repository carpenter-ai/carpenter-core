"""Template-driven workflow execution.

Provides helpers for handlers to execute workflow steps based on templates.
"""

import logging
from typing import Any

from ... import config
from ...db import get_db, db_connection

logger = logging.getLogger(__name__)


def _extract_steps_list(raw) -> list[dict]:
    """Extract the steps list from parsed steps_json.

    steps_json may be stored as either:
      - A bare list: [{"name": "step1", ...}, ...]
      - A dict with a "steps" key: {"steps": [...], "capabilities": [...]}

    Returns the list of step dicts in either case.
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return raw.get("steps", [])
    return []


def get_template_for_workflow(workflow_type: str) -> dict | None:
    """Look up template for a workflow type from config.

    Args:
        workflow_type: Workflow type (e.g. "coding-change", "writing-repo-change")

    Returns:
        Template dict with id, name, steps, etc. or None if not found
    """
    # Get template name from config
    workflow_templates = config.CONFIG.get("workflow_templates", {})
    template_name = workflow_templates.get(workflow_type)

    if not template_name:
        logger.debug("No template configured for workflow type: %s", workflow_type)
        return None

    # Look up template in database
    with db_connection() as db:
        row = db.execute(
            "SELECT * FROM workflow_templates WHERE name = ?",
            (template_name,)
        ).fetchone()

        if not row:
            logger.warning(
                "Template '%s' configured for workflow '%s' but not found in database",
                template_name, workflow_type
            )
            return None

        return dict(row)


def get_step_config(template_id: int | None, step_name: str) -> dict | None:
    """Get configuration for a specific step in a template.

    Args:
        template_id: Template ID from workflow_templates table
        step_name: Step name (e.g. "invoke-agent", "verify-quality")

    Returns:
        Step config dict with name, description, model_policy, etc. or None
    """
    if template_id is None:
        return None

    with db_connection() as db:
        row = db.execute(
            "SELECT steps_json FROM workflow_templates WHERE id = ?",
            (template_id,)
        ).fetchone()

        if not row:
            return None

        import json
        steps = _extract_steps_list(json.loads(row["steps_json"]))

        # Find the step by name
        for step in steps:
            if step.get("name") == step_name:
                return step

        return None


def get_verification_steps(template_id: int | None) -> list[dict]:
    """Get all verification steps from a template.

    Args:
        template_id: Template ID from workflow_templates table

    Returns:
        List of step dicts with arc_role="verifier"
    """
    if template_id is None:
        return []

    with db_connection() as db:
        row = db.execute(
            "SELECT steps_json FROM workflow_templates WHERE id = ?",
            (template_id,)
        ).fetchone()

        if not row:
            return []

        import json
        steps = _extract_steps_list(json.loads(row["steps_json"]))

        # Filter to verification steps
        return [s for s in steps if s.get("arc_role") == "verifier"]


def get_model_policy_for_step(
    template_id: int | None,
    step_name: str,
    fallback: str | None = None
) -> int | None:
    """Get model_policy_id for a template step.

    Args:
        template_id: Template ID
        step_name: Step name
        fallback: Fallback policy name if step doesn't specify one

    Returns:
        model_policy ID or None
    """
    from ..arcs import manager as arc_manager  # deferred to avoid circular import with arcs subpackage

    step = get_step_config(template_id, step_name)
    if not step:
        if fallback:
            return arc_manager.get_policy_id_by_name(fallback)
        return None

    policy_name = step.get("model_policy")
    if not policy_name:
        if fallback:
            return arc_manager.get_policy_id_by_name(fallback)
        return None

    return arc_manager.get_policy_id_by_name(policy_name)
