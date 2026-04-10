"""Arc tool backend — handles arc CRUD callbacks from executors."""
import json
import logging
import sqlite3
from datetime import datetime, timezone
from ..core.arcs import CODING_CHANGE_PREFIX, manager as arc_manager
from ..core.trust.types import validate_integrity_level, validate_output_type, validate_agent_type
from ..core.trust.audit import log_trust_event
from ..db import get_db, db_connection, db_transaction
from .. import config

logger = logging.getLogger(__name__)

# Import Fernet for encryption key generation
try:
    from cryptography.fernet import Fernet
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False


def handle_create(params: dict) -> dict:
    """Create a new arc. Params: name, goal (opt), parent_id (opt),
    integrity_level (opt), output_type (opt), agent_type (opt),
    model (opt), model_role (opt), agent_role (opt), wait_until (opt),
    model_policy_id (opt), _allow_tainted (opt)."""
    kwargs = {}
    for key in ("integrity_level", "output_type", "agent_type", "model", "model_role",
                "agent_role", "wait_until", "output_contract", "model_policy_id"):
        if key in params:
            kwargs[key] = params[key]

    # Allow tainted arcs when explicitly permitted (e.g., from tainted conversations)
    if params.get("_allow_tainted"):
        kwargs["_allow_tainted"] = True

    arc_id = arc_manager.create_arc(
        name=params["name"],
        goal=params.get("goal"),
        parent_id=params.get("parent_id"),
        **kwargs,
    )
    return {"arc_id": arc_id}


def handle_add_child(params: dict) -> dict:
    """Add a child arc. Params: parent_id, name, goal (opt),
    integrity_level (opt), output_type (opt), agent_type (opt),
    model (opt), model_role (opt), agent_role (opt), wait_until (opt),
    output_contract (opt), model_policy_id (opt), _allow_tainted (opt)."""
    kwargs = {}
    for key in ("integrity_level", "output_type", "agent_type", "model", "model_role",
                "agent_role", "wait_until", "output_contract", "model_policy_id"):
        if key in params:
            kwargs[key] = params[key]

    # Note: add_child internally uses _allow_tainted for tainted children
    parent_id = params["parent_id"]
    child_id = arc_manager.add_child(
        parent_id=parent_id,
        name=params["name"],
        goal=params.get("goal"),
        **kwargs,
    )

    result = {"arc_id": child_id}

    # Platform hint: suggest PLANNER root when adding multiple children to non-PLANNER
    try:
        parent = arc_manager.get_arc(parent_id)
        if parent and parent.get("agent_type") != "PLANNER":
            children = arc_manager.get_children(parent_id)
            if len(children) >= 2:
                result["hint"] = (
                    "This arc has multiple children but is not a PLANNER. "
                    "Consider using a PLANNER root for multi-step work — "
                    "it gives the escalation system a meaningful target "
                    "when children fail. See skills/planner-root in the knowledge base."
                )
    except (sqlite3.Error, KeyError, ValueError) as _exc:
        pass  # Don't fail child creation over hint logic

    return result


def handle_get(params: dict) -> dict:
    """Get arc by ID. Params: arc_id."""
    arc = arc_manager.get_arc(params["arc_id"])
    if arc is None:
        return {"error": "Arc not found"}
    return {"arc": arc}


def handle_get_children(params: dict) -> dict:
    """Get children of arc. Params: arc_id."""
    children = arc_manager.get_children(params["arc_id"])
    return {"children": children}


def handle_get_history(params: dict) -> dict:
    """Get history of arc. Params: arc_id."""
    history = arc_manager.get_history(params["arc_id"])
    return {"history": history}


def handle_cancel(params: dict) -> dict:
    """Cancel an arc and its descendants. Params: arc_id."""
    count = arc_manager.cancel_arc(params["arc_id"], actor=params.get("actor", "agent"))
    return {"cancelled_count": count}


def handle_update_status(params: dict) -> dict:
    """Update arc status. Params: arc_id, status."""
    try:
        arc_manager.update_status(
            params["arc_id"],
            params["status"],
            actor=params.get("actor", "agent"),
        )
        return {"success": True}
    except ValueError as e:
        return {"error": str(e)}


