"""Webhook -> Resource wrap pipeline (Phase B PR B2 of the Resource refactor).

When a webhook arrives and its ``webhook_subscriptions`` row has
``resource_content_type`` set, the platform writes the payload as a
raw-ingest Resource and spawns a REVIEWER (+ optional JUDGE) arc
template pipeline to process it.

The raw Resource's ``produced_by_arc_id`` is set to the REVIEWER arc
(the first reviewer "owns" the raw Resource per the design: it exists
to be processed by that REVIEWER).  The derived Resource is pre-created
pending, with ``produced_by_template`` from the registry entry matched
on ``consumes_content_type``.

Auto-approve (``auto_approve_verdict=1`` on the subscription) is a user
config override to the "nothing starts trusted" default:
  - The JUDGE arc is NOT spawned.
  - The REVIEWER arc's state carries ``_auto_approve_resource_id`` and
    ``_review_target_resource_id``.
  - On REVIEWER ``completed`` transition, ``manager.update_status``
    calls :func:`apply_auto_approve_on_completion` which marks the
    derived Resource's ``template_verdict`` approved.

This keeps the auto-approve codepath in a single small module without
bolting extra logic onto ``review_manager``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ...db import db_transaction
from ..resources import (
    create_resource,
    derive_resource,
    get_template_for,
    link_arc_resource,
    mark_template_verdict,
    resource_storage_path,
    update_resource_content_stats,
    hash_file,
)
from ._arc_state import get_arc_state, set_arc_state

logger = logging.getLogger(__name__)


def wrap_webhook_as_resource(
    *,
    webhook_id: str,
    payload: dict,
    parsed: dict,
    subscription: dict,
) -> dict | None:
    """Wrap an incoming webhook payload as a Resource + arc pipeline.

    Args:
        webhook_id: The webhook identifier from the URL.
        payload: The full incoming webhook body (raw JSON dict).
        parsed: The normalized event dict from the source-type parser
            (e.g. Forgejo PR extraction).  Goes into arc state alongside
            the Resource so downstream handlers can read structured
            fields without re-parsing.
        subscription: The ``webhook_subscriptions`` row dict.

    Returns:
        Dict with keys ``parent_arc_id``, ``reviewer_arc_id``,
        ``judge_arc_id`` (may be None when auto-approve),
        ``raw_resource_id``, ``derived_resource_id``.  Or ``None`` if the
        resource_content_type has no registered template (caller should
        fall back to the legacy path).
    """
    content_type = subscription.get("resource_content_type")
    if not content_type:
        return None

    template = get_template_for(content_type)
    if template is None:
        logger.warning(
            "webhook %s: resource_content_type=%r has no registered "
            "template; falling back to legacy behaviour",
            webhook_id, content_type,
        )
        return None

    auto_approve = bool(subscription.get("auto_approve_verdict", 0))
    conversation_id = subscription.get("conversation_id")

    # --- Arc pipeline ------------------------------------------------------
    #
    # Build: parent PLANNER -> REVIEWER [+ JUDGE] .  We go through the
    # create_batch backend because it handles Fernet key wiring and
    # review-key setup for the REVIEWER/JUDGE trust boundary.
    from ..arcs import manager as arc_manager
    from ..engine import work_queue as _wq
    from ...tool_backends import arc as arc_backend

    goal = (
        subscription.get("source_config")
        or {}
    )
    # subscription rows come back with JSON fields as strings; normalize.
    if isinstance(subscription.get("source_config"), str):
        try:
            goal = json.loads(subscription["source_config"]) or {}
        except (TypeError, json.JSONDecodeError):
            goal = {}
    handler_goal = subscription.get("description") or (
        f"Process webhook {webhook_id}"
    )

    parent_name = f"Process webhook {webhook_id}"
    parent_goal = (
        f"Ingest, review, and surface webhook payload {webhook_id} "
        f"(content_type={content_type})."
    )

    parent_id = arc_manager.create_arc(
        name=parent_name,
        goal=parent_goal,
        agent_type="PLANNER",
    )

    # Link parent to conversation so arc.chat_notify will deliver results.
    if conversation_id:
        from ...agent import conversation as _conv
        _conv.link_arc_to_conversation(conversation_id, parent_id)

    arc_manager.update_status(parent_id, "active")

    # Build the child arcs.  Auto-approve path has only a REVIEWER (no JUDGE).
    reviewer_goal_template = template.get(
        "goal_template",
        "Read the untrusted resource at {input_path} and write a clean "
        "summary to {output_path}. Handler goal: {goal}.",
    )
    # At batch-create time we don't yet know the file paths — the REVIEWER
    # reads them out of arc state.  Use the template's goal_template just
    # as a description; the REVIEWER arc prompt directs it to arc state.
    reviewer_goal = (
        reviewer_goal_template
        .replace("{input_path}", "(see arc state key 'raw_resource_path')")
        .replace("{output_path}", "(see arc state key 'derived_resource_path')")
        .replace("{goal}", handler_goal)
    )
    reviewer_goal += (
        "\n\nAfter writing your summary, call resource.finalize with the "
        "derived_resource_id from arc state and deprecate_inputs=True. "
        "Store the summary in arc state key '_agent_response' as well "
        "for the chat notify path."
    )

    arcs_spec: list[dict] = [
        {
            "name": "Review webhook payload",
            "goal": reviewer_goal,
            "parent_id": parent_id,
            "agent_type": "REVIEWER",
            "integrity_level": "trusted",
            "reviewer_profile": template.get(
                "reviewer_profile", "security-reviewer"
            ),
            "model_policy": template.get("model_policy", "fast-chat"),
            "step_order": 0,
        },
    ]
    if not auto_approve:
        arcs_spec.append({
            "name": "Validate webhook review",
            "goal": (
                "Validate that the reviewer's extraction is accurate and "
                "safe. When approving, call resource.submit_verdict with "
                "the derived_resource_id from arc state and "
                "verdict='approved' (or 'rejected' if unsafe/incorrect). "
                "Copy the final answer to arc state key '_agent_response'."
            ),
            "parent_id": parent_id,
            "agent_type": "JUDGE",
            "integrity_level": "trusted",
            "reviewer_profile": template.get("judge_profile", "judge"),
            "step_order": 1,
        })

    batch_result = arc_backend.handle_create_batch({"arcs": arcs_spec})
    if "error" in batch_result:
        try:
            arc_manager.update_status(parent_id, "failed")
        except ValueError:
            pass
        logger.error(
            "webhook %s: failed to create arc batch: %s",
            webhook_id, batch_result["error"],
        )
        return None

    child_ids = batch_result["arc_ids"]
    reviewer_arc_id = child_ids[0]
    judge_arc_id = child_ids[1] if not auto_approve else None

    # --- Resource wiring --------------------------------------------------
    #
    # Raw Resource: produced_by_arc_id = REVIEWER (the first reviewer arc
    # "owns" the raw payload).  This is the architectural decision in
    # the B2 plan: the audit trail reads "this resource exists to be
    # processed by REVIEWER arc N."
    raw_resource_id = create_resource(
        content_type=content_type,
        file_path=None,  # placeholder; filled in after we know the id
        produced_by_arc_id=reviewer_arc_id,
        source_descriptor=f"webhook:{webhook_id}",
    )
    raw_path = resource_storage_path(raw_resource_id, "blob")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    raw_path.write_bytes(payload_bytes)
    byte_size, content_hash = hash_file(raw_path)
    # Fill file_path + content stats in one UPDATE.
    with db_transaction() as _db:
        _db.execute(
            "UPDATE resources SET file_path = ?, byte_size = ?, "
            "content_hash = ? WHERE id = ?",
            (str(raw_path), byte_size, content_hash, raw_resource_id),
        )

    # Derived Resource: pre-created pending.
    derived_resource_id = derive_resource(
        content_type=template.get("produces_content_type", "text-summary"),
        file_path=None,
        produced_by_arc_id=reviewer_arc_id,
        produced_by_template=template["name"],
        template_verdict="pending",
        source_descriptor=f"webhook:{webhook_id}",
    )
    derived_path = resource_storage_path(derived_resource_id, "blob")
    derived_path.parent.mkdir(parents=True, exist_ok=True)
    with db_transaction() as _db:
        _db.execute(
            "UPDATE resources SET file_path = ? WHERE id = ?",
            (str(derived_path), derived_resource_id),
        )

    # Links: REVIEWER reads raw (input), writes derived (output).
    link_arc_resource(
        arc_id=reviewer_arc_id, resource_id=raw_resource_id, role="input"
    )
    link_arc_resource(
        arc_id=reviewer_arc_id, resource_id=derived_resource_id, role="output"
    )

    # --- Arc state pre-seeding -------------------------------------------
    set_arc_state(reviewer_arc_id, "raw_resource_path", str(raw_path))
    set_arc_state(reviewer_arc_id, "raw_resource_id", raw_resource_id)
    set_arc_state(reviewer_arc_id, "derived_resource_path", str(derived_path))
    set_arc_state(reviewer_arc_id, "derived_resource_id", derived_resource_id)
    set_arc_state(reviewer_arc_id, "webhook_id", webhook_id)
    set_arc_state(reviewer_arc_id, "parsed_event", parsed)

    if auto_approve:
        # REVIEWER completion flips the verdict (handled in
        # manager.update_status -> apply_auto_approve_on_completion).
        set_arc_state(reviewer_arc_id, "_auto_approve_resource_id", derived_resource_id)
        set_arc_state(reviewer_arc_id, "_review_target_resource_id", derived_resource_id)
    else:
        # JUDGE verdict flips the derived Resource's template_verdict via
        # review_manager._apply_resource_verdict_if_any (PR2 wiring).
        # _review_target is what submit_verdict uses to validate the
        # reviewer arc is designated for the arc being reviewed.  In the
        # webhook wrap path the JUDGE validates the REVIEWER's output,
        # so its target arc is the REVIEWER.
        set_arc_state(judge_arc_id, "_review_target", reviewer_arc_id)
        set_arc_state(judge_arc_id, "_review_target_resource_id", derived_resource_id)
        set_arc_state(judge_arc_id, "derived_resource_id", derived_resource_id)
        set_arc_state(judge_arc_id, "webhook_id", webhook_id)

    # Parent exposes the derived Resource as primary for chat notify.
    set_arc_state(parent_id, "_primary_resource_id", derived_resource_id)
    set_arc_state(parent_id, "webhook_id", webhook_id)

    if conversation_id:
        from ...agent import conversation as _conv
        for child_id in child_ids:
            _conv.link_arc_to_conversation(conversation_id, child_id)

    # Enqueue the REVIEWER dispatch (JUDGE, if spawned, will be dispatched
    # by the normal step-order chain after the REVIEWER completes).
    _wq.enqueue(
        "arc.dispatch",
        {"arc_id": reviewer_arc_id},
        idempotency_key=f"arc_dispatch:{reviewer_arc_id}",
    )

    logger.info(
        "webhook %s: wrapped as Resource %d (derived %d), reviewer=%d, "
        "judge=%s, auto_approve=%s",
        webhook_id, raw_resource_id, derived_resource_id,
        reviewer_arc_id, judge_arc_id, auto_approve,
    )

    return {
        "parent_arc_id": parent_id,
        "reviewer_arc_id": reviewer_arc_id,
        "judge_arc_id": judge_arc_id,
        "raw_resource_id": raw_resource_id,
        "derived_resource_id": derived_resource_id,
    }


def apply_auto_approve_on_completion(arc_id: int) -> bool:
    """If ``arc_id``'s state carries auto-approve keys, flip the verdict.

    Called from ``arcs.manager.update_status`` on every ``completed``
    transition.  Silent no-op for arcs that weren't set up via the
    auto-approve webhook path (the overwhelming majority).

    Returns True if a verdict was applied, False otherwise.
    """
    resource_id = get_arc_state(arc_id, "_auto_approve_resource_id", None)
    if resource_id is None or not isinstance(resource_id, int):
        return False
    try:
        mark_template_verdict(resource_id, "approved")
    except ValueError as exc:
        logger.warning(
            "webhook auto-approve: mark_template_verdict(%s, 'approved') "
            "from arc %s failed: %s",
            resource_id, arc_id, exc,
        )
        return False
    logger.info(
        "webhook auto-approve: REVIEWER arc %d completion approved "
        "Resource %d",
        arc_id, resource_id,
    )
    return True
