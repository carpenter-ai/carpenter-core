"""Template manager for Carpenter.

Handles loading workflow templates from YAML files, storing them in the
database, and instantiating them as child arcs on a parent arc.

Templates define rigid workflow structures: ordered steps that must be
followed. Once instantiated, template-mandated arcs cannot be deleted
or reordered without violating rigidity.

Key invariants:
- Template names are unique; re-loading bumps the version
- Instantiation creates one child arc per template step
- Steps with activation_event get registered in arc_activations
- Rigidity validation ensures template arcs remain intact
"""

import json
import logging
import os
from datetime import datetime, timezone

import yaml

from ...db import get_db, db_connection, db_transaction

logger = logging.getLogger(__name__)


def _parse_steps_json(raw_json: str) -> tuple[list, list]:
    """Parse steps_json, handling both old list and new dict formats.

    Returns (steps, capabilities) where capabilities defaults to []
    for templates stored before the capability grant feature.
    """
    parsed = json.loads(raw_json)
    if isinstance(parsed, list):
        # Legacy format: plain list of steps, no template capabilities
        return parsed, []
    # New format: {"steps": [...], "capabilities": [...]}
    return parsed.get("steps", []), parsed.get("capabilities", [])


def load_template(yaml_path: str) -> int:
    """Load a workflow template from a YAML file.

    Parses the YAML, stores or updates the template in the
    workflow_templates table. If a template with the same name already
    exists, updates it and increments the version.

    Returns the template ID.

    Raises:
        ValueError: If any step declares an ``untrusted_shape`` that is
            unknown or that conflicts with other step-level fields.
            Validation happens at load time so authorship errors surface
            before a workflow is ever instantiated.
    """
    from ..trust.untrusted_shapes import validate_step_against_shape

    with open(yaml_path, "r") as f:
        data = yaml.safe_load(f)

    name = data["name"]
    description = data.get("description", "")
    required_for = data.get("required_for", [])
    steps = data.get("steps", [])
    capabilities = data.get("capabilities", [])

    # Validate any ``untrusted_shape`` declarations up front.
    for step in steps:
        if isinstance(step, dict) and step.get("untrusted_shape"):
            validate_step_against_shape(step)

    required_for_json = json.dumps(required_for)
    # Store as dict with steps + template-level capabilities
    steps_json = json.dumps({"steps": steps, "capabilities": capabilities})

    with db_transaction() as db:
        existing = db.execute(
            "SELECT id, version FROM workflow_templates WHERE name = ?",
            (name,),
        ).fetchone()

        now = datetime.now(timezone.utc).isoformat()

        if existing:
            new_version = existing["version"] + 1
            db.execute(
                "UPDATE workflow_templates SET "
                "description = ?, yaml_path = ?, required_for_json = ?, "
                "steps_json = ?, version = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    description, yaml_path, required_for_json,
                    steps_json, new_version, now, existing["id"],
                ),
            )
            template_id = existing["id"]
        else:
            cursor = db.execute(
                "INSERT INTO workflow_templates "
                "(name, description, yaml_path, required_for_json, "
                " steps_json, version, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    name, description, yaml_path, required_for_json,
                    steps_json, 1, now,
                ),
            )
            template_id = cursor.lastrowid

        return template_id