def handle_grant_read_access(params: dict) -> dict:
    """Grant cross-arc read access. Params: reader_arc_id, target_arc_id, depth (opt).

    The caller_arc_id (auto-injected by callbacks.py) is used for authorization:
    arc callers must be able to see both reader and target arcs.
    """
    reader_id = params["reader_arc_id"]
    target_id = params["target_arc_id"]
    depth = params.get("depth", "subtree")
    caller_arc_id = params.get("_caller_arc_id")

    # Authorization: arc callers must be able to see both reader and target
    if caller_arc_id is not None:
        from ..api.callbacks import _is_descendant_of
        for check_id, label in ((reader_id, "reader"), (target_id, "target")):
            if (check_id != caller_arc_id
                    and not _is_descendant_of(check_id, caller_arc_id)
                    and not arc_manager.has_read_grant(caller_arc_id, check_id)):
                return {
                    "error": f"Cannot grant access — {label} arc #{check_id} "
                             f"is not visible to caller arc #{caller_arc_id}"
                }
        granted_by = f"parent:{caller_arc_id}"
    else:
        granted_by = "tool"

    try:
        grant_id = arc_manager.grant_read_access(
            reader_id, target_id, depth=depth,
            reason="Granted via tool call",
            granted_by=granted_by,
        )
        return {"grant_id": grant_id, "reader_arc_id": reader_id,
                "target_arc_id": target_id, "depth": depth}
    except ValueError as e:
        return {"error": str(e)}


# ── Trust boundary handlers (Phase B) ─────────────────────────────────

# Structural fields safe for any caller (no execution data, no state).
_PLAN_FIELDS = (
    "id", "name", "goal", "status", "parent_id", "step_order",
    "template_id", "integrity_level", "agent_type", "output_type",
    "depth", "from_template", "template_mutable",
    "descendant_tokens", "descendant_executions", "descendant_arc_count",
    "agent_config_id", "created_at", "updated_at",
)


def handle_get_plan(params: dict) -> dict:
    """Get structural-only arc data. Safe for any caller including planners.
    Returns only planning-relevant fields, no execution data."""
    arc = arc_manager.get_arc(params["arc_id"])
    if arc is None:
        return {"error": "Arc not found"}
    return {"arc": {k: arc[k] for k in _PLAN_FIELDS if k in arc}}


def handle_get_children_plan(params: dict) -> dict:
    """Get structural-only data for children. Safe for any caller."""
    children = arc_manager.get_children(params["arc_id"])
    return {
        "children": [
            {k: c[k] for k in _PLAN_FIELDS if k in c}
            for c in children
        ]
    }


def handle_read_output_UNTRUSTED(params: dict) -> dict:
    """Read full arc data + history + state. Taint-gated by callback handler.

    Only tainted or review arcs can call this (enforced in callbacks.py).
    """
    arc_id = params["arc_id"]
    arc = arc_manager.get_arc(arc_id)
    if arc is None:
        return {"error": "Arc not found"}

    history = arc_manager.get_history(arc_id)

    # Get arc state
    with db_connection() as db:
        state_rows = db.execute(
            "SELECT key, value_json FROM arc_state WHERE arc_id = ?",
            (arc_id,),
        ).fetchall()

    state = {row["key"]: json.loads(row["value_json"]) for row in state_rows}

    return {
        "arc": arc,
        "history": history,
        "state": state,
    }


def handle_read_state_UNTRUSTED(params: dict) -> dict:
    """Cross-arc state read. Taint-gated by callback handler.

    Delegates to state backend for the target arc.
    """
    from . import state as state_backend
    return state_backend.handle_get(params)


