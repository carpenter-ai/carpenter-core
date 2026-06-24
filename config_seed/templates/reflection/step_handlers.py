"""Python step handlers for the reflection template.

Three Python-only steps ship in this package:

- :func:`handle_gather_activity` — pre-reflect step. Reads the typed
  reflection *subject* off the parent arc (see ``_subject.py``), calls
  :func:`activity_gatherer.gather_from_subject`, packages the result
  into a ``GatheredActivity`` data model, and writes it as this arc's
  typed output. The reflect step reads that output as its typed input
  and renders a stable instruction prompt against it.

- :func:`handle_save_reflection` — post-reflect step. Reads the sibling
  ``reflect`` arc's ``_agent_response``, validates it as a
  ``ReflectionResult``, and enqueues the KB write keyed on the subject
  (``by-arc/{id}``, ``by-day/{date}``, or ``by-theme/{slug}``).

- :func:`handle_dispatch_actions` — post-save step. Reads the sibling
  ``reflect`` arc's ``ReflectionResult``, iterates its structured
  ``proposed_actions``, applies the ``_is_batch_restricted`` predicate
  (taint roll-up: any non-trusted arc in the batch routes the *whole
  batch's* actions through the gated human-review templates), and
  spawns one child arc per action up to
  ``reflection.max_actions_per_reflection`` (default 5).

All three are registered in ``__init__.py`` via :func:`register_handlers`.
Reflection is driven by a daily cron (see :mod:`.daily_tick`).
"""

from __future__ import annotations

import json
import logging

import cattrs

from carpenter import config
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.arcs.data_model_validation import validate_contract
from carpenter.core.engine.arc_outputs import (
    find_sibling_arc_id,
    set_arc_output,
)
from carpenter.core.workflows._arc_state import (
    get_arc_state as _get_arc_state,
    set_arc_state,
)
from carpenter.db import db_connection
from carpenter.agent import conversation as conv_module
from carpenter.agent import model_resolver
from carpenter.prompts import load_prompt_template

from .proposed_action_parser import classify_action
from .reflection_storage import save_reflection

logger = logging.getLogger(__name__)


GATHERED_ACTIVITY_OUTPUT = "gathered_activity"
REFLECTION_RESULT_OUTPUT = "reflection_result"

_GATHERED_ACTIVITY_CONTRACT = "data_models.reflection:GatheredActivity"
_REFLECTION_RESULT_CONTRACT = "data_models.reflection:ReflectionResult"


# (action_type, review_mode) → (template_name, prompt_name).
#
# ``review_mode`` is ``"auto"`` for clean reflections and ``"human"`` when
# the reflected arc (or any descendant) was non-trusted — the latter
# routes to the gated template variants, which add an ``await-approval``
# step gated on ``arc.manual_trigger`` ahead of ``execute-action``.
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


def _read_reflection_result(reflect_arc_id: int):
    """Validate the reflect arc's ``_agent_response`` as a ``ReflectionResult``.

    The reflect step is an EXECUTOR whose ``_agent_response`` is
    constrained by the ``reflect-goal`` prompt to be a single JSON object
    matching the ``ReflectionResult`` schema. We tolerate the agent
    wrapping the JSON in a Markdown fence, but the JSON must be present.

    Returns the validated model, or ``None`` when the response is
    missing/unparseable beyond the plain-summary fallback. Plain-text
    responses are wrapped as ``ReflectionResult(summary=text)`` with no
    proposed actions.
    """
    from data_models.reflection import ReflectionResult  # type: ignore

    raw = _get_arc_state(reflect_arc_id, "_agent_response")
    if not raw:
        logger.warning(
            "reflect arc %d has no _agent_response — treating as empty reflection",
            reflect_arc_id,
        )
        return None

    text = raw.strip() if isinstance(raw, str) else raw
    if isinstance(text, str):
        fenced = text
        if fenced.startswith("```"):
            fenced = fenced.split("\n", 1)[1] if "\n" in fenced else ""
            if fenced.endswith("```"):
                fenced = fenced[: -3]
            text = fenced.strip()
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            logger.warning(
                "reflect arc %d: _agent_response is not valid JSON — "
                "treating as plain summary with no proposed actions",
                reflect_arc_id,
            )
            return ReflectionResult(summary=str(raw), proposed_actions=[])
    else:
        payload = text

    try:
        return validate_contract(payload, _REFLECTION_RESULT_CONTRACT)
    except (cattrs.errors.ClassValidationError, ValueError, TypeError):
        logger.warning(
            "reflect arc %d: _agent_response does not match ReflectionResult "
            "schema — treating as plain summary with no proposed actions",
            reflect_arc_id, exc_info=True,
        )
        return ReflectionResult(summary=str(raw), proposed_actions=[])


