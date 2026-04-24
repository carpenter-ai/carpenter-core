"""Arc manager for Carpenter.

Handles CRUD operations on arcs, step dispatch logic, history logging,
and immutability enforcement.

Arc statuses: pending, active, waiting, completed, failed, cancelled

Key invariants:
- Completed/failed/cancelled arcs are immutable (no children, no transitions)
- Template-created arcs (from_template=True) are immutable (no children, no modifications)
- Status transitions follow a strict state machine
- Cancellation cascades to all pending/active/waiting descendants
- History entries are append-only
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone

from ...db import get_db, db_connection, db_transaction

logger = logging.getLogger(__name__)

VALID_STATUSES = {"pending", "active", "waiting", "completed", "failed", "cancelled", "escalated"}

FROZEN_STATUSES = {"completed", "failed", "cancelled", "escalated"}

# Statuses that count as "done" for dependency purposes
DONE_STATUSES = {"completed", "escalated"}

TRANSITIONS = {
    "pending": {"active", "cancelled"},
    "active": {"waiting", "completed", "failed", "cancelled", "escalated"},
    "waiting": {"active", "completed", "failed", "cancelled"},
    "completed": set(),
    "failed": {"escalated"},
    "cancelled": set(),
    "escalated": set(),
}


def _validate_verification_target(
    db, verification_target_id: int, parent_id: int | None, arc_id: int | None = None,
) -> None:
    """Validate verification_target_id constraints.

    Enforces:
    - No self-verification (verification_target_id != arc_id)
    - Target must be a sibling (shared parent_id)
    - Target must exist

    Args:
        db: Database connection.
        verification_target_id: The target arc to verify.
        parent_id: The parent_id of the verifier arc.
        arc_id: The ID of the verifier arc (None if not yet created).

    Raises:
        ValueError: If any constraint is violated.
    """
    if arc_id is not None and verification_target_id == arc_id:
        raise ValueError(
            f"Self-verification not allowed: verification_target_id ({verification_target_id}) "
            f"cannot equal arc_id ({arc_id})"
        )

    target = db.execute(
        "SELECT id, parent_id FROM arcs WHERE id = ?",
        (verification_target_id,),
    ).fetchone()
    if target is None:
        raise ValueError(
            f"Verification target arc {verification_target_id} not found"
        )

    # Sibling check: both must share the same parent_id
    if target["parent_id"] != parent_id:
        raise ValueError(
            f"Verification target arc {verification_target_id} is not a sibling "
            f"(target parent_id={target['parent_id']}, verifier parent_id={parent_id})"
        )


def create_arc(
    name: str,
    goal: str | None = None,
    parent_id: int | None = None,
    code_file_id: int | None = None,
    template_id: int | None = None,
    from_template: bool = False,
    template_mutable: bool = False,
    timeout_minutes: int | None = None,
    step_order: int = 0,
    integrity_level: str = "trusted",
    output_type: str = "python",
    agent_type: str = "EXECUTOR",
    model: str | None = None,
    model_role: str | None = None,
    agent_role: str | None = None,
    agent_model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    agent_config_id: int | None = None,
    model_policy_id: int | None = None,
    wait_until: str | None = None,
    output_contract: str | None = None,
    arc_role: str = "worker",
    verification_target_id: int | None = None,
    _allow_tainted: bool = False,
    _db_conn=None,
    _audit_queue: list | None = None,
) -> int:
    """Create a new arc and log a history entry.

    Auto-calculates depth from parent (parent.depth + 1, or 0 if root).

    Args:
        integrity_level: Integrity level ('trusted', 'constrained', 'untrusted').
        output_type: Expected output format ('python', 'text', 'json', 'unknown').
        agent_type: Agent role ('PLANNER', 'EXECUTOR', 'REVIEWER', 'CHAT').
        _allow_tainted: Internal flag to allow untrusted arcs (for add_child use).
        _db_conn: Optional existing database connection (for batching).
        _audit_queue: Optional list to queue audit events instead of logging immediately.

    Returns the arc ID.
    """
    from ..trust.types import validate_integrity_level, validate_output_type, validate_agent_type
    from ..trust.integrity import is_non_trusted

    integrity_level = validate_integrity_level(integrity_level)
    output_type = validate_output_type(output_type)
    agent_type = validate_agent_type(agent_type)

    # Reject individual non-trusted arc creation (must use batch creation)
    # Exception: add_child() can create non-trusted children (batch validates reviewers)
    if is_non_trusted(integrity_level) and not _allow_tainted:
        raise ValueError(
            "Cannot create individual untrusted arc. Use arc.create_batch to create "
            "untrusted arcs with their required review arcs atomically."
        )

    # Resolve agent_model short identifier to provider:model_id
    if agent_model and not model:
        from ...agent.model_resolver import resolve_model_identifier
        model = resolve_model_identifier(agent_model)

    owns_connection = _db_conn is None
    db = _db_conn if _db_conn else get_db()

    # Resolve agent_config_id if model params provided
    if agent_config_id is None and (model or model_role or agent_role):
        from ...agent.model_resolver import get_model_for_role
        resolved_model = model
        if not resolved_model and model_role:
            resolved_model = get_model_for_role(model_role)
        if not resolved_model:
            resolved_model = get_model_for_role("default_step")
        agent_config_id = get_or_create_agent_config(
            model=resolved_model,
            agent_role=agent_role,
            temperature=temperature,
            max_tokens=max_tokens,
            _db_conn=db,
        )
    try:
        depth = 0
        if parent_id is not None:
            parent = db.execute(
                "SELECT depth FROM arcs WHERE id = ?", (parent_id,)
            ).fetchone()
            if parent is not None:
                depth = parent["depth"] + 1

        # Validate arc_role
        valid_arc_roles = {"coordinator", "worker", "verifier"}
        if arc_role not in valid_arc_roles:
            raise ValueError(
                f"Invalid arc_role: {arc_role!r}. "
                f"Valid roles: {sorted(valid_arc_roles)}"
            )

        # Validate verification_target_id constraints
        if verification_target_id is not None:
            _validate_verification_target(
                db, verification_target_id, parent_id, arc_id=None,
            )

        # Sync model_policy_id from agent_config_id if not explicitly set
        if model_policy_id is None and agent_config_id is not None:
            model_policy_id = agent_config_id

        now = datetime.now(timezone.utc).isoformat()
        cursor = db.execute(
            "INSERT INTO arcs "
            "(name, goal, parent_id, code_file_id, template_id, "
            " from_template, template_mutable, timeout_minutes, step_order, depth, "
            " integrity_level, output_type, agent_type, agent_config_id, model_policy_id, "
            " wait_until, output_contract, arc_role, verification_target_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name, goal, parent_id, code_file_id, template_id,
                from_template, template_mutable, timeout_minutes, step_order, depth,
                integrity_level, output_type, agent_type, agent_config_id, model_policy_id,
                wait_until, output_contract, arc_role, verification_target_id, now,
            ),
        )
        arc_id = cursor.lastrowid

        # Log creation history
        db.execute(
            "INSERT INTO arc_history (arc_id, entry_type, content_json, actor) "
            "VALUES (?, ?, ?, ?)",
            (arc_id, "created", json.dumps({"name": name, "goal": goal}), "system"),
        )

        if owns_connection:
            db.commit()

        # Initialize retry state
        if owns_connection:  # Only initialize if we own the transaction
            try:
                from . import retry as arc_retry
                # Extract retry_policy from kwargs if present
                retry_policy = "transient_only"  # Default
                arc_retry.initialize_retry_state(
                    arc_id,
                    retry_policy=retry_policy,
                )
            except (ImportError, sqlite3.Error, ValueError) as _exc:
                logger.exception("Failed to initialize retry state for arc %d", arc_id)
                # Don't fail arc creation over retry state init

        # Queue or log trust audit event
        if _audit_queue is not None:
            _audit_queue.append((arc_id, "integrity_assigned", {
                "integrity_level": integrity_level,
                "output_type": output_type,
                "agent_type": agent_type,
            }))
        else:
            try:
                from ..trust.audit import log_trust_event
                log_trust_event(arc_id, "integrity_assigned", {
                    "integrity_level": integrity_level,
                    "output_type": output_type,
                    "agent_type": agent_type,
                })
            except (ImportError, sqlite3.Error) as _exc:
                pass  # Don't fail arc creation over audit logging

        # Advisory: log if non-trusted arc with non-automated review output type
        if is_non_trusted(integrity_level):
            try:
                from ..workflows.review_templates import has_automated_review
                if not has_automated_review(output_type):
                    logger.info(
                        "Arc %d (%s) is tainted with output_type='%s' — "
                        "human review will be required",
                        arc_id, name, output_type,
                    )
            except ImportError:
                pass

        # Immediately enqueue root arcs that have code to execute.
        # Arcs without code (PLANNER/REVIEWER/JUDGE) need children to be added
        # first — enqueuing them immediately races with add_child() calls and
        # can cause "Cannot add child to frozen arc" errors. The heartbeat will
        # pick them up within ~5s after their children are in place.
        # Skip immediate enqueue if wait_until is set and in the future.
        _skip_enqueue = False
        if wait_until:
            try:
                wait_dt = datetime.fromisoformat(wait_until)
                if wait_dt.tzinfo is None:
                    wait_dt = wait_dt.replace(tzinfo=timezone.utc)
                if wait_dt > datetime.now(timezone.utc):
                    _skip_enqueue = True
            except (ValueError, TypeError):
                pass

        if parent_id is None and owns_connection and code_file_id is not None and not _skip_enqueue:
            try:
                from ..engine import work_queue as _wq
                _wq.enqueue(
                    "arc.dispatch",
                    {"arc_id": arc_id},
                    idempotency_key=f"arc_dispatch:{arc_id}",
                )
                logger.debug("Enqueued root arc %d for immediate dispatch", arc_id)
                from ..engine import main_loop as _ml
                _ml.wake_signal.set()
            except (ImportError, sqlite3.Error) as _exc:
                pass  # Heartbeat will catch it

        return arc_id
    finally:
        if owns_connection:
            db.close()


def add_child(
    parent_id: int,
    name: str,
    goal: str | None = None,
    **kwargs,
) -> int:
    """Add a child arc to a parent.

    Validates parent exists and is not frozen. Auto-sets step_order to
    max sibling order + 1.

    Uses batched database operations for performance: single transaction,
    queued audit logging.

    Returns the child arc ID.

    Raises:
        ValueError: If parent does not exist or is in a frozen status.
        ValueError: If parent was created by a template (from_template=True).
    """
    db = get_db()
    audit_queue = []  # Queue audit events for logging after commit

    try:
        parent = db.execute(
            "SELECT id, status, from_template, template_mutable FROM arcs WHERE id = ?", (parent_id,)
        ).fetchone()
        if parent is None:
            raise ValueError(f"Parent arc {parent_id} not found")
        if parent["status"] in FROZEN_STATUSES:
            raise ValueError(
                f"Cannot add child to arc {parent_id} with status '{parent['status']}'"
            )
        if parent["from_template"] and not parent["template_mutable"]:
            raise ValueError(
                f"Cannot add child to arc {parent_id} created by template (from_template=True)"
            )

        # Auto-calculate step_order
        row = db.execute(
            "SELECT COALESCE(MAX(step_order), -1) AS max_order "
            "FROM arcs WHERE parent_id = ?",
            (parent_id,),
        ).fetchone()
        step_order = row["max_order"] + 1

        # Log history on parent
        db.execute(
            "INSERT INTO arc_history (arc_id, entry_type, content_json, actor) "
            "VALUES (?, ?, ?, ?)",
            (
                parent_id,
                "child_added",
                json.dumps({"child_name": name}),
                "system",
            ),
        )

        # Create the child arc within same transaction
        from ..trust.integrity import is_non_trusted as _is_non_trusted
        child_integrity = kwargs.get("integrity_level", "trusted")
        child_id = create_arc(
            name=name,
            goal=goal,
            parent_id=parent_id,
            step_order=step_order,
            _allow_tainted=_is_non_trusted(child_integrity),
            _db_conn=db,  # Reuse connection
            _audit_queue=audit_queue,  # Share audit queue
            **kwargs,
        )

        # Single commit for all operations
        db.commit()

        # Log queued audit events after successful commit
        for arc_id, event_type, details in audit_queue:
            try:
                from ..trust.audit import log_trust_event
                log_trust_event(arc_id, event_type, details)
            except (ImportError, sqlite3.Error) as _exc:
                pass  # Don't fail arc creation over audit logging

        # Update ancestor performance counters (descendant_arc_count)
        try:
            increment_ancestor_arc_count(child_id)
        except (sqlite3.Error, ValueError) as _exc:
            pass  # Don't fail arc creation over counter updates

        # Check if this child is immediately ready to dispatch
        # (No preceding siblings or all predecessors completed)
        # Skip if wait_until is set and in the future.
        _child_wait = kwargs.get("wait_until")
        _skip_child_enqueue = False
        if _child_wait:
            try:
                _cw_dt = datetime.fromisoformat(_child_wait)
                if _cw_dt.tzinfo is None:
                    _cw_dt = _cw_dt.replace(tzinfo=timezone.utc)
                if _cw_dt > datetime.now(timezone.utc):
                    _skip_child_enqueue = True
            except (ValueError, TypeError):
                pass

        if not _skip_child_enqueue:
            try:
                if check_dependencies(child_id) and check_activation(child_id):
                    # Enqueue for dispatch
                    try:
                        from ..engine import work_queue as _wq
                        _wq.enqueue(
                            "arc.dispatch",
                            {"arc_id": child_id},
                            idempotency_key=f"arc_dispatch:{child_id}",
                        )
                        logger.info("Enqueued newly created arc %d for immediate dispatch", child_id)
                        # Wake main loop
                        from ..engine import main_loop as _ml
                        _ml.wake_signal.set()
                    except (ImportError, sqlite3.Error) as _exc:
                        pass  # Heartbeat will catch it
            except (sqlite3.Error, ValueError) as _exc:
                pass  # Heartbeat will catch it

        return child_id
    finally:
        db.close()


def get_arc(arc_id: int) -> dict | None:
    """Get an arc by ID. Returns dict or None."""
    with db_connection() as db:
        row = db.execute(
            "SELECT * FROM arcs WHERE id = ?", (arc_id,)
        ).fetchone()
        return dict(row) if row else None


def get_children(arc_id: int) -> list[dict]:
    """Get children of an arc, ordered by step_order."""
    with db_connection() as db:
        rows = db.execute(
            "SELECT * FROM arcs WHERE parent_id = ? ORDER BY step_order",
            (arc_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_subtree(arc_id: int) -> list[dict]:
    """Get all descendants of an arc as a flat list.

    Uses recursive CTE. Results ordered by depth then step_order.
    """
    with db_connection() as db:
        rows = db.execute(
            "WITH RECURSIVE subtree AS ( "
            "  SELECT * FROM arcs WHERE parent_id = ? "
            "  UNION ALL "
            "  SELECT a.* FROM arcs a "
            "  INNER JOIN subtree s ON a.parent_id = s.id "
            ") "
            "SELECT * FROM subtree ORDER BY depth, step_order",
            (arc_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def update_status(
    arc_id: int,
    new_status: str,
    actor: str = "system",
) -> None:
    """Update the status of an arc with transition validation.

    Raises:
        ValueError: If the transition is not legal or arc not found.
    """
    if new_status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status: {new_status!r}. "
            f"Valid statuses: {sorted(VALID_STATUSES)}"
        )

    # Collect metadata needed for post-transition hooks (populated inside transaction)
    _arc_meta_for_hooks = None
    _is_root_arc = False

    with db_transaction() as db:
        row = db.execute(
            "SELECT status FROM arcs WHERE id = ?", (arc_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Arc {arc_id} not found")

        old_status = row["status"]
        allowed = TRANSITIONS.get(old_status, set())
        if new_status not in allowed:
            hint = f"Allowed from {old_status!r}: {sorted(allowed)}" if allowed else f"Status {old_status!r} is terminal (no transitions allowed). Use arc.cancel instead."
            raise ValueError(
                f"Invalid transition: {old_status} -> {new_status}. {hint}"
            )

        now = datetime.now(timezone.utc).isoformat()
        cursor = db.execute(
            "UPDATE arcs SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (new_status, now, arc_id, old_status),
        )

        # CAS check: if rowcount is 0, the status changed between SELECT and UPDATE
        if cursor.rowcount == 0:
            raise ValueError(
                f"Arc {arc_id} status changed during update (expected {old_status!r}). "
                "This may indicate a concurrent modification."
            )

        # Log history
        db.execute(
            "INSERT INTO arc_history (arc_id, entry_type, content_json, actor) "
            "VALUES (?, ?, ?, ?)",
            (
                arc_id,
                "status_changed",
                json.dumps({"old_status": old_status, "new_status": new_status}),
                actor,
            ),
        )

        # Cascade cancel to children if cancelled
        if new_status == "cancelled":
            _cascade_cancel(db, arc_id, actor)

        # Read metadata needed by post-transition hooks while we have the connection
        _arc_meta_for_hooks = db.execute(
            "SELECT name, arc_role, parent_id, agent_type FROM arcs WHERE id = ?",
            (arc_id,),
        ).fetchone()
        if _arc_meta_for_hooks:
            _arc_meta_for_hooks = dict(_arc_meta_for_hooks)
            _is_root_arc = _arc_meta_for_hooks["parent_id"] is None

    # --- Post-transition hooks (outside transaction to avoid nested writes) ---

    # Emit arc lifecycle event for the trigger/subscription pipeline
    if _arc_meta_for_hooks:
        try:
            from ..engine.triggers.arc_lifecycle import emit_status_changed
            emit_status_changed(
                arc_id=arc_id,
                old_status=old_status,
                new_status=new_status,
                arc_name=_arc_meta_for_hooks["name"],
                arc_role=_arc_meta_for_hooks["arc_role"],
                parent_id=_arc_meta_for_hooks["parent_id"],
                agent_type=_arc_meta_for_hooks["agent_type"],
            )
        except Exception:  # broad catch: event emission must not break status updates
            logger.debug("Failed to emit arc lifecycle event for arc %d", arc_id, exc_info=True)

    # Post-transition: enqueue work history summary for completed root arcs
    if new_status == "completed" and _is_root_arc:
        try:
            from ..engine import work_queue as _wq
            _wq.enqueue(
                "kb.work_summary",
                {"arc_id": arc_id},
                idempotency_key=f"work_summary_{arc_id}",
            )
        except (ImportError, sqlite3.Error) as _exc:
            logger.debug("Failed to enqueue work summary for arc %d", arc_id, exc_info=True)

    # Post-transition: notify linked conversation for completed/failed root arcs
    # Only notify if the arc is linked to a conversation (via conversation_arcs).
    # Internal arcs (e.g. verification arcs) aren't linked and would fall back
    # to get_last_conversation(), causing spurious chat invocations.
    if new_status in ("completed", "failed") and _is_root_arc:
        try:
            with db_connection() as _notify_db:
                _has_conv = _notify_db.execute(
                    "SELECT 1 FROM conversation_arcs WHERE arc_id = ? LIMIT 1",
                    (arc_id,),
                ).fetchone()
            if _has_conv:
                from ..engine import work_queue as _wq2
                _wq2.enqueue(
                    "arc.chat_notify",
                    {"arc_id": arc_id},
                    idempotency_key=f"chat_notify_{arc_id}",
                )
        except (ImportError, sqlite3.Error):
            logger.debug("Failed to enqueue chat notify for arc %d", arc_id, exc_info=True)

    # Post-transition: notify parent when a child fails
    if new_status == "failed":
        try:
            _notify_parent_of_failure(arc_id)
        except Exception:  # broad catch: notification chain may raise anything
            logger.exception("Failed to notify parent of arc %d failure", arc_id)


def _cascade_cancel(db, arc_id: int, actor: str) -> int:
    """Cancel all pending/active/waiting descendants. Returns count cancelled."""
    count = 0
    children = db.execute(
        "SELECT id, status FROM arcs WHERE parent_id = ?",
        (arc_id,),
    ).fetchall()

    now = datetime.now(timezone.utc).isoformat()
    for child in children:
        if child["status"] in ("pending", "active", "waiting"):
            db.execute(
                "UPDATE arcs SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (now, child["id"]),
            )
            db.execute(
                "INSERT INTO arc_history (arc_id, entry_type, content_json, actor) "
                "VALUES (?, ?, ?, ?)",
                (
                    child["id"],
                    "status_changed",
                    json.dumps({
                        "old_status": child["status"],
                        "new_status": "cancelled",
                    }),
                    actor,
                ),
            )
            count += 1
        # Recurse regardless — a cancelled parent may have pending grandchildren
        count += _cascade_cancel(db, child["id"], actor)

    return count


def cancel_arc(arc_id: int, actor: str = "system") -> int:
    """Cancel an arc and cascade to descendants.

    Returns count of arcs cancelled (including self if applicable).
    Only cancels arcs in pending/active/waiting status.
    """
    with db_transaction() as db:
        row = db.execute(
            "SELECT status FROM arcs WHERE id = ?", (arc_id,)
        ).fetchone()
        if row is None:
            return 0

        count = 0
        now = datetime.now(timezone.utc).isoformat()

        if row["status"] in ("pending", "active", "waiting"):
            db.execute(
                "UPDATE arcs SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (now, arc_id),
            )
            db.execute(
                "INSERT INTO arc_history (arc_id, entry_type, content_json, actor) "
                "VALUES (?, ?, ?, ?)",
                (
                    arc_id,
                    "status_changed",
                    json.dumps({
                        "old_status": row["status"],
                        "new_status": "cancelled",
                    }),
                    actor,
                ),
            )
            count = 1

        count += _cascade_cancel(db, arc_id, actor)
        return count


def add_history(
    arc_id: int,
    entry_type: str,
    content: dict,
    code_file_id: int | None = None,
    actor: str = "system",
) -> int:
    """Add a history entry to an arc. Returns the history entry ID."""
    with db_transaction() as db:
        cursor = db.execute(
            "INSERT INTO arc_history "
            "(arc_id, entry_type, content_json, code_file_id, actor) "
            "VALUES (?, ?, ?, ?, ?)",
            (arc_id, entry_type, json.dumps(content), code_file_id, actor),
        )
        history_id = cursor.lastrowid
        return history_id


def get_history(arc_id: int) -> list[dict]:
    """Get all history entries for an arc, ordered by created_at ASC."""
    with db_connection() as db:
        rows = db.execute(
            "SELECT * FROM arc_history WHERE arc_id = ? ORDER BY created_at ASC",
            (arc_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def check_dependencies(arc_id: int) -> bool:
    """Check if all preceding siblings (lower step_order) are completed.

    Root arcs with no siblings always return True.
    Returns True if all dependencies are satisfied.
    """
    with db_connection() as db:
        arc = db.execute(
            "SELECT parent_id, step_order FROM arcs WHERE id = ?", (arc_id,)
        ).fetchone()
        if arc is None:
            return True
        if arc["parent_id"] is None:
            return True

        preceding = db.execute(
            "SELECT status FROM arcs "
            "WHERE parent_id = ? AND step_order < ?",
            (arc["parent_id"], arc["step_order"]),
        ).fetchall()

        if not preceding:
            return True

        return all(row["status"] in DONE_STATUSES for row in preceding)


def check_activation(arc_id: int) -> bool:
    """Check if an arc's activation conditions are met.

    If no activations registered, returns True.
    If activations registered, checks for matching processed events.
    """
    with db_connection() as db:
        activations = db.execute(
            "SELECT event_type FROM arc_activations WHERE arc_id = ?",
            (arc_id,),
        ).fetchall()

        if not activations:
            return True

        for activation in activations:
            match = db.execute(
                "SELECT id FROM events "
                "WHERE event_type = ? AND processed = TRUE "
                "LIMIT 1",
                (activation["event_type"],),
            ).fetchone()
            if match is not None:
                return True

        return False


def dispatch_arc(arc_id: int) -> dict:
    """Dispatch an arc for execution.

    If arc has code_file_id, executes via code_manager.execute().
    Otherwise, returns an invoke_agent action.

    Raises:
        ValueError: If arc is not in pending, active, or waiting status.
    """
    with db_connection() as db:
        arc = db.execute(
            "SELECT * FROM arcs WHERE id = ?", (arc_id,)
        ).fetchone()
        if arc is None:
            raise ValueError(f"Arc {arc_id} not found")

        if arc["status"] not in ("pending", "active", "waiting"):
            raise ValueError(
                f"Cannot dispatch arc {arc_id} with status '{arc['status']}'"
            )

        code_file_id = arc["code_file_id"]
        current_status = arc["status"]

    # Update status to active if pending or waiting (retry)
    if current_status in ("pending", "waiting"):
        update_status(arc_id, "active")

    if code_file_id:
        from .. import code_manager
        result = code_manager.execute(code_file_id, arc_id=arc_id,
                                 execution_context="arc-step")
        add_history(
            arc_id,
            "dispatched",
            {"action": "execute_code", "code_file_id": code_file_id, "result": result},
        )
        return {"action": "execute_code", "arc_id": arc_id, "result": result}
    else:
        add_history(
            arc_id,
            "dispatched",
            {"action": "invoke_agent"},
        )
        return {"action": "invoke_agent", "arc_id": arc_id}


def freeze_arc(arc_id: int) -> None:
    """Freeze an arc after execution.

    Decision logic:
    - No children → completed
    - All children completed → completed
    - Any child failed AND all children frozen → failed (propagate)
    - Children still running → waiting
    """
    arc = get_arc(arc_id)
    if arc and arc["status"] in FROZEN_STATUSES:
        return  # Already frozen — idempotent, nothing to do

    children = get_children(arc_id)

    if not children:
        update_status(arc_id, "completed")
        return

    all_done = all(c["status"] in DONE_STATUSES for c in children)
    if all_done:
        update_status(arc_id, "completed")
        return

    # Check if any child failed and all children are frozen
    any_failed = any(c["status"] == "failed" for c in children)
    all_frozen = all(c["status"] in FROZEN_STATUSES for c in children)
    if any_failed and all_frozen:
        update_status(arc_id, "failed")
        return

    if arc and arc["status"] == "active":
        update_status(arc_id, "waiting")


def is_frozen(arc_id: int) -> bool:
    """Return True if arc status is completed, failed, or cancelled."""
    arc = get_arc(arc_id)
    if arc is None:
        return False
    return arc["status"] in FROZEN_STATUSES


# ── Child failure notification ────────────────────────────────────


def _notify_parent_of_failure(arc_id: int) -> None:
    """Notify the parent arc when a child fails.

    Called from update_status() after a child transitions to 'failed'.
    Enqueues an 'arc.child_failed' work item so the parent can be
    re-invoked to create alternatives or propagate failure.
    """
    arc = get_arc(arc_id)
    if arc is None:
        return

    parent_id = arc["parent_id"]
    if parent_id is None:
        _handle_root_failure(arc_id)
        return

    parent = get_arc(parent_id)
    if parent is None:
        return

    # Only notify if parent is waiting for children
    if parent["status"] != "waiting":
        return

    # Template-managed parents are handled by the template handler
    if parent["from_template"]:
        return

    # Check escalation policy (stored in arc_state)
    with db_connection() as db:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = '_escalation_policy'",
            (parent_id,),
        ).fetchone()

    policy = "replan"  # default
    if row:
        import json as _json
        policy = _json.loads(row["value_json"])

    if policy == "fail":
        return

    if policy == "human":
        try:
            from .. import notifications
            notifications.notify(
                f"Arc #{arc_id} (child of #{parent_id}) failed. "
                f"Parent requires human intervention.",
                priority="normal",
                category="child_failure",
            )
        except Exception:  # broad catch: notification delivery may raise anything
            logger.exception("Failed to send human notification for arc %d", arc_id)
        return

    if policy == "escalate":
        # Escalate the failed child arc via _escalate_arc
        from ...agent.model_resolver import get_model_for_role, get_next_model
        # Find current model
        current_model = None
        child_config_id = arc.get("agent_config_id")
        if child_config_id:
            cfg = get_agent_config(child_config_id)
            if cfg:
                current_model = cfg["model"]
        if not current_model:
            current_model = get_model_for_role("default_step")
        next_model = get_next_model(current_model, "general")
        if next_model:
            _escalate_arc(arc_id, next_model)
        return

    # policy == "replan" (default): enqueue work item for parent re-invocation
    try:
        from ..engine import work_queue
        work_queue.enqueue(
            "arc.child_failed",
            {
                "parent_id": parent_id,
                "failed_child_id": arc_id,
                "failed_child_name": arc["name"],
                "failed_child_goal": arc["goal"],
            },
            idempotency_key=f"child_failed:{parent_id}:{arc_id}",
        )
        from ..engine import main_loop
        main_loop.wake_signal.set()
    except (ImportError, sqlite3.Error) as _exc:
        logger.exception("Failed to enqueue child_failed work item for arc %d", arc_id)


def _escalate_arc(arc_id: int, next_model: str) -> int | None:
    """Create an escalated sibling arc with a stronger model.

    - If root arc (no parent): creates a new root arc with next_model
    - If child arc: creates a sibling with same step_order + parent_id
    - Marks original arc as 'escalated'
    - Stores _escalated_from metadata

    Returns the new arc ID, or None on failure.
    """
    arc = get_arc(arc_id)
    if arc is None:
        return None

    # Build new agent config for the escalated model
    new_config_id = get_or_create_agent_config(model=next_model)

    # Create the escalated arc
    new_arc_id = create_arc(
        name=f"{arc['name']} (escalated)",
        goal=arc["goal"],
        parent_id=arc["parent_id"],
        step_order=arc["step_order"],
        agent_config_id=new_config_id,
        agent_type=arc["agent_type"],
        integrity_level=arc["integrity_level"],
        output_type=arc["output_type"],
    )

    # Mark original as escalated
    try:
        update_status(arc_id, "escalated")
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
        grant_read_access(
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

    arc = get_arc(arc_id)
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
        policy_row = get_model_policy(policy_id)
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

    # Find current model from agent_config or arc_state
    current_model = None
    config_id = arc.get("agent_config_id")
    if config_id:
        cfg = get_agent_config(config_id)
        if cfg:
            current_model = cfg["model"]

    if not current_model:
        # Legacy fallback: check arc_state
        with db_connection() as db:
            row = db.execute(
                "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = '_model'",
                (arc_id,),
            ).fetchone()
        if row:
            current_model = json.loads(row["value_json"])

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
    new_policy_id = get_or_create_model_policy(
        model=None,
        agent_role=policy_row.get("agent_role"),
        temperature=policy_row.get("temperature"),
        max_tokens=policy_row.get("max_tokens"),
        policy_json=new_policy_json,
        name=f"{policy_row.get('name', '')} (escalated q>={new_min})",
    )

    # Create the escalated arc
    new_arc_id = create_arc(
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
        update_status(arc_id, "escalated")
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
        grant_read_access(
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


def check_dependencies_detailed(arc_id: int) -> dict:
    """Check dependencies with detailed failure information.

    Returns dict with:
        satisfied: bool — all preceding siblings completed
        blocked_by_pending: list[int] — pending/active/waiting predecessors
        blocked_by_failed: list[int] — failed predecessors
        failed_predecessors: list[dict] — name/goal/status of failed predecessors
    """
    with db_connection() as db:
        arc = db.execute(
            "SELECT parent_id, step_order FROM arcs WHERE id = ?", (arc_id,)
        ).fetchone()
        if arc is None or arc["parent_id"] is None:
            return {
                "satisfied": True,
                "blocked_by_pending": [],
                "blocked_by_failed": [],
                "failed_predecessors": [],
            }

        preceding = db.execute(
            "SELECT id, name, goal, status FROM arcs "
            "WHERE parent_id = ? AND step_order < ?",
            (arc["parent_id"], arc["step_order"]),
        ).fetchall()

        if not preceding:
            return {
                "satisfied": True,
                "blocked_by_pending": [],
                "blocked_by_failed": [],
                "failed_predecessors": [],
            }

        blocked_by_pending = []
        blocked_by_failed = []
        failed_predecessors = []

        for row in preceding:
            if row["status"] in DONE_STATUSES:
                continue
            elif row["status"] == "failed":
                blocked_by_failed.append(row["id"])
                failed_predecessors.append({
                    "id": row["id"],
                    "name": row["name"],
                    "goal": row["goal"],
                    "status": row["status"],
                })
            else:
                blocked_by_pending.append(row["id"])

        satisfied = not blocked_by_pending and not blocked_by_failed

        return {
            "satisfied": satisfied,
            "blocked_by_pending": blocked_by_pending,
            "blocked_by_failed": blocked_by_failed,
            "failed_predecessors": failed_predecessors,
        }


# ── Performance counters ──────────────────────────────────────────


def _walk_ancestors(db, arc_id: int) -> list[int]:
    """Return list of ancestor arc IDs (parent, grandparent, ...).

    Walks up the parent chain from the given arc.
    Does not include the arc itself.
    """
    ancestors = []
    current_id = arc_id
    while True:
        row = db.execute(
            "SELECT parent_id FROM arcs WHERE id = ?", (current_id,)
        ).fetchone()
        if row is None or row["parent_id"] is None:
            break
        ancestors.append(row["parent_id"])
        current_id = row["parent_id"]
    return ancestors


def increment_ancestor_arc_count(arc_id: int, _db_conn=None) -> None:
    """Increment descendant_arc_count for all ancestors of the given arc.

    Called after a new arc is created as a child.
    """
    owns_connection = _db_conn is None
    db = _db_conn if _db_conn else get_db()
    try:
        ancestors = _walk_ancestors(db, arc_id)
        now = datetime.now(timezone.utc).isoformat()
        for ancestor_id in ancestors:
            db.execute(
                "UPDATE arcs SET descendant_arc_count = descendant_arc_count + 1, "
                "updated_at = ? WHERE id = ?",
                (now, ancestor_id),
            )
        if owns_connection:
            db.commit()
    finally:
        if owns_connection:
            db.close()


def increment_ancestor_executions(arc_id: int) -> None:
    """Increment descendant_executions for the arc and all its ancestors.

    Called after a code execution completes for the given arc.
    The arc itself also gets incremented (it is its own ancestor for
    counting purposes when viewed from a parent).
    """
    with db_transaction() as db:
        now = datetime.now(timezone.utc).isoformat()
        ancestors = _walk_ancestors(db, arc_id)
        for ancestor_id in ancestors:
            db.execute(
                "UPDATE arcs SET descendant_executions = descendant_executions + 1, "
                "updated_at = ? WHERE id = ?",
                (now, ancestor_id),
            )


def increment_ancestor_tokens(arc_id: int, tokens: int) -> None:
    """Increment descendant_tokens for all ancestors of the given arc.

    Called after an API call completes for work associated with the given arc.

    Args:
        arc_id: The arc that consumed tokens.
        tokens: Total tokens (input + output) to add.
    """
    if tokens <= 0:
        return
    with db_transaction() as db:
        now = datetime.now(timezone.utc).isoformat()
        ancestors = _walk_ancestors(db, arc_id)
        for ancestor_id in ancestors:
            db.execute(
                "UPDATE arcs SET descendant_tokens = descendant_tokens + ?, "
                "updated_at = ? WHERE id = ?",
                (tokens, now, ancestor_id),
            )


# ── Agent config helpers ───────────────────────────────────────────


def get_or_create_agent_config(
    model: str,
    agent_role: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    _db_conn=None,
) -> int:
    """Get or create an agent_config row. Returns the config ID.

    Uses INSERT OR IGNORE + SELECT with COALESCE-based dedup to ensure
    identical parameter sets share a single row.

    Args:
        _db_conn: Optional existing database connection (reuses transaction).
    """
    owns_connection = _db_conn is None
    db = _db_conn if _db_conn else get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO agent_configs "
            "(model, agent_role, temperature, max_tokens) "
            "VALUES (?, ?, ?, ?)",
            (model, agent_role, temperature, max_tokens),
        )
        if owns_connection:
            db.commit()

        row = db.execute(
            "SELECT id FROM agent_configs "
            "WHERE model = ? "
            "AND COALESCE(agent_role, '') = COALESCE(?, '') "
            "AND COALESCE(temperature, -1) = COALESCE(?, -1) "
            "AND COALESCE(max_tokens, -1) = COALESCE(?, -1)",
            (model, agent_role, temperature, max_tokens),
        ).fetchone()
        return row["id"]
    finally:
        if owns_connection:
            db.close()


def get_agent_config(config_id: int) -> dict | None:
    """Get an agent_config by ID. Returns dict or None."""
    with db_connection() as db:
        row = db.execute(
            "SELECT * FROM agent_configs WHERE id = ?", (config_id,)
        ).fetchone()
        return dict(row) if row else None


def get_or_create_model_policy(
    model: str | None = None,
    agent_role: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    policy_json: str | None = None,
    name: str | None = None,
    _db_conn=None,
) -> int:
    """Get or create a model_policy row. Returns the policy ID.

    For policies with policy_json (selector-based), deduplicates by
    (model, agent_role, temperature, max_tokens, policy_json).
    For hard-pinned policies (no policy_json), deduplicates by
    (model, agent_role, temperature, max_tokens) like agent_configs.

    Args:
        _db_conn: Optional existing database connection (reuses transaction).
    """
    owns_connection = _db_conn is None
    db = _db_conn if _db_conn else get_db()
    try:
        # Try exact match first
        if policy_json:
            row = db.execute(
                "SELECT id FROM model_policies "
                "WHERE COALESCE(model, '') = COALESCE(?, '') "
                "AND COALESCE(agent_role, '') = COALESCE(?, '') "
                "AND COALESCE(temperature, -1) = COALESCE(?, -1) "
                "AND COALESCE(max_tokens, -1) = COALESCE(?, -1) "
                "AND policy_json = ?",
                (model, agent_role, temperature, max_tokens, policy_json),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT id FROM model_policies "
                "WHERE COALESCE(model, '') = COALESCE(?, '') "
                "AND COALESCE(agent_role, '') = COALESCE(?, '') "
                "AND COALESCE(temperature, -1) = COALESCE(?, -1) "
                "AND COALESCE(max_tokens, -1) = COALESCE(?, -1) "
                "AND policy_json IS NULL",
                (model, agent_role, temperature, max_tokens),
            ).fetchone()

        if row:
            return row["id"]

        # Create new
        cursor = db.execute(
            "INSERT INTO model_policies "
            "(name, model, agent_role, temperature, max_tokens, policy_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, model, agent_role, temperature, max_tokens, policy_json),
        )
        if owns_connection:
            db.commit()
        return cursor.lastrowid
    finally:
        if owns_connection:
            db.close()


def get_model_policy(policy_id: int) -> dict | None:
    """Get a model_policy by ID. Returns dict or None."""
    with db_connection() as db:
        row = db.execute(
            "SELECT * FROM model_policies WHERE id = ?", (policy_id,)
        ).fetchone()
        return dict(row) if row else None


def grant_read_access(
    reader_arc_id: int,
    target_arc_id: int,
    depth: str = "subtree",
    reason: str | None = None,
    granted_by: str | None = None,
) -> int:
    """Grant cross-arc read access from reader to target.

    Args:
        reader_arc_id: Arc that will gain read access.
        target_arc_id: Arc (or subtree root) to be readable.
        depth: 'self' (exact arc only) or 'subtree' (arc and all descendants).
        reason: Human-readable reason for the grant.
        granted_by: Origin of the grant ('platform', 'parent:<id>', 'chat:<conv_id>').

    Returns:
        The grant row ID.

    Raises:
        ValueError: If depth is invalid or either arc does not exist.
    """
    if depth not in ("self", "subtree"):
        raise ValueError(f"Invalid depth: {depth!r}. Must be 'self' or 'subtree'.")

    with db_transaction() as db:
        # Validate both arcs exist
        for arc_id, label in ((reader_arc_id, "reader"), (target_arc_id, "target")):
            row = db.execute("SELECT id FROM arcs WHERE id = ?", (arc_id,)).fetchone()
            if row is None:
                raise ValueError(f"{label.title()} arc {arc_id} not found")

        cursor = db.execute(
            "INSERT OR REPLACE INTO arc_read_grants "
            "(reader_arc_id, target_arc_id, depth, reason, granted_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (reader_arc_id, target_arc_id, depth, reason, granted_by),
        )
        grant_id = cursor.lastrowid
        return grant_id


def has_read_grant(reader_arc_id: int, target_arc_id: int) -> bool:
    """Check if reader has a read grant covering target.

    Two checks:
    1. Direct grant: reader → target (any depth).
    2. Subtree grant: reader → some ancestor of target with depth='subtree'.

    Returns True if any grant covers the target.
    """
    with db_connection() as db:
        # Check 1: direct grant
        row = db.execute(
            "SELECT id FROM arc_read_grants "
            "WHERE reader_arc_id = ? AND target_arc_id = ?",
            (reader_arc_id, target_arc_id),
        ).fetchone()
        if row is not None:
            return True

        # Check 2: walk up target's parent chain looking for subtree grant
        current_id = target_arc_id
        for _ in range(100):  # max depth guard
            parent_row = db.execute(
                "SELECT parent_id FROM arcs WHERE id = ?", (current_id,)
            ).fetchone()
            if parent_row is None or parent_row["parent_id"] is None:
                break
            parent_id = parent_row["parent_id"]
            grant_row = db.execute(
                "SELECT id FROM arc_read_grants "
                "WHERE reader_arc_id = ? AND target_arc_id = ? AND depth = 'subtree'",
                (reader_arc_id, parent_id),
            ).fetchone()
            if grant_row is not None:
                return True
            current_id = parent_id

        return False


def list_read_grants(arc_id: int) -> list[dict]:
    """List all read grants where the given arc is the reader.

    Returns list of dicts with grant details.
    """
    with db_connection() as db:
        rows = db.execute(
            "SELECT * FROM arc_read_grants WHERE reader_arc_id = ? ORDER BY id",
            (arc_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_policy_id_by_name(name: str, _db_conn=None) -> int | None:
    """Look up model_policy ID by name.

    Args:
        name: Policy preset name (e.g., "fast-chat", "careful-coding")
        _db_conn: Optional existing DB connection. Callers inside a
            ``db_transaction()`` should pass their connection so this
            read reuses the transaction's snapshot rather than opening
            a second connection.

    Returns:
        Policy ID or None if not found
    """
    if _db_conn is not None:
        row = _db_conn.execute(
            "SELECT id FROM model_policies WHERE name = ?",
            (name,)
        ).fetchone()
        return row["id"] if row else None
    with db_connection() as db:
        row = db.execute(
            "SELECT id FROM model_policies WHERE name = ?",
            (name,)
        ).fetchone()
        return row["id"] if row else None


def get_policy_by_name(name: str) -> dict | None:
    """Look up full model_policy by name.

    Args:
        name: Policy preset name (e.g., "fast-chat", "careful-coding")

    Returns:
        Policy dict (id, name, model, policy_json, etc.) or None if not found
    """
    with db_connection() as db:
        row = db.execute(
            "SELECT * FROM model_policies WHERE name = ?",
            (name,)
        ).fetchone()
        return dict(row) if row else None


def update_arc_counters(arc_id: int) -> None:
    """Recompute all performance counters for an arc from its descendants.

    This is a full recount (not incremental) — useful for consistency
    checks or recovery. Counts:
    - descendant_arc_count: total number of descendant arcs
    - descendant_executions: total code_executions for descendant arcs
    - descendant_tokens: total API tokens (input + output) for descendant arcs

    Args:
        arc_id: The arc whose counters should be recomputed.
    """
    with db_transaction() as db:
        # Count descendant arcs using recursive CTE
        row = db.execute(
            "WITH RECURSIVE subtree AS ( "
            "  SELECT id FROM arcs WHERE parent_id = ? "
            "  UNION ALL "
            "  SELECT a.id FROM arcs a "
            "  INNER JOIN subtree s ON a.parent_id = s.id "
            ") "
            "SELECT COUNT(*) AS cnt FROM subtree",
            (arc_id,),
        ).fetchone()
        desc_arc_count = row["cnt"] if row else 0

        # Count executions for descendant arcs
        row = db.execute(
            "WITH RECURSIVE subtree AS ( "
            "  SELECT id FROM arcs WHERE parent_id = ? "
            "  UNION ALL "
            "  SELECT a.id FROM arcs a "
            "  INNER JOIN subtree s ON a.parent_id = s.id "
            ") "
            "SELECT COUNT(*) AS cnt FROM code_executions ce "
            "INNER JOIN code_files cf ON ce.code_file_id = cf.id "
            "WHERE cf.arc_id IN (SELECT id FROM subtree)",
            (arc_id,),
        ).fetchone()
        desc_executions = row["cnt"] if row else 0

        # Sum tokens for descendant arcs (via conversation_arcs link)
        row = db.execute(
            "WITH RECURSIVE subtree AS ( "
            "  SELECT id FROM arcs WHERE parent_id = ? "
            "  UNION ALL "
            "  SELECT a.id FROM arcs a "
            "  INNER JOIN subtree s ON a.parent_id = s.id "
            ") "
            "SELECT COALESCE(SUM(ac.input_tokens + ac.output_tokens), 0) AS total "
            "FROM api_calls ac "
            "INNER JOIN conversation_arcs ca ON ac.conversation_id = ca.conversation_id "
            "WHERE ca.arc_id IN (SELECT id FROM subtree)",
            (arc_id,),
        ).fetchone()
        desc_tokens = row["total"] if row else 0

        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "UPDATE arcs SET "
            "descendant_arc_count = ?, "
            "descendant_executions = ?, "
            "descendant_tokens = ?, "
            "updated_at = ? "
            "WHERE id = ?",
            (desc_arc_count, desc_executions, desc_tokens, now, arc_id),
        )