def handle_create_batch(params: dict) -> dict:
    """Create multiple arcs atomically with validation.

    Params:
        arcs: List of arc dicts with keys: name, goal, parent_id, integrity_level,
              output_type, agent_type, step_order, reviewer_profile.

    Validation rules (atomic, all-or-nothing):
    1. All arcs must have same parent_id (or all None)
    2. Reviewer/Judge arcs must reference valid reviewer_profile from config
    3. Tainted arcs must have at least one REVIEWER or JUDGE arc in batch
    4. Maximum one JUDGE arc per batch
    5. JUDGE must have highest step_order among reviewers
    6. Auto-assign step_order if not provided

    Returns:
        {"arc_ids": [list of created arc IDs]} or {"error": "message"}
    """
    arcs_list = params.get("arcs", [])
    if not arcs_list:
        return {"error": "No arcs provided"}

    # Validation 1: Check parent_id consistency
    parent_ids = {arc.get("parent_id") for arc in arcs_list}
    if len(parent_ids) > 1:
        return {"error": "All arcs must have the same parent_id"}

    parent_id = parent_ids.pop() if parent_ids else None

    # Load agent roles from config
    agent_roles = config.CONFIG.get("agent_roles", {})

    # Validation 2: Check agent roles exist for reviewer/judge arcs
    for arc_spec in arcs_list:
        agent_type = arc_spec.get("agent_type", "EXECUTOR")
        if agent_type in ("REVIEWER", "JUDGE"):
            role_name = arc_spec.get("reviewer_profile") or arc_spec.get("agent_role")
            if not role_name:
                return {
                    "error": f"Arc '{arc_spec.get('name')}' is {agent_type} but missing agent_role/reviewer_profile"
                }
            if role_name not in agent_roles:
                return {
                    "error": f"Unknown agent_role '{role_name}' for arc '{arc_spec.get('name')}'"
                }

    # Validation 3 & 4: Check non-trusted arcs have reviewers and max one judge
    from carpenter.core.trust.integrity import is_non_trusted
    tainted_arcs = [a for a in arcs_list if is_non_trusted(a.get("integrity_level", "trusted"))]
    reviewer_arcs = [
        a for a in arcs_list
        if a.get("agent_type") in ("REVIEWER", "JUDGE")
    ]
    judge_arcs = [a for a in arcs_list if a.get("agent_type") == "JUDGE"]

    if tainted_arcs and not reviewer_arcs:
        return {"error": "Untrusted arcs require at least one REVIEWER or JUDGE arc"}

    if len(judge_arcs) > 1:
        return {"error": "Maximum one JUDGE arc allowed per batch"}

    # Auto-assign step_order if not provided
    with db_transaction() as db:
        try:
            # Get max existing step_order for parent
            if parent_id is not None:
                row = db.execute(
                    "SELECT COALESCE(MAX(step_order), -1) AS max_order "
                    "FROM arcs WHERE parent_id = ?",
                    (parent_id,),
                ).fetchone()
                next_step = row["max_order"] + 1
            else:
                next_step = 0

            # Assign step_order to arcs that don't have one
            for arc_spec in arcs_list:
                if "step_order" not in arc_spec or arc_spec["step_order"] is None:
                    arc_spec["step_order"] = next_step
                    next_step += 1

            # Validation 5: Judge must have highest step_order among reviewers
            if judge_arcs:
                judge = judge_arcs[0]
                judge_order = judge["step_order"]
                for reviewer in reviewer_arcs:
                    if reviewer is not judge and reviewer["step_order"] >= judge_order:
                        return {
                            "error": "JUDGE must have highest step_order among all reviewer/judge arcs"
                        }

            # All validations passed — create arcs in transaction
            created_ids = []
            audit_events = []  # Collect audit events to log after commit
            now = datetime.now(timezone.utc).isoformat()

            for idx, arc_spec in enumerate(arcs_list):
                # Validate and normalize trust types
                integrity_level = validate_integrity_level(arc_spec.get("integrity_level", "trusted"))
                output_type = validate_output_type(arc_spec.get("output_type", "python"))
                agent_type = validate_agent_type(arc_spec.get("agent_type", "EXECUTOR"))

                # Calculate depth
                depth = 0
                if parent_id is not None:
                    parent = db.execute(
                        "SELECT depth FROM arcs WHERE id = ?", (parent_id,)
                    ).fetchone()
                    if parent is not None:
                        depth = parent["depth"] + 1

                # Resolve model_policy_id (by name or direct ID)
                policy_id = arc_spec.get("model_policy_id")
                if policy_id is None and arc_spec.get("model_policy"):
                    policy_id = arc_manager.get_policy_id_by_name(
                        arc_spec["model_policy"]
                    )

                # Insert arc
                cursor = db.execute(
                    "INSERT INTO arcs "
                    "(name, goal, parent_id, step_order, depth, "
                    " integrity_level, output_type, agent_type, "
                    " model_policy_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        arc_spec.get("name", ""),
                        arc_spec.get("goal"),
                        parent_id,
                        arc_spec["step_order"],
                        depth,
                        integrity_level,
                        output_type,
                        agent_type,
                        policy_id,
                        now,
                    ),
                )
                arc_id = cursor.lastrowid
                created_ids.append(arc_id)

                # Log creation history
                db.execute(
                    "INSERT INTO arc_history (arc_id, entry_type, content_json, actor) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        arc_id,
                        "created",
                        json.dumps({"name": arc_spec.get("name", ""), "goal": arc_spec.get("goal")}),
                        "system",
                    ),
                )

                # Store reviewer_profile in arc_state if provided
                if arc_spec.get("reviewer_profile"):
                    db.execute(
                        "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                        (arc_id, "_reviewer_profile", json.dumps(arc_spec["reviewer_profile"])),
                    )

            # Collect reviewer and tainted indices for post-processing
            reviewer_indices = [i for i, spec in enumerate(arcs_list) if spec.get("agent_type") in ("REVIEWER", "JUDGE")]
            tainted_indices = [i for i, spec in enumerate(arcs_list) if is_non_trusted(spec.get("integrity_level", "trusted"))]

            # Generate Fernet encryption keys for tainted arcs and link reviewers
            for tainted_idx in tainted_indices:
                tainted_arc_id = created_ids[tainted_idx]

                # Collect all reviewer arc IDs for this tainted arc
                reviewer_ids = [created_ids[i] for i in reviewer_indices]

                # Generate and store encryption key for all reviewers (inline to avoid nested transactions)
                if reviewer_ids:
                    if not HAS_CRYPTOGRAPHY:
                        # Check if encryption is enforced
                        enforce_encryption = config.CONFIG.get("encryption", {}).get("enforce", True)
                        if enforce_encryption:
                            db.rollback()
                            raise RuntimeError(
                                "Cannot create tainted arc: cryptography library is not available. "
                                "Encryption is required for tainted arc output (encryption.enforce=true). "
                                "Install with: pip install cryptography>=41.0 "
                                "Or set encryption.enforce=false in config.yaml to allow plaintext fallback."
                            )
                        else:
                            logger.warning(
                                "cryptography library not available - cannot encrypt arc %d output. "
                                "Install with: pip install cryptography>=41.0",
                                tainted_arc_id
                            )
                            audit_events.append((tainted_arc_id, "encryption_unavailable", {
                                "reason": "cryptography_library_missing",
                                "reviewer_count": len(reviewer_ids),
                            }))
                    else:
                        key = Fernet.generate_key()
                        for reviewer_id in reviewer_ids:
                            db.execute(
                                "INSERT INTO review_keys "
                                "(target_arc_id, reviewer_arc_id, fernet_key_encrypted) "
                                "VALUES (?, ?, ?) "
                                "ON CONFLICT(target_arc_id, reviewer_arc_id) "
                                "DO UPDATE SET fernet_key_encrypted = excluded.fernet_key_encrypted",
                                (tainted_arc_id, reviewer_id, key),
                            )
                        # Queue audit event for after commit
                        audit_events.append((tainted_arc_id, "encryption_key_created", {
                            "reviewer_count": len(reviewer_ids),
                        }))

                # Link all reviewers to this tainted arc
                for reviewer_idx in reviewer_indices:
                    reviewer_arc_id = created_ids[reviewer_idx]
                    db.execute(
                        "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                        (reviewer_arc_id, "_review_target", json.dumps(tainted_arc_id)),
                    )


        except Exception as e:  # broad catch: batch arc creation involves many operations
            db.rollback()
            return {"error": str(e)}

    # Post-commit: log audit events (outside transaction to avoid nested writes)
    for arc_id, event_type, details in audit_events:
        try:
            log_trust_event(arc_id, event_type, details)
        except (ImportError, sqlite3.Error) as _exc:
            pass  # Don't fail the batch creation over audit logging

    result = {"arc_ids": created_ids}

    # Platform hint: suggest PLANNER root when batch-creating children under non-PLANNER
    if parent_id is not None:
        try:
            parent = arc_manager.get_arc(parent_id)
            if parent and parent.get("agent_type") != "PLANNER":
                worker_arcs = [
                    a for a in arcs_list
                    if a.get("agent_type", "EXECUTOR") not in ("REVIEWER", "JUDGE")
                ]
                if len(worker_arcs) >= 2:
                    result["hint"] = (
                        "This arc has multiple worker children but is not a PLANNER. "
                        "Consider using a PLANNER root for multi-step work — "
                        "it gives the escalation system a meaningful target "
                        "when children fail. See skills/planner-root in the knowledge base."
                    )
        except (sqlite3.Error, KeyError, ValueError) as _exc:
            pass  # Don't fail batch creation over hint logic

    return result