async def handle_gather_activity(arc_id: int, arc_info: dict) -> None:
    """Build a ``GatheredActivity`` and write it as this arc's typed output."""
    from . import activity_gatherer
    from ._subject import get_subject, subject_arc_ids
    from data_models.reflection import GatheredActivity  # type: ignore

    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    parent_id = arc_info["parent_id"]
    subject = get_subject(parent_id)
    if subject is None:
        logger.warning(
            "gather-activity arc %d: parent %d has no reflection_subject",
            arc_id, parent_id,
        )
        content = "# Reflection — no subject specified\n"
        gathered = GatheredActivity(content=content)
    else:
        content = activity_gatherer.gather_from_subject(subject)
        gathered = GatheredActivity(
            content=content,
            source_arc_ids=subject_arc_ids(subject),
            subject_kind=subject.get("kind", ""),
            subject_refs=list(subject.get("refs", []) or []) or None,
            window=subject.get("window") or None,
        )

    set_arc_output(
        arc_id, GATHERED_ACTIVITY_OUTPUT, cattrs.unstructure(gathered),
    )

    reflect_arc_id = find_sibling_arc_id(arc_id, "analyze")
    if reflect_arc_id is not None:
        try:
            reflect_goal = load_prompt_template(
                "reflect-goal",
                context={"activity_content": content},
                subdirectory="reflections",
            )
        except FileNotFoundError:
            reflect_goal = content
        from carpenter.db import db_transaction
        with db_transaction() as db:
            db.execute(
                "UPDATE arcs SET goal = ? WHERE id = ?",
                (reflect_goal, reflect_arc_id),
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
        "gather-activity arc %d completed: %d chars of activity for reflect arc %s",
        arc_id, len(content), reflect_arc_id,
    )


async def handle_save_reflection(arc_id: int, arc_info: dict) -> None:
    """Persist the reflect step's ``ReflectionResult.summary`` to KB."""
    from ._subject import get_subject

    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    parent_id = arc_info["parent_id"]
    subject = get_subject(parent_id)

    reflect_arc_id = find_sibling_arc_id(arc_id, "analyze")
    if reflect_arc_id is None:
        logger.warning(
            "save-reflection arc %d: no sibling reflect arc (role=analyze) "
            "under parent %d",
            arc_id, parent_id,
        )
        summary = "(No reflection output)"
    else:
        result = _read_reflection_result(reflect_arc_id)
        summary = result.summary if result is not None else "(No reflection output)"

    model = model_resolver.get_model_for_role("reflection")

    if subject is None:
        logger.warning(
            "save-reflection arc %d: parent %d has no reflection_subject — "
            "keying the reflection on the parent arc id as a fallback",
            arc_id, parent_id,
        )
        subject = {"kind": "arcs", "refs": [parent_id]}

    save_reflection(subject, summary, model=model)

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
    """A batch is restricted if *any* arc in it would be restricted."""
    if not arc_ids:
        return _is_reflection_restricted(None, proposed_action)
    return any(_is_reflection_restricted(aid, proposed_action) for aid in arc_ids)


