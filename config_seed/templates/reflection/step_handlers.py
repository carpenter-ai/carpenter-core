"""Python step handlers for the reflection template.

Three Python-only steps ship in this package:

- :func:`handle_gather_activity` — pre-reflect step. Reads the typed
  reflection *subject* off the parent arc (see ``_subject.py``), calls
  :func:`activity_gatherer.gather_from_subject` (which dispatches on
  ``kind`` — single-arc, period-batch, or theme), and writes the result
  into the sibling ``reflect`` arc's goal. Then completes and propagates
  so the ``reflect`` step can be dispatched with its goal populated.
  Falls back to a synthesised single-arc subject from the legacy
  ``reflected_arc_id`` for backwards compat.

- :func:`handle_save_reflection` — post-reflect step. Reads the sibling
  ``reflect`` arc's ``_agent_response`` via
  :func:`arc_outputs.find_sibling_arc_id` (``role=analyze``), enqueues
  the KB write via :func:`.reflection_storage.save_reflection` keyed on
  the subject (``by-arc/{id}``, ``by-day/{date}``, or ``by-theme/{slug}``)
  and completes. Auto-action fan-out is owned by the ``dispatch-actions``
  sibling step.

- :func:`handle_dispatch_actions` — post-save step. Parses proposed
  actions out of the reflect output, classifies each, applies the
  ``_is_batch_restricted`` predicate (taint roll-up: any non-trusted arc
  in the batch routes the *whole batch's* actions through the gated
  human-review templates), and spawns one child arc per action up to
  ``reflection.max_actions_per_reflection`` (default 5).

All three are registered in ``__init__.py`` via :func:`register_handlers`.
Reflection is driven by a daily cron (see :mod:`.daily_tick`), not by
per-arc completion — the per-arc trigger formed an unbounded feedback
loop and was replaced by the cadence model.
"""

from __future__ import annotations

import logging

from carpenter import config
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine.arc_outputs import find_sibling_arc_id
from carpenter.core.workflows._arc_state import (
    get_arc_state as _get_arc_state,
)
from carpenter.db import db_connection, db_transaction
from carpenter.agent import conversation as conv_module
from carpenter.agent import model_resolver
from carpenter.core.workflows._arc_state import set_arc_state
from carpenter.prompts import load_prompt_template

from .proposed_action_parser import classify_action, parse_proposed_actions
from .reflection_storage import save_reflection

logger = logging.getLogger(__name__)


# (action_type, review_mode) → (template_name, prompt_name).
#
# ``review_mode`` is ``"auto"`` for clean reflections and ``"human"`` when
# the reflected arc (or any descendant) was non-trusted — the latter
# routes to the gated template variants, which add an ``await-approval``
# step gated on ``arc.manual_trigger`` ahead of ``execute-action``.
#
# Config/other route to KB per legacy ``_submit_other_action``-equivalent
# behavior (see ``reflection_action.py``: non-actionable types get
# recorded; the closest arc analogue is a KB entry documenting the
# proposed change).
_ACTION_TYPE_TO_TEMPLATE = {
    ("kb", "auto"): ("reflection-kb-action", "action-kb"),
    ("code", "auto"): ("reflection-code-action", "action-code"),
    ("config", "auto"): ("reflection-kb-action", "action-kb"),
    ("other", "auto"): ("reflection-kb-action", "action-kb"),
    ("kb", "human"): ("reflection-kb-action-gated", "action-kb"),
    ("code", "human"): ("reflection-code-action-gated", "action-code"),
    ("config", "human"): ("reflection-kb-action-gated", "action-kb"),
    ("other", "human"): ("reflection-kb-action-gated", "action-kb"),
}