def handle_request_ai_review(params: dict) -> dict:
    """Request an ad-hoc AI review of a coding-change arc.

    Params: target_arc_id, model, focus_areas (opt).

    Creates a REVIEWER child arc configured with the requested model.
    The reviewer examines the workspace and diff, storing structured
    findings in arc_state.  Informational only — does not trigger
    automated acceptance.

    Returns: {"arc_id": int}
    """
    target_arc_id = params.get("target_arc_id")
    model = params.get("model", "")
    focus_areas = params.get("focus_areas")

    if not target_arc_id:
        return {"error": "target_arc_id is required"}
    if not model:
        return {"error": "model is required"}

    # Validate target arc exists and is a coding-change arc in 'waiting' status
    target = arc_manager.get_arc(target_arc_id)
    if target is None:
        return {"error": f"Arc {target_arc_id} not found"}
    if target["status"] != "waiting":
        return {"error": f"Arc {target_arc_id} is not in 'waiting' status (got '{target['status']}')"}

    # Read workspace_path and diff from target's arc_state
    with db_connection() as db:
        state_rows = db.execute(
            "SELECT key, value_json FROM arc_state WHERE arc_id = ?",
            (target_arc_id,),
        ).fetchall()

    state = {}
    for row in state_rows:
        try:
            state[row["key"]] = json.loads(row["value_json"])
        except (json.JSONDecodeError, TypeError):
            state[row["key"]] = row["value_json"]

    diff = state.get("diff", "")
    workspace_path = state.get("workspace_path", "")
    if not diff:
        return {"error": f"No diff found in arc_state for arc {target_arc_id}"}

    # Find the await-approval child to determine step_order
    children = arc_manager.get_children(target_arc_id)
    await_approval = None
    for child in children:
        if child.get("name", "").startswith("await-approval"):
            await_approval = child
            break

    step_order = await_approval["step_order"] if await_approval else 2

    # Build reviewer goal
    goal_parts = [
        f"Review the coding changes for arc #{target_arc_id}.",
        f"Workspace: {workspace_path}" if workspace_path else "",
        "Examine the diff and workspace files. Use files.read to inspect the changed files.",
        "Store your structured findings via state.set(key='review_findings', value=<findings>).",
        "Expected findings format: {\"summary\": \"...\", \"issues\": [...], "
        "\"recommendations\": [...], \"verdict\": \"approve\" or \"concerns\"}.",
    ]
    if focus_areas:
        goal_parts.append(f"Focus areas: {focus_areas}")
    goal = "\n".join(part for part in goal_parts if part)

    # Resolve model identifier
    from ..agent.model_resolver import resolve_model_identifier
    try:
        resolved_model = resolve_model_identifier(model)
    except ValueError as e:
        return {"error": str(e)}

    # Create REVIEWER child arc
    reviewer_id = arc_manager.create_arc(
        name=f"ad-hoc-review-{model}",
        goal=goal,
        parent_id=target_arc_id,
        step_order=step_order,
        agent_type="REVIEWER",
        arc_role="worker",
        agent_model=model,
    )

    # Store _review_target in reviewer's arc_state
    with db_transaction() as db:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
            (reviewer_id, "_review_target", json.dumps(target_arc_id)),
        )

    # Log event on target arc
    arc_manager.add_history(
        target_arc_id,
        "ad_hoc_review_requested",
        {"model": model, "reviewer_arc_id": reviewer_id, "focus_areas": focus_areas},
        actor="system",
    )

    return {"arc_id": reviewer_id}