def _is_reflection_restricted(
    reflected_arc_id: int | None,
    proposed_action: dict | None = None,
) -> bool:
    """Return True if the reflection should route to the gated template.

    Three arms:

    1. **Taint** — if the reflected arc (or any descendant) is non-trusted,
       the reflection drew from tainted inputs.
    2. **Path tier** — if a ``proposed_action`` carries a target filesystem
       path, classify it via :func:`platform_paths.path_tier`. T1 or T0
       targets force human review.
    3. **Change category** — until category-specific workflows exist,
       python/unknown targets fall back to human review.
    """
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
    """Record an integrity audit row for a restricted reflection."""
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
    """Backward-compat alias for :func:`_is_reflection_restricted`."""
    return _is_reflection_restricted(reflected_arc_id, None)


async def handle_dispatch_actions(arc_id: int, arc_info: dict) -> None:
    """Fan out reflection-proposed actions into one child arc per action."""
    from carpenter.core.engine import template_manager

    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    from ._subject import get_subject, subject_arc_ids

    parent_id = arc_info["parent_id"]
    subject = get_subject(parent_id)
    subject_ids = subject_arc_ids(subject)

    reflect_arc_id = find_sibling_arc_id(arc_id, "analyze")
    result = None
    if reflect_arc_id is not None:
        result = _read_reflection_result(reflect_arc_id)
    else:
        logger.warning(
            "dispatch-actions arc %d: no sibling reflect arc (role=analyze)",
            arc_id,
        )

    proposed = list(result.proposed_actions) if result is not None else []

    if not proposed:
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
    total_proposed = len(proposed)
    truncated_count = max(0, total_proposed - cap)
    proposed = proposed[:cap]

    spawned_arcs: list[int] = []
    action_types: list[str] = []
    any_restricted = False

    for i, action in enumerate(proposed):
        action_desc = (action.description or "").strip()
        action_target = action.target_path
        if not action_desc:
            continue

        action_dict = {
            "description": action_desc,
            "target_path": action_target,
        }
        restricted = _is_batch_restricted(subject_ids, action_dict)
        if restricted:
            any_restricted = True
        review_mode = "human" if restricted else "auto"

        action_type = action.action_type or "other"
        if action_type == "other":
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
            child_arc_id = arc_manager.add_child(
                parent_id=parent_id,
                name=f"reflection-action-{i}",
                goal=rendered_goal,
                template_id=template["id"],
            )
            template_manager.instantiate_template(template["id"], child_arc_id)
        except Exception:
            logger.exception(
                "dispatch-actions arc %d: failed to spawn action %d (type=%s)",
                arc_id, i, action_type,
            )
            continue

        set_arc_state(child_arc_id, "action_type", action_type)
        set_arc_state(child_arc_id, "action_description", action_desc)
        if action_target:
            set_arc_state(child_arc_id, "action_target_path", action_target)
        if restricted:
            set_arc_state(child_arc_id, "_review_mode", "human")
            try:
                from carpenter.api.review import create_arc_approval_review
                with db_connection() as db:
                    gate_row = db.execute(
                        "SELECT id FROM arcs WHERE parent_id = ? "
                        "AND name = 'await-approval'",
                        (child_arc_id,),
                    ).fetchone()
                if gate_row is not None:
                    review = create_arc_approval_review(
                        target_arc_id=child_arc_id,
                        gate_arc_id=gate_row["id"],
                        title=f"Reflection-proposed {action_type} action",
                        action_description=action_desc,
                        proposing_arc_id=parent_id,
                    )
                    set_arc_state(child_arc_id, "review_id", review["review_id"])
                    set_arc_state(child_arc_id, "review_url", review["url"])
            except Exception:
                logger.exception(
                    "dispatch-actions arc %d: failed to create arc-approval "
                    "review for child arc %d", arc_id, child_arc_id,
                )

        spawned_arcs.append(child_arc_id)
        action_types.append(action_type)

    is_tainted = any_restricted

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