async def handle_gather_activity(arc_id: int, arc_info: dict) -> None:
    """Populate the sibling ``reflect`` arc's goal with gathered data.

    The subscription's ``initial_arc_state`` stores
    ``reflected_arc_id`` on the parent. This handler reads it, runs
    :func:`activity_gatherer.gather_from_arc`, and writes the returned
    markdown into the sibling reflect arc's ``goal`` column (the reflect
    step runs an LLM agent and reads its goal from the arcs row).
    """
    from . import activity_gatherer
    from ._subject import get_subject

    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    parent_id = arc_info["parent_id"]
    subject = get_subject(parent_id)
    if subject is None:
        logger.warning(
            "gather-activity arc %d: parent %d has no reflection_subject",
            arc_id, parent_id,
        )
        gathered = "# Reflection — no subject specified\n"
    else:
        gathered = activity_gatherer.gather_from_subject(subject)

    reflect_arc_id = find_sibling_arc_id(arc_id, "analyze")
    if reflect_arc_id is not None:
        with db_transaction() as db:
            db.execute(
                "UPDATE arcs SET goal = ? WHERE id = ?",
                (gathered, reflect_arc_id),
            )
    else:
        logger.warning(
            "gather-activity arc %d: no sibling reflect arc (role=analyze)",
            arc_id,
        )

    arc_manager.update_status(arc_id, "completed")
    arc_manager.freeze_arc(arc_id)

    from carpenter.core.arcs.dispatch_handler import _propagate_completion
    _propagate_completion(arc_id)

    logger.info(
        "gather-activity arc %d completed: %d chars into reflect arc %s",
        arc_id, len(gathered), reflect_arc_id,
    )


async def handle_save_reflection(arc_id: int, arc_info: dict) -> None:
    """Save the reflection from its sibling reflect arc's AI output."""
    from ._subject import get_subject

    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    parent_id = arc_info["parent_id"]
    subject = get_subject(parent_id)

    response_text = "(No reflection output)"
    reflect_arc_id = find_sibling_arc_id(arc_id, "analyze")
    if reflect_arc_id is not None:
        raw = _get_arc_state(reflect_arc_id, "_agent_response")
        if raw:
            response_text = raw
        else:
            logger.warning(
                "save-reflection arc %d: no _agent_response on reflect arc %d",
                arc_id, reflect_arc_id,
            )
    else:
        logger.warning(
            "save-reflection arc %d: no sibling reflect arc (role=analyze) "
            "under parent %d",
            arc_id, parent_id,
        )

    model = model_resolver.get_model_for_role("reflection")

    if subject is None:
        logger.warning(
            "save-reflection arc %d: parent %d has no reflection_subject — "
            "keying the reflection on the parent arc id as a fallback",
            arc_id, parent_id,
        )
        subject = {"kind": "arcs", "refs": [parent_id]}

    save_reflection(subject, response_text, model=model)

    # Auto-action fan-out now lives in the ``dispatch-actions`` template
    # step (see :func:`handle_dispatch_actions`); spawning child arcs per
    # proposed action replaced the legacy ``reflection_actions`` SQL
    # writes that ``process_reflection_actions`` used to do here.

    arc_manager.update_status(arc_id, "completed")
    arc_manager.freeze_arc(arc_id)

    from carpenter.core.arcs.dispatch_handler import _propagate_completion
    _propagate_completion(arc_id)

    with db_connection() as db:
        conv_row = db.execute(
            "SELECT conversation_id FROM conversation_arcs WHERE arc_id = ?",
            (parent_id,),
        ).fetchone()
    if conv_row:
        conv_module.archive_conversation(conv_row["conversation_id"])

    logger.info(
        "save-reflection arc %d completed: KB write enqueued (subject=%s)",
        arc_id, subject.get("kind") if subject else None,
    )


def _is_batch_restricted(arc_ids: list[int], proposed_action: dict | None) -> bool:
    """A batch is restricted if *any* arc in it would be restricted.

    Conservative: one tainted/high-tier arc in a daily batch routes the
    batch's spawned actions through the gated human-review path. With no
    arc ids (e.g. a theme subject), falls back to the path/category arm.
    """
    if not arc_ids:
        return _is_reflection_restricted(None, proposed_action)
    return any(_is_reflection_restricted(aid, proposed_action) for aid in arc_ids)