def _resolve_source_dir(source_dir: str) -> str:
    """Resolve source_dir aliases and auto-default to configured paths.

    Supports:
      - "platform" -> platform_server_dir from config (the running checkout)
      - "" or missing -> platform_server_dir from config (default target)
      - Non-existent absolute paths -> attempt fallback to platform_server_dir
      - Existing absolute paths -> used as-is

    Returns the resolved absolute path, or "" if no valid path is found.
    """
    import os
    from .. import config

    _PLATFORM_ALIASES = {"platform", "platform-source", "self"}

    raw = (source_dir or "").strip()

    # Resolve well-known aliases
    if not raw or raw.lower() in _PLATFORM_ALIASES:
        resolved = config.CONFIG.get("platform_server_dir", "")
        if not resolved:
            resolved = config.CONFIG.get("platform_source_dir", "")
        if resolved and os.path.isdir(resolved):
            return resolved
        return resolved  # may be "" if neither is configured

    # If the path exists, use it as-is
    if os.path.isdir(raw):
        return raw

    # Path doesn't exist — try config fallbacks before failing.
    # The agent may have guessed a wrong path (e.g. /root/carpenter).
    for key in ("platform_server_dir", "platform_source_dir"):
        fallback = config.CONFIG.get(key, "")
        if fallback and os.path.isdir(fallback):
            logger.warning(
                "source_dir %r does not exist, falling back to %s=%s",
                raw, key, fallback,
            )
            return fallback

    # Return the original path — workspace_manager will raise FileNotFoundError
    return raw