def get_template(
    template_id: int | None = None,
    *,
    name: str | None = None,
) -> dict | None:
    """Get a template by ID or name.

    Exactly one of ``template_id`` or ``name`` must be provided.

    Returns a dict with parsed steps_json, or None if not found.
    """
    if template_id is not None and name is not None:
        raise ValueError("Provide template_id or name, not both")
    if template_id is None and name is None:
        raise ValueError("Provide template_id or name")

    if template_id is not None:
        where, params = "id = ?", (template_id,)
    else:
        where, params = "name = ?", (name,)

    with db_connection() as db:
        row = db.execute(
            f"SELECT * FROM workflow_templates WHERE {where}",
            params,
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["steps"], result["capabilities"] = _parse_steps_json(result["steps_json"])
        result["required_for"] = json.loads(result["required_for_json"]) if result["required_for_json"] else []
        return result


def get_template_by_name(name: str) -> dict | None:
    """Get a template by name (convenience alias for ``get_template(name=...)``).

    Returns a dict with parsed steps_json, or None if not found.
    """
    return get_template(name=name)


def list_templates() -> list[dict]:
    """List all templates.

    Returns a list of dicts with parsed steps_json.
    """
    with db_connection() as db:
        rows = db.execute(
            "SELECT * FROM workflow_templates ORDER BY name"
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            d["steps"], d["capabilities"] = _parse_steps_json(d["steps_json"])
            d["required_for"] = json.loads(d["required_for_json"]) if d["required_for_json"] else []
            results.append(d)
        return results


def find_template_for_resource(resource: str) -> dict | None:
    """Find a template whose required_for list contains the given resource.

    Returns the template dict, or None if no match.
    """
    with db_connection() as db:
        rows = db.execute(
            "SELECT * FROM workflow_templates"
        ).fetchall()
        for row in rows:
            required_for = json.loads(row["required_for_json"]) if row["required_for_json"] else []
            if resource in required_for:
                result = dict(row)
                result["steps"], result["capabilities"] = _parse_steps_json(result["steps_json"])
                result["required_for"] = required_for
                return result
        return None


def _enforce_min_tier(agent_model: str, model_min_tier: str) -> None:
    """Validate that a model's cost_tier meets the minimum tier requirement.

    Args:
        agent_model: Short model identifier (e.g., "haiku").
        model_min_tier: Minimum required cost tier ("low", "medium", "high").

    Raises:
        ValueError: If the model's cost_tier is below model_min_tier.
    """
    from ...agent.model_resolver import get_cost_tier, compare_cost_tiers

    actual_tier = get_cost_tier(agent_model)
    if compare_cost_tiers(actual_tier, model_min_tier) < 0:
        raise ValueError(
            f"Model {agent_model!r} has cost_tier {actual_tier!r} which is below "
            f"the required model_min_tier {model_min_tier!r}"
        )


def _instantiate_untrusted_shape(
    step: dict,
    parent_arc_id: int,
    template_id: int,
    template_capabilities: list,
) -> list[int]:
    """Expand an ``untrusted_shape`` step into a canonical arc batch.

    Renders the shape's child specs, parents them to ``parent_arc_id``,
    and delegates to :func:`carpenter.core.trust.batch.create_untrusted_batch`.
    Step-level fields that the shape does not own (``description``,
    ``capabilities``, ``activation_event``, ``required_pass``,
    ``model_min_tier``) are applied to the child arcs after creation
    using the same conventions as the trusted-step path.
    """
    from ..trust.batch import create_untrusted_batch
    from ..trust.untrusted_shapes import render_shape

    bindings = {
        "goal": step.get("description", "") or step.get("name", ""),
        "name": step.get("name", ""),
    }
    specs = render_shape(step["untrusted_shape"], bindings)

    # Offset child step_orders by the YAML-declared base ``order`` so
    # template-rigidity-style invariants still see a monotonic layout.
    base_order = step.get("order", 0)
    for spec in specs:
        spec["parent_id"] = parent_arc_id
        spec["step_order"] = base_order + int(spec.get("step_order", 0))
        # Tag as template-originated so rigidity checks treat them
        # identically to ordinary template steps.
        spec["template_id"] = template_id
        spec["from_template"] = True
        spec["template_mutable"] = step.get("mutable", False)

    result = create_untrusted_batch(specs, parent_id=parent_arc_id)
    if "error" in result:
        raise ValueError(
            f"Failed to instantiate untrusted_shape "
            f"{step['untrusted_shape']!r}: {result['error']}"
        )

    arc_ids = result["arc_ids"]

    # Merge template-level + step-level capabilities onto each child.
    step_capabilities = step.get("capabilities", [])
    merged_caps = sorted(set(template_capabilities) | set(step_capabilities))
    if merged_caps:
        with db_transaction() as db:
            for arc_id in arc_ids:
                db.execute(
                    "INSERT OR REPLACE INTO arc_state "
                    "(arc_id, key, value_json) VALUES (?, ?, ?)",
                    (arc_id, "_capabilities", json.dumps(merged_caps)),
                )

    # activation_event on a shape step attaches to the first (executor)
    # child, matching the "one event → one entry point" convention.
    activation_event = step.get("activation_event")
    if activation_event:
        with db_transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO arc_activations "
                "(arc_id, event_type) VALUES (?, ?)",
                (arc_ids[0], activation_event),
            )

    return arc_ids


def instantiate_template(template_id: int, parent_arc_id: int) -> list[int]:
    """Instantiate a template as child arcs on a parent arc.

    Creates one child arc per step in the template. Each child arc has
    from_template=True and template_id set. If a step has an
    activation_event, it is registered in the arc_activations table.

    Returns the list of created arc IDs.
    """
    from ..arcs import manager as arc_manager

    template = get_template(template_id)
    if template is None:
        raise ValueError(f"Template {template_id} not found")

    steps = template["steps"]
    template_capabilities = template.get("capabilities", [])
    arc_ids = []

    for step in steps:
        # ``untrusted_shape`` steps expand into a canonical batch of
        # (EXECUTOR-untrusted, reviewer(s), judge) child arcs via the
        # shared trust helper instead of a single create_arc call.
        shape_name = step.get("untrusted_shape")
        if shape_name:
            arc_ids.extend(
                _instantiate_untrusted_shape(
                    step=step,
                    parent_arc_id=parent_arc_id,
                    template_id=template_id,
                    template_capabilities=template_capabilities,
                )
            )
            continue

        # Pass through optional arc properties from the step definition
        extra_kwargs = {}
        for step_key in ("agent_type", "integrity_level", "output_type",
                         "model", "model_role", "agent_role", "arc_role",
                         "agent_model", "model_policy_id"):
            if step_key in step:
                extra_kwargs[step_key] = step[step_key]

        # If model_policy is a preset name string, resolve to policy_id
        model_policy_name = step.get("model_policy")
        if model_policy_name and "model_policy_id" not in extra_kwargs:
            try:
                from ..models.selector import get_presets
                preset = get_presets().get(model_policy_name)
                if preset:
                    policy_json = preset.to_policy_json()
                    extra_kwargs["model_policy_id"] = arc_manager.get_or_create_model_policy(
                        model=preset.model,
                        agent_role=preset.agent_role,
                        temperature=preset.temperature,
                        max_tokens=preset.max_tokens,
                        policy_json=policy_json,
                        name=model_policy_name,
                    )
            except (ImportError, KeyError, ValueError, TypeError) as _exc:
                logger.exception(
                    "Failed to resolve model_policy preset %r", model_policy_name
                )

        # Store model_min_tier in arc_state after creation (validated below)
        model_min_tier = step.get("model_min_tier")

        # Enforce model_min_tier: if an agent_model is specified, its
        # cost_tier must be >= model_min_tier
        if model_min_tier and extra_kwargs.get("agent_model"):
            _enforce_min_tier(extra_kwargs["agent_model"], model_min_tier)

        arc_id = arc_manager.create_arc(
            name=step["name"],
            goal=step.get("description"),
            parent_id=parent_arc_id,
            template_id=template_id,
            from_template=True,
            template_mutable=step.get("mutable", False),
            step_order=step.get("order", 0),
            **extra_kwargs,
        )
        arc_ids.append(arc_id)

        # Merge template-level + step-level capabilities and persist
        step_capabilities = step.get("capabilities", [])
        merged_caps = sorted(set(template_capabilities) | set(step_capabilities))
        if merged_caps:
            with db_transaction() as db:
                db.execute(
                    "INSERT OR REPLACE INTO arc_state (arc_id, key, value_json) "
                    "VALUES (?, ?, ?)",
                    (arc_id, "_capabilities", json.dumps(merged_caps)),
                )

        # Persist model_min_tier as arc_state so planners can read it
        if model_min_tier:
            with db_transaction() as db:
                db.execute(
                    "INSERT OR REPLACE INTO arc_state (arc_id, key, value_json) "
                    "VALUES (?, ?, ?)",
                    (arc_id, "_model_min_tier", json.dumps(model_min_tier)),
                )

        # Persist required_pass as arc_state for review gating
        if step.get("required_pass"):
            with db_transaction() as db:
                db.execute(
                    "INSERT OR REPLACE INTO arc_state (arc_id, key, value_json) "
                    "VALUES (?, ?, ?)",
                    (arc_id, "_required_pass", json.dumps(True)),
                )

        activation_event = step.get("activation_event")
        if activation_event:
            with db_transaction() as db:
                db.execute(
                    "INSERT OR IGNORE INTO arc_activations "
                    "(arc_id, event_type) VALUES (?, ?)",
                    (arc_id, activation_event),
                )

    return arc_ids


def validate_template_rigidity(parent_arc_id: int) -> bool:
    """Validate that template-mandated arcs have not been tampered with.

    Checks that:
    - The parent has a template_id
    - All template steps exist as child arcs with from_template=True
    - The count matches
    - The step_orders match

    Returns True if valid, False if violated.
    """
    with db_connection() as db:
        parent = db.execute(
            "SELECT template_id FROM arcs WHERE id = ?",
            (parent_arc_id,),
        ).fetchone()
        if parent is None:
            return False
        if parent["template_id"] is None:
            return True  # No template, nothing to validate

        template_id = parent["template_id"]

    template = get_template(template_id)
    if template is None:
        return False

    steps = template["steps"]
    expected_orders = sorted(step.get("order", 0) for step in steps)

    with db_connection() as db:
        children = db.execute(
            "SELECT step_order FROM arcs "
            "WHERE parent_id = ? AND from_template = TRUE "
            "ORDER BY step_order",
            (parent_arc_id,),
        ).fetchall()

        if len(children) != len(steps):
            return False

        actual_orders = [child["step_order"] for child in children]
        return actual_orders == expected_orders


def load_templates_from_dir(dir_path: str) -> int:
    """Load all YAML templates from a directory.

    Scans for .yaml and .yml files and calls load_template for each.

    Returns the count of templates loaded.
    """
    count = 0
    for filename in sorted(os.listdir(dir_path)):
        if filename.endswith((".yaml", ".yml")):
            filepath = os.path.join(dir_path, filename)
            load_template(filepath)
            count += 1
    return count