def _is_reflection_restricted(
    reflected_arc_id: int | None,
    proposed_action: dict | None = None,
) -> bool:
    """Return True if the reflection should route to the gated template.

    Broader than the legacy taint-only check (PR 4 platform-integrity):

    1. **Taint** — if the reflected arc (or any descendant) is non-trusted,
       the reflection drew from tainted inputs.  Spawned action arcs are
       marked for human review.  (Preserved from
       ``_is_reflected_arc_tainted``.)
    2. **Path tier** — if a ``proposed_action`` is supplied and carries a
       target filesystem path, classify it via
       :func:`carpenter.security.platform_paths.path_tier`.  T1 or T0
       targets force human review.
    3. **Change category** — if the proposed action's target is anything
       other than KB/YAML, treat as restricted by default until PR 5
       wires category-specific workflows (``coding-change``,
       ``yaml-change``, ``kb-change``).

    As of PR 7 (close-out), ``parse_proposed_actions`` returns structured
    dicts ``{"description": str, "target_path": str | None}`` extracted
    from backticked path-like tokens in the description.  When the parser
    finds a target path the dispatch loop in
    :func:`handle_dispatch_actions` calls this predicate per-action, so
    the tier and category arms now fire reliably.  Actions without an
    extractable path fall back to the taint-only behavior (preserved for
    backwards compatibility).
    """
    # Taint arm — preserved from the legacy check.
    if reflected_arc_id is not None:
        try:
            with db_connection() as db:
                row = db.execute(
                    "SELECT integrity_level FROM arcs WHERE id = ?",
                    (reflected_arc_id,),
                ).fetchone()
                if (
                    row
                    and row["integrity_level"]
                    and row["integrity_level"] != "trusted"
                ):
                    _audit_reflection_restricted(reflected_arc_id, "taint")
                    return True
                # Check descendants. Cheap recursive walk; reflection arcs
                # are rare and tree depth is bounded, so no CTE needed.
                descendant_rows = db.execute(
                    "WITH RECURSIVE descendants(id) AS ("
                    "  SELECT id FROM arcs WHERE parent_id = ? "
                    "  UNION ALL "
                    "  SELECT a.id FROM arcs a "
                    "  JOIN descendants d ON a.parent_id = d.id"
                    ") "
                    "SELECT a.integrity_level FROM arcs a "
                    "JOIN descendants d ON a.id = d.id "
                    "WHERE a.integrity_level IS NOT NULL "
                    "  AND a.integrity_level != 'trusted' "
                    "LIMIT 1",
                    (reflected_arc_id,),
                ).fetchall()
                if descendant_rows:
                    _audit_reflection_restricted(reflected_arc_id, "taint")
                    return True
        except Exception:
            logger.exception(
                "taint check failed for reflected arc %s; defaulting to untainted",
                reflected_arc_id,
            )
            # Fall through to the path/category arms — defensive.

    # Path/category arms — fire when the parsed action surfaces a target
    # path.  As of PR 7 the parser extracts target_path from backticked
    # path-like tokens in the description, so this path is now live.
    if proposed_action is not None and isinstance(proposed_action, dict):
        target = proposed_action.get("target_path")
        if isinstance(target, str) and target:
            try:
                from carpenter.security.platform_paths import (
                    PATH_TIER_T0,
                    PATH_TIER_T1,
                    change_category,
                    path_tier,
                )
                tier = path_tier(target)
                if tier in (PATH_TIER_T0, PATH_TIER_T1):
                    _audit_reflection_restricted(reflected_arc_id, "tier")
                    return True
                # Until PR 5 wires category-specific workflows, treat
                # python/unknown changes as restricted by default.
                cat = change_category(target)
                if cat in ("python", "unknown"):
                    _audit_reflection_restricted(reflected_arc_id, "category")
                    return True
            except Exception:
                logger.exception(
                    "path/category check failed for reflected arc %s "
                    "target=%r; defaulting to unrestricted",
                    reflected_arc_id, target,
                )

    return False


