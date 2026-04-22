"""Shared helper for creating untrusted arc batches.

Extracts the validation + Fernet key generation + ``review_keys`` wiring
logic out of :func:`carpenter.tool_backends.arc.handle_create_batch` so
both the tool handler *and* YAML template instantiation can produce the
same arc structure without going through the tool layer.

The canonical shape of an untrusted batch is:

- At least one arc with ``integrity_level != "trusted"`` (EXECUTOR).
- At least one ``REVIEWER`` or ``JUDGE`` arc (all reviewers see every
  tainted target in the batch).
- At most one ``JUDGE``; if present it must carry the highest
  ``step_order`` among reviewer-style arcs.
- All arcs share the same ``parent_id``.

The helper enforces those invariants inside a single database
transaction, materialises the arcs via
:func:`carpenter.core.arcs.manager.create_arc` (using the existing
``_allow_tainted`` kwarg to bypass the individual-untrusted guard), and
wires up Fernet keys + ``_reviewer_profile`` / ``_review_target``
``arc_state`` rows.

Callers should provide ``arc_specs`` with the same keys accepted by
``handle_create_batch``: ``name``, ``goal``, ``integrity_level``,
``output_type``, ``agent_type``, ``step_order``, ``reviewer_profile``
(or ``agent_role``), plus optional ``model_policy`` / ``model_policy_id``
and any extra kwargs understood by ``create_arc`` (``model_role``,
``agent_role``, ``arc_role`` …).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from ... import config
from ...db import db_transaction
from ..arcs import manager as arc_manager
from .audit import log_trust_event
from .integrity import is_non_trusted
from .types import (
    validate_agent_type,
    validate_integrity_level,
    validate_output_type,
)

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet  # type: ignore
    HAS_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover - exercised only when crypto missing
    HAS_CRYPTOGRAPHY = False


# Keys that are consumed by the validation layer / wiring logic.  Any
# other key in ``arc_specs[i]`` is forwarded as a kwarg to ``create_arc``.
_CREATE_ARC_KWARGS = {
    "agent_type",
    "integrity_level",
    "output_type",
    "agent_role",
    "model",
    "model_role",
    "agent_model",
    "arc_role",
    "model_policy_id",
    "wait_until",
    "output_contract",
    "temperature",
    "max_tokens",
    "agent_config_id",
    "code_file_id",
    "template_id",
    "from_template",
    "template_mutable",
    "timeout_minutes",
    "verification_target_id",
}


def _validate_batch(
    arc_specs: list[dict[str, Any]],
    parent_id: int | None,
) -> str | None:
    """Validate batch-level invariants. Returns an error string or None."""
    if not arc_specs:
        return "No arcs provided"

    parent_ids = {spec.get("parent_id", parent_id) for spec in arc_specs}
    if len(parent_ids) > 1:
        return "All arcs must have the same parent_id"

    agent_roles = config.CONFIG.get("agent_roles", {})
    for spec in arc_specs:
        agent_type = spec.get("agent_type", "EXECUTOR")
        if agent_type in ("REVIEWER", "JUDGE"):
            role_name = spec.get("reviewer_profile") or spec.get("agent_role")
            if not role_name:
                return (
                    f"Arc '{spec.get('name')}' is {agent_type} but missing "
                    "agent_role/reviewer_profile"
                )
            if role_name not in agent_roles:
                return (
                    f"Unknown agent_role '{role_name}' for arc "
                    f"'{spec.get('name')}'"
                )

    tainted = [
        s for s in arc_specs
        if is_non_trusted(s.get("integrity_level", "trusted"))
    ]
    reviewers = [
        s for s in arc_specs
        if s.get("agent_type") in ("REVIEWER", "JUDGE")
    ]
    judges = [s for s in arc_specs if s.get("agent_type") == "JUDGE"]

    if tainted and not reviewers:
        return "Untrusted arcs require at least one REVIEWER or JUDGE arc"
    if len(judges) > 1:
        return "Maximum one JUDGE arc allowed per batch"

    return None


def _assign_step_orders(
    db,
    arc_specs: list[dict[str, Any]],
    parent_id: int | None,
) -> None:
    """Fill in missing ``step_order`` values using the next available slot."""
    if parent_id is not None:
        row = db.execute(
            "SELECT COALESCE(MAX(step_order), -1) AS max_order "
            "FROM arcs WHERE parent_id = ?",
            (parent_id,),
        ).fetchone()
        next_step = row["max_order"] + 1
    else:
        next_step = 0

    for spec in arc_specs:
        if "step_order" not in spec or spec["step_order"] is None:
            spec["step_order"] = next_step
            next_step += 1


def _resolve_model_policy(spec: dict[str, Any]) -> None:
    """Resolve ``model_policy`` preset name to ``model_policy_id`` in place.

    Tries the selector-preset registry first (matching how
    ``template_manager.instantiate_template`` resolves presets); falls
    back to a DB lookup by policy name (matching the pre-refactor
    ``handle_create_batch`` behaviour).
    """
    name = spec.get("model_policy")
    if not name or spec.get("model_policy_id") is not None:
        return
    try:
        from ..models.selector import get_presets
        preset = get_presets().get(name)
        if preset is not None:
            policy_json = preset.to_policy_json()
            spec["model_policy_id"] = arc_manager.get_or_create_model_policy(
                model=preset.model,
                agent_role=preset.agent_role,
                temperature=preset.temperature,
                max_tokens=preset.max_tokens,
                policy_json=policy_json,
                name=name,
            )
            return
    except (ImportError, KeyError, ValueError, TypeError):
        logger.exception("Failed to resolve model_policy preset %r", name)

    # Legacy fallback: plain DB lookup by policy name.
    try:
        policy_id = arc_manager.get_policy_id_by_name(name)
        if policy_id is not None:
            spec["model_policy_id"] = policy_id
    except (sqlite3.Error, ValueError):
        logger.exception("Failed to resolve model_policy by name %r", name)


def create_untrusted_batch(
    arc_specs: list[dict[str, Any]],
    parent_id: int | None = None,
) -> dict[str, Any]:
    """Atomically create a batch of arcs (trusted or untrusted).

    Performs all the side-effects previously implemented inline in
    :func:`carpenter.tool_backends.arc.handle_create_batch`:

    1. Validates batch invariants (shared parent, reviewer coverage,
       at most one judge, judge-highest-order).
    2. Creates each arc via :func:`arc_manager.create_arc`, passing
       ``_allow_tainted=True`` for non-trusted specs.
    3. For every ``(tainted_target, reviewer)`` pair, generates a
       Fernet key and inserts it into ``review_keys`` (if the
       ``cryptography`` library is available and encryption is
       enforced).
    4. Persists ``_reviewer_profile`` on each REVIEWER/JUDGE arc and
       ``_review_target`` pointing at each tainted target.

    Args:
        arc_specs: List of arc specs. Each spec may override
            ``parent_id`` but all ``parent_id`` values must agree.
        parent_id: Default parent_id used when a spec does not supply
            one; also used as the fallback for the batch-level
            invariant check.

    Returns:
        ``{"arc_ids": [int, ...]}`` on success, or ``{"error": str}``.
    """
    # Normalise parent_id so validation sees a consistent value.
    for spec in arc_specs:
        if "parent_id" not in spec:
            spec["parent_id"] = parent_id

    err = _validate_batch(arc_specs, parent_id)
    if err:
        return {"error": err}

    effective_parent = arc_specs[0].get("parent_id") if arc_specs else parent_id

    audit_events: list[tuple[int, str, dict]] = []
    created_ids: list[int] = []

    # Resolve ``model_policy`` preset names *outside* the main transaction:
    # ``get_or_create_model_policy`` opens its own connection, which would
    # deadlock with an already-held write lock.
    for spec in arc_specs:
        _resolve_model_policy(spec)

    try:
        with db_transaction() as db:
            _assign_step_orders(db, arc_specs, effective_parent)

            # Validate judge step_order now that orders are assigned.
            judges = [s for s in arc_specs if s.get("agent_type") == "JUDGE"]
            reviewers = [
                s for s in arc_specs
                if s.get("agent_type") in ("REVIEWER", "JUDGE")
            ]
            if judges:
                judge = judges[0]
                judge_order = judge["step_order"]
                for rev in reviewers:
                    if rev is not judge and rev["step_order"] >= judge_order:
                        return {
                            "error": (
                                "JUDGE must have highest step_order among "
                                "all reviewer/judge arcs"
                            )
                        }

            for spec in arc_specs:
                integrity_level = validate_integrity_level(
                    spec.get("integrity_level", "trusted")
                )
                output_type = validate_output_type(
                    spec.get("output_type", "python")
                )
                agent_type = validate_agent_type(
                    spec.get("agent_type", "EXECUTOR")
                )

                # Forward recognised kwargs to create_arc.
                kwargs: dict[str, Any] = {}
                for key, value in spec.items():
                    if key in _CREATE_ARC_KWARGS:
                        kwargs[key] = value
                kwargs["integrity_level"] = integrity_level
                kwargs["output_type"] = output_type
                kwargs["agent_type"] = agent_type

                # ``reviewer_profile`` is stored in ``arc_state`` (below),
                # mirroring the legacy ``handle_create_batch`` contract.
                # We deliberately do NOT promote it into ``agent_role`` —
                # that would force ``create_arc`` to resolve a model and
                # create an agent_config_id for reviewers, which the
                # original batch handler avoided.

                arc_id = arc_manager.create_arc(
                    name=spec.get("name", ""),
                    goal=spec.get("goal"),
                    parent_id=spec.get("parent_id"),
                    step_order=spec["step_order"],
                    _allow_tainted=is_non_trusted(integrity_level),
                    _db_conn=db,
                    _audit_queue=audit_events,
                    **kwargs,
                )
                created_ids.append(arc_id)

                if spec.get("reviewer_profile"):
                    db.execute(
                        "INSERT INTO arc_state (arc_id, key, value_json) "
                        "VALUES (?, ?, ?)",
                        (
                            arc_id,
                            "_reviewer_profile",
                            json.dumps(spec["reviewer_profile"]),
                        ),
                    )

            # Wire reviewers to tainted targets + generate Fernet keys.
            reviewer_indices = [
                i for i, s in enumerate(arc_specs)
                if s.get("agent_type") in ("REVIEWER", "JUDGE")
            ]
            tainted_indices = [
                i for i, s in enumerate(arc_specs)
                if is_non_trusted(s.get("integrity_level", "trusted"))
            ]

            for t_idx in tainted_indices:
                tainted_arc_id = created_ids[t_idx]
                reviewer_ids = [created_ids[i] for i in reviewer_indices]

                if reviewer_ids:
                    if not HAS_CRYPTOGRAPHY:
                        enforce = config.CONFIG.get(
                            "encryption", {}
                        ).get("enforce", True)
                        if enforce:
                            raise RuntimeError(
                                "Cannot create tainted arc: cryptography "
                                "library is not available. Encryption is "
                                "required for tainted arc output "
                                "(encryption.enforce=true). Install with: "
                                "pip install cryptography>=41.0 "
                                "Or set encryption.enforce=false in "
                                "config.yaml to allow plaintext fallback."
                            )
                        logger.warning(
                            "cryptography library not available - "
                            "cannot encrypt arc %d output.",
                            tainted_arc_id,
                        )
                        audit_events.append((
                            tainted_arc_id,
                            "encryption_unavailable",
                            {
                                "reason": "cryptography_library_missing",
                                "reviewer_count": len(reviewer_ids),
                            },
                        ))
                    else:
                        key = Fernet.generate_key()
                        for reviewer_id in reviewer_ids:
                            db.execute(
                                "INSERT INTO review_keys "
                                "(target_arc_id, reviewer_arc_id, "
                                " fernet_key_encrypted) VALUES (?, ?, ?) "
                                "ON CONFLICT(target_arc_id, reviewer_arc_id) "
                                "DO UPDATE SET fernet_key_encrypted = "
                                "excluded.fernet_key_encrypted",
                                (tainted_arc_id, reviewer_id, key),
                            )
                        audit_events.append((
                            tainted_arc_id,
                            "encryption_key_created",
                            {"reviewer_count": len(reviewer_ids)},
                        ))

                for r_idx in reviewer_indices:
                    reviewer_arc_id = created_ids[r_idx]
                    db.execute(
                        "INSERT INTO arc_state (arc_id, key, value_json) "
                        "VALUES (?, ?, ?)",
                        (
                            reviewer_arc_id,
                            "_review_target",
                            json.dumps(tainted_arc_id),
                        ),
                    )
    except RuntimeError as exc:
        # Encryption unavailable + enforced — surface as error rather than
        # raising (matches the historical contract of handle_create_batch).
        return {"error": str(exc)}
    except Exception as exc:  # broad catch matching original handler
        logger.exception("create_untrusted_batch failed")
        return {"error": str(exc)}

    # Post-commit: flush queued audit events.
    for arc_id, event_type, details in audit_events:
        try:
            log_trust_event(arc_id, event_type, details)
        except (ImportError, sqlite3.Error):
            pass  # audit logging must not fail batch creation

    return {"arc_ids": created_ids}
