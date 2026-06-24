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
import sqlite3
from datetime import datetime, timezone

import yaml

from ...db import get_db, db_connection, db_transaction

logger = logging.getLogger(__name__)


def _parse_steps_json(raw_json: str) -> tuple[list, list, list]:
    """Parse steps_json, handling legacy list and dict formats.

    Returns (steps, capabilities, triggers). The older formats default
    the missing fields to empty lists so previously stored templates
    continue to deserialise.
    """
    parsed = json.loads(raw_json)
    if isinstance(parsed, list):
        # Legacy format: plain list of steps.
        return parsed, [], []
    return (
        parsed.get("steps", []),
        parsed.get("capabilities", []),
        parsed.get("triggers", []),
    )


def load_template(
    yaml_path: str,
    *,
    owner_package: str | None = None,
    db_conn: sqlite3.Connection | None = None,
) -> int:
    """Load a workflow template from a YAML file.

    Parses the YAML, stores or updates the template in the
    workflow_templates table. If a template with the same name already
    exists, updates it and increments the version.

    Args:
        yaml_path: Path to the template YAML file.
        owner_package: When the template is shipped by a capability
            package, the package's name. Recorded on the template so
            that instantiation can stamp the package's per-arc grant
            (``pkg.<owner>``) onto every step arc (see
            :func:`instantiate_template`). Platform-shipped templates
            leave this NULL and receive no grant.
        db_conn: Optional existing DB connection. Callers that are
            already inside a ``db_transaction()`` on the same thread
            (e.g. the daemon's startup package discovery, which wraps
            ``discover_and_register`` in a transaction) MUST pass their
            connection so this function reuses it rather than opening a
            nested ``db_transaction()`` -- which trips the same-thread
            deadlock guard in ``carpenter.db`` and strands the template.

    Returns the template ID.
    """
    with open(yaml_path, "r") as f:
        data = yaml.safe_load(f)

    name = data["name"]
    description = data.get("description", "")
    required_for = data.get("required_for", [])
    steps = data.get("steps", [])
    capabilities = data.get("capabilities", [])
    triggers = data.get("triggers", [])

    required_for_json = json.dumps(required_for)
    # Store as dict with steps, template-level capabilities, and triggers.
    steps_json = json.dumps({
        "steps": steps,
        "capabilities": capabilities,
        "triggers": triggers,
    })

    def _do(db: sqlite3.Connection) -> int:
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
                "steps_json = ?, version = ?, owner_package = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    description, yaml_path, required_for_json,
                    steps_json, new_version, owner_package, now, existing["id"],
                ),
            )
            return existing["id"]
        cursor = db.execute(
            "INSERT INTO workflow_templates "
            "(name, description, yaml_path, required_for_json, "
            " steps_json, version, owner_package, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name, description, yaml_path, required_for_json,
                steps_json, 1, owner_package, now,
            ),
        )
        return cursor.lastrowid

    if db_conn is not None:
        # Reuse the caller's active transaction; the caller owns commit.
        return _do(db_conn)
    with db_transaction() as db:
        return _do(db)


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
        (
            result["steps"],
            result["capabilities"],
            result["triggers"],
        ) = _parse_steps_json(result["steps_json"])
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
            d["steps"], d["capabilities"], d["triggers"] = _parse_steps_json(d["steps_json"])
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
                result["steps"], result["capabilities"], result["triggers"] = _parse_steps_json(result["steps_json"])
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
    # Capability-package grant stamping: a template shipped by a
    # capability package carries that package's name in ``owner_package``.
    # Every step arc it instantiates is stamped with the package's per-arc
    # grant (``pkg.<owner>``) so the arcs in this pipeline — including the
    # EXECUTOR child that calls ``dispatch(<verb>)`` — pass the per-package
    # dispatch gate (carpenter/executor/dispatch_bridge.py). The grant is
    # scoped to this package's own templates: platform-shipped templates
    # (owner_package NULL) and other packages' arcs never receive it.
    owner_package = template.get("owner_package")
    owner_grant_caps: list[str] = []
    if owner_package:
        from ...packages.capabilities import capability_grant_for_package
        owner_grant_caps = [capability_grant_for_package(owner_package)]
    arc_ids = []

    for step in steps:
        # Pass through optional arc properties from the step definition
        extra_kwargs = {}
        for step_key in ("agent_type", "integrity_level", "output_type",
                         "model", "model_role", "agent_role", "arc_role",
                         "agent_model", "model_policy_id", "output_contract"):
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
            step_role=step.get("role"),
            from_template=True,
            template_mutable=step.get("mutable", False),
            step_order=step.get("order", 0),
            **extra_kwargs,
        )
        arc_ids.append(arc_id)

        # Merge template-level + step-level capabilities and persist.
        # Capability-package grant (``pkg.<owner>``) is added for every
        # step arc when the template is package-owned, so the EXECUTOR
        # child can invoke the package's registered capability verbs.
        step_capabilities = step.get("capabilities", [])
        merged_caps = sorted(
            set(template_capabilities)
            | set(step_capabilities)
            | set(owner_grant_caps)
        )
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

        # Persist dispatch-time goal-rendering config. When set, the arc
        # dispatch handler renders ``goal_template`` against the named
        # output of a preceding sibling arc (resolved by step role) and
        # uses the rendered string as the agent goal — replacing the
        # arc row's ``goal`` column only for the duration of the agent
        # invocation. See ``dispatch_handler.handle_arc_dispatch``.
        goal_template = step.get("goal_template")
        if goal_template:
            goal_cfg = {
                "template": goal_template,
                "subdir": step.get("goal_template_subdir", ""),
                "sibling_role": step.get("goal_input_sibling_role"),
                "output_name": step.get("goal_input_output_name"),
                "input_field": step.get("goal_input_field"),
            }
            with db_transaction() as db:
                db.execute(
                    "INSERT OR REPLACE INTO arc_state (arc_id, key, value_json) "
                    "VALUES (?, ?, ?)",
                    (arc_id, "_goal_template_config", json.dumps(goal_cfg)),
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

    Flat ``.yaml`` / ``.yml`` files are loaded as before. Subdirectories
    are treated as template packages: every YAML file in the package is
    loaded, and if the package contains an ``__init__.py`` with a
    ``register_handlers(registry)`` function, it is imported and called
    with the engine's handler_registry module. This lets templates ship
    Python step handlers alongside their YAML.

    Returns the count of templates loaded.
    """
    count = 0
    for name in sorted(os.listdir(dir_path)):
        full = os.path.join(dir_path, name)
        if os.path.isfile(full) and name.endswith((".yaml", ".yml")):
            load_template(full)
            count += 1
        elif os.path.isdir(full) and not name.startswith((".", "_")):
            # Only treat a subdir as a template package if it declares
            # itself as one via __init__.py. This keeps the loader safe
            # when templates_dir sits next to unrelated directories
            # (tools, prompts, etc. — common in test fixtures and in
            # ad-hoc operator layouts).
            if os.path.isfile(os.path.join(full, "__init__.py")):
                count += _load_template_package(full, name)
    return count


def _load_template_package(pkg_dir: str, pkg_name: str) -> int:
    """Load every YAML in a package dir and invoke its handler hook.

    Returns the count of YAML files loaded.
    """
    yaml_files = sorted(
        f for f in os.listdir(pkg_dir) if f.endswith((".yaml", ".yml"))
    )
    count = 0
    for yaml_file in yaml_files:
        load_template(os.path.join(pkg_dir, yaml_file))
        count += 1

    init_path = os.path.join(pkg_dir, "__init__.py")
    if os.path.isfile(init_path):
        _import_package_and_register(pkg_name, pkg_dir, init_path)
    return count


_TEMPLATE_PKG_ROOT = "carpenter_template_packages"


def _ensure_pkg_root_in_sys_modules() -> None:
    """Register a synthetic root namespace for template packages.

    Template packages are loaded via :func:`importlib.util.spec_from_file_location`
    under the dotted name ``carpenter_template_packages.<pkg_name>``. When
    code inside a loaded package performs a deferred relative import
    (e.g. ``from . import sibling`` inside a step handler called after
    startup), Python's import machinery walks the parent chain from the
    root. Without a ``carpenter_template_packages`` entry in
    ``sys.modules``, that walk raises ``ModuleNotFoundError``. Inserting
    a module-shaped namespace object once, up front, is enough to make
    deferred relative imports inside template packages resolve.
    """
    import sys
    import types

    if _TEMPLATE_PKG_ROOT in sys.modules:
        return
    root = types.ModuleType(_TEMPLATE_PKG_ROOT)
    root.__path__ = []  # mark as a (namespace) package
    sys.modules[_TEMPLATE_PKG_ROOT] = root


def _import_package_and_register(
    pkg_name: str, pkg_dir: str, init_path: str,
) -> None:
    """Import a template package by file path and call register_handlers."""
    import importlib.util
    import sys

    from . import handler_registry

    _ensure_pkg_root_in_sys_modules()

    module_name = f"{_TEMPLATE_PKG_ROOT}.{pkg_name}"
    spec = importlib.util.spec_from_file_location(
        module_name, init_path,
        submodule_search_locations=[pkg_dir],
    )
    if spec is None or spec.loader is None:
        logger.warning(
            "Template package %r: could not build import spec for %s",
            pkg_name, init_path,
        )
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        logger.exception(
            "Template package %r: failed to import %s", pkg_name, init_path,
        )
        sys.modules.pop(module_name, None)
        return

    register_fn = getattr(module, "register_handlers", None)
    if register_fn is None:
        return
    try:
        register_fn(handler_registry)
        logger.info("Template package %r: handlers registered", pkg_name)
    except Exception:
        logger.exception(
            "Template package %r: register_handlers raised", pkg_name,
        )


def collect_template_triggers() -> list[dict]:
    """Gather ``triggers`` declarations from every loaded template.

    Each trigger entry is a subscription config fragment (same shape
    accepted by ``subscriptions.load_subscriptions``). Templates declare
    their triggers under a top-level ``triggers:`` key in their YAML so
    that the coordinator no longer has to enumerate feature-specific
    cadences in Python.

    Subscription ``name`` is auto-namespaced as
    ``{template_name}:{trigger_name}`` when the trigger does not already
    include a colon, so two templates cannot accidentally clash on a
    shared short name.

    Returns a flat list of subscription configs, ready to hand to
    ``subscriptions.load_subscriptions``.
    """
    configs: list[dict] = []
    for tmpl in list_templates():
        tmpl_name = tmpl["name"]
        for trigger in tmpl.get("triggers", []) or []:
            cfg = dict(trigger)
            raw_name = cfg.get("name") or f"trigger-{len(configs)}"
            if ":" not in raw_name:
                cfg["name"] = f"{tmpl_name}:{raw_name}"
            configs.append(cfg)
    return configs


def load_template_triggers() -> int:
    """Register subscriptions declared in every loaded template's ``triggers:``.

    Thin wrapper that calls :func:`collect_template_triggers` and hands
    the result to :func:`subscriptions.load_subscriptions`. Returns the
    number of subscriptions loaded.
    """
    from . import subscriptions

    configs = collect_template_triggers()
    if not configs:
        return 0
    return subscriptions.load_subscriptions(configs)
