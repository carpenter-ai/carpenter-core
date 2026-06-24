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
  ``proposed_actions``, and dispatches each one through the platform's
  standard ``invoke_coding_change`` entry point. The change-workflow
  selector (``select_workflow_for_paths``) routes to
  ``coding-change`` / ``yaml-change`` / ``kb-change`` based on the
  action's ``target_path``, and the standard force-human gate handles
  T1/T0 paths uniformly with all other coding changes.

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

from .proposed_action_parser import classify_action
from .reflection_storage import save_reflection

logger = logging.getLogger(__name__)


GATHERED_ACTIVITY_OUTPUT = "gathered_activity"
REFLECTION_RESULT_OUTPUT = "reflection_result"

_GATHERED_ACTIVITY_CONTRACT = "data_models.reflection:GatheredActivity"
_REFLECTION_RESULT_CONTRACT = "data_models.reflection:ReflectionResult"


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

    # The reflect step's goal is rendered at dispatch time from this
    # typed output by ``dispatch_handler._render_goal_from_sibling_output``
    # — see the ``goal_template`` block on the reflect step in
    # ``reflection.yaml``. No direct write to the sibling arc is needed.

    arc_manager.update_status(arc_id, "completed")
    arc_manager.freeze_arc(arc_id)

    from carpenter.core.arcs.dispatch_handler import _propagate_completion
    _propagate_completion(arc_id)

    logger.info(
        "gather-activity arc %d completed: %d chars of activity",
        arc_id, len(content),
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


async def handle_dispatch_actions(arc_id: int, arc_info: dict) -> None:
    """Fan out reflection-proposed actions through the standard coding-change pipeline.

    Each proposed action is routed to ``handle_invoke_coding_change``,
    which picks the appropriate workflow template
    (``coding-change`` / ``yaml-change`` / ``kb-change``) via
    :func:`select_workflow_for_paths` and applies the standard force-human
    gate for T1/T0 paths — the same path every other coding change takes.
    """
    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    parent_id = arc_info["parent_id"]

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

    from carpenter.tool_backends import arc as arc_backend

    spawned_arcs: list[int] = []
    action_types: list[str] = []

    for i, action in enumerate(proposed):
        action_desc = (action.description or "").strip()
        action_target = action.target_path
        if not action_desc:
            continue

        action_type = action.action_type or "other"
        if action_type == "other":
            action_type = classify_action(action_desc)

        # Parent the spawned change arc under THIS dispatch-actions arc so
        # the action lives inside the reflection tree. When the action fails,
        # _notify_parent_of_failure walks ancestors and finds the reflection
        # SUPERVISOR root (dispatch-actions's parent), waking it.
        invoke_params: dict = {
            "source_dir": "platform",
            "prompt": action_desc,
            "parent_id": arc_id,
        }
        if action_target:
            invoke_params["affected_paths"] = [action_target]

        try:
            invoke_result = arc_backend.handle_invoke_coding_change(invoke_params)
        except Exception:
            logger.exception(
                "dispatch-actions arc %d: failed to spawn action %d (type=%s)",
                arc_id, i, action_type,
            )
            continue

        if not isinstance(invoke_result, dict) or "arc_id" not in invoke_result:
            logger.warning(
                "dispatch-actions arc %d: invoke_coding_change returned no arc_id "
                "for action %d: %r",
                arc_id, i, invoke_result,
            )
            continue

        child_arc_id = invoke_result["arc_id"]

        set_arc_state(child_arc_id, "action_type", action_type)
        set_arc_state(child_arc_id, "action_description", action_desc)
        if action_target:
            set_arc_state(child_arc_id, "action_target_path", action_target)
        set_arc_state(child_arc_id, "reflection_parent_arc_id", parent_id)

        spawned_arcs.append(child_arc_id)
        action_types.append(action_type)

    set_arc_state(arc_id, "_agent_response", {
        "spawned_arcs": spawned_arcs,
        "action_types": action_types,
        "total_proposed": total_proposed,
        "truncated": truncated_count,
    })

    arc_manager.update_status(arc_id, "completed")
    arc_manager.freeze_arc(arc_id)

    from carpenter.core.arcs.dispatch_handler import _propagate_completion
    _propagate_completion(arc_id)

    logger.info(
        "dispatch-actions arc %d: spawned %d child arcs from %d proposed "
        "actions (%d truncated by cap=%d)",
        arc_id, len(spawned_arcs), total_proposed, truncated_count, cap,
    )