def _audit_reflection_restricted(
    reflected_arc_id: int | None,
    reason: str,
) -> None:
    """Record an integrity audit row for a restricted reflection.

    Late import of ``audit_path_decision`` keeps the reflection
    template package importable in environments where the security
    module is not yet initialized.  Audit failures are swallowed inside
    the helper.
    """
    try:
        from carpenter.security.platform_paths import audit_path_decision
        audit_path_decision(
            None,
            "reflection_action_restricted",
            "",
            {"reflected_arc_id": reflected_arc_id, "reason": reason},
        )
    except Exception:
        logger.warning(
            "audit of reflection_action_restricted failed "
            "(reflected_arc_id=%s, reason=%s)",
            reflected_arc_id, reason, exc_info=True,
        )


def _is_reflected_arc_tainted(reflected_arc_id: int | None) -> bool:
    """Backward-compat alias for :func:`_is_reflection_restricted`.

    Kept so any out-of-tree caller (or test) referencing the legacy
    name continues to work without modification.
    """
    return _is_reflection_restricted(reflected_arc_id, None)


async def handle_dispatch_actions(arc_id: int, arc_info: dict) -> None:
    """Fan out reflection-proposed actions into one child arc per action.

    Logic:

    1. Locate the sibling reflect arc (``role=analyze``) and read its
       ``_agent_response``.
    2. Parse proposed actions out of the response text (uses
       :func:`.proposed_action_parser.parse_proposed_actions`).
    3. Truncate to ``reflection.max_actions_per_reflection`` (default 5).
    4. For each remaining action: classify, select a template, render the
       corresponding prompt with ``description=action_desc``, and spawn a
       new arc under the reflection parent arc with that prompt as goal.
    5. Record the spawned arc ids / action types on this arc's state so
       the flow is queryable.

    Taint propagation: if the reflected arc (or any descendant) is
    non-trusted, spawned action arcs are tagged with an arc_state flag
    ``_review_mode=human`` (kept for observability — lets you query which
    arcs were spawned tainted) and are instantiated from the *-gated
    template variants instead of the auto variants. The gated templates
    insert an ``await-approval`` step with
    ``activation_event: arc.manual_trigger`` before ``execute-action``,
    so the action arc's dispatch is blocked until an operator emits
    ``arc.manual_trigger``. Clean reflections use the auto variants
    (single-step ``execute-action``) unchanged.
    """
    from carpenter.core.engine import template_manager

    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    from ._subject import get_subject, subject_arc_ids

    parent_id = arc_info["parent_id"]
    subject = get_subject(parent_id)
    subject_ids = subject_arc_ids(subject)

    # Fetch the reflect arc's AI output as the raw proposed-actions text.
    raw_response: str | None = None
    reflect_arc_id = find_sibling_arc_id(arc_id, "analyze")
    if reflect_arc_id is not None:
        raw_response = _get_arc_state(reflect_arc_id, "_agent_response")
    else:
        logger.warning(
            "dispatch-actions arc %d: no sibling reflect arc (role=analyze)",
            arc_id,
        )

    actions = parse_proposed_actions(raw_response)
    if not actions:
        logger.info(
            "dispatch-actions arc %d: no proposed actions in reflect output "
            "(reflect arc=%s) — no-op",
            arc_id, reflect_arc_id,
        )
        set_arc_state(arc_id, "_agent_response", {
            "spawned_arcs": [],
            "action_types": [],
            "total_proposed": 0,
            "truncated": 0,
        })
        arc_manager.update_status(arc_id, "completed")
        arc_manager.freeze_arc(arc_id)
        from carpenter.core.arcs.dispatch_handler import _propagate_completion
        _propagate_completion(arc_id)
        return

    cap = int(
        config.CONFIG.get("reflection", {}).get("max_actions_per_reflection", 5)
    )
    total_proposed = len(actions)
    truncated_count = max(0, total_proposed - cap)
    actions = actions[:cap]

    # PR 7 platform-integrity close-out: ``parse_proposed_actions`` now
    # returns structured ``{"description": ..., "target_path": ...}``
    # dicts.  Compute per-action review_mode so a single tier-T1 action
    # in a batch routes to the gated template even when its siblings are
    # clean.  ``any_restricted`` is recorded on this dispatch arc for
    # observability.
    spawned_arcs: list[int] = []
    action_types: list[str] = []
    any_restricted = False

    for i, action in enumerate(actions):
        if not isinstance(action, dict):
            # Defensive: skip malformed entries rather than crash.
            logger.warning(
                "dispatch-actions arc %d: action %d is not a dict (%r); skipping",
                arc_id, i, type(action).__name__,
            )
            continue
        action_desc = action.get("description", "") or ""
        action_target = action.get("target_path")
        if not action_desc:
            continue

        restricted = _is_batch_restricted(subject_ids, action)
        if restricted:
            any_restricted = True
        review_mode = "human" if restricted else "auto"

        action_type = classify_action(action_desc)
        template_name, prompt_name = _ACTION_TYPE_TO_TEMPLATE.get(
            (action_type, review_mode),
            _ACTION_TYPE_TO_TEMPLATE[("other", review_mode)],
        )

        template = template_manager.get_template_by_name(template_name)
        if template is None:
            logger.warning(
                "dispatch-actions arc %d: template %r not found; skipping action %d",
                arc_id, template_name, i,
            )
            continue

        prompt_context = {"description": action_desc}
        if action_target:
            prompt_context["target_path"] = action_target
        try:
            rendered_goal = load_prompt_template(
                prompt_name,
                context=prompt_context,
                subdirectory="reflections",
            )
        except FileNotFoundError:
            rendered_goal = action_desc

        try:
            # parent_id is the reflection root arc (non-template, mutable).
            child_arc_id = arc_manager.add_child(
                parent_id=parent_id,
                name=f"reflection-action-{i}",
                goal=rendered_goal,
                template_id=template["id"],
            )
            # Instantiate the template's single step as a grandchild.
            template_manager.instantiate_template(template["id"], child_arc_id)
        except Exception:
            logger.exception(
                "dispatch-actions arc %d: failed to spawn action %d (type=%s)",
                arc_id, i, action_type,
            )
            continue

        # Record the original action description + type + taint gating on
        # the spawned arc so tooling / follow-up gating can inspect.
        set_arc_state(child_arc_id, "action_type", action_type)
        set_arc_state(child_arc_id, "action_description", action_desc)
        if action_target:
            set_arc_state(child_arc_id, "action_target_path", action_target)
        if restricted:
            set_arc_state(child_arc_id, "_review_mode", "human")

        spawned_arcs.append(child_arc_id)
        action_types.append(action_type)

    # ``is_tainted`` is preserved as the recorded field name for callers
    # that already query it; the value now means "at least one spawned
    # action was restricted by the broadened predicate".
    is_tainted = any_restricted

    # Record dispatch outcome on this arc for queryability.
    set_arc_state(arc_id, "_agent_response", {
        "spawned_arcs": spawned_arcs,
        "action_types": action_types,
        "total_proposed": total_proposed,
        "truncated": truncated_count,
        "tainted": is_tainted,
    })

    arc_manager.update_status(arc_id, "completed")
    arc_manager.freeze_arc(arc_id)

    from carpenter.core.arcs.dispatch_handler import _propagate_completion
    _propagate_completion(arc_id)

    logger.info(
        "dispatch-actions arc %d: spawned %d child arcs from %d proposed "
        "actions (%d truncated by cap=%d, tainted=%s)",
        arc_id, len(spawned_arcs), total_proposed, truncated_count, cap,
        is_tainted,
    )