def handle_invoke_coding_change(params: dict) -> dict:
    """Start the coding-change workflow.

    Params: source_dir, prompt, conversation_id (injected by callbacks.py), coding_agent (opt).

    Creates a coding-change arc, stores conversation_id and source_dir in arc_state,
    then enqueues coding-change.invoke-agent to begin the workspace/agent/review cycle.

    Returns: {"arc_id": int}
    """
    import json as _json
    import time as _time
    from ..core.engine import work_queue
    from ..core.source_classifier import classify_source_dir, get_policy_for_category
    from ..core.engine.template_executor import get_template_for_workflow

    source_dir = params.get("source_dir", "")
    prompt = params.get("prompt", "")
    conversation_id = params.get("conversation_id")
    coding_agent = params.get("coding_agent")

    # Resolve well-known aliases and auto-default for source_dir.
    # The agent may pass "platform" (alias) or an empty/missing value.
    # Resolve to the configured platform_server_dir so the coding-change
    # workflow targets the running server checkout.
    source_dir = _resolve_source_dir(source_dir)

    if not source_dir:
        return {"error": "source_dir is required (set platform_server_dir in config)"}
    if not prompt:
        return {"error": "prompt is required"}

    # Look up template for this workflow type
    template = get_template_for_workflow(CODING_CHANGE_PREFIX)
    template_id = template["id"] if template else None

    # Classify source directory and select appropriate model policy
    source_category = classify_source_dir(source_dir)
    coder_policy_name = get_policy_for_category(source_category, "model_policy")
    policy_id = arc_manager.get_policy_id_by_name(coder_policy_name)

    arc_name = f"{CODING_CHANGE_PREFIX}: {prompt[:60]}"
    arc_id = arc_manager.create_arc(
        name=arc_name,
        goal=prompt,
        model_policy_id=policy_id,  # Apply context-aware model policy
        template_id=template_id     # Link to workflow template
    )

    # Store conversation_id, source_dir, and source_category so the handler and reviewer can use them.
    with db_transaction() as db:
        for key, value in [
            ("conversation_id", conversation_id),
            ("source_dir", source_dir),
            ("source_category", source_category)
        ]:
            if value is not None:
                db.execute(
                    "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?) "
                    "ON CONFLICT(arc_id, key) DO UPDATE SET value_json = excluded.value_json, "
                    "updated_at = CURRENT_TIMESTAMP",
                    (arc_id, key, _json.dumps(value)),
                )

    payload: dict = {"arc_id": arc_id, "source_dir": source_dir, "prompt": prompt}
    if coding_agent:
        payload["coding_agent"] = coding_agent

    work_queue.enqueue(
        f"{CODING_CHANGE_PREFIX}.invoke-agent",
        payload,
        idempotency_key=f"{CODING_CHANGE_PREFIX}-invoke-{arc_id}-{int(_time.time())}",
        max_retries=work_queue.SINGLE_ATTEMPT,
    )

    return {"arc_id": arc_id}
