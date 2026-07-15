"""Python step handlers for the reflection template.

Four Python-only step handlers ship in this package:

- :func:`handle_gather_activity` — pre-triage step. Reads the typed
  reflection *subject* off the parent arc (see ``_subject.py``), calls
  :func:`activity_gatherer.gather_from_subject` (for the reflect step's
  full-trajectory view) AND
  :func:`activity_gatherer.triage_summary_from_subject` (for the
  triage step's lightweight view), packages both into a
  ``GatheredActivity`` data model, and writes it as this arc's typed
  output.

- :func:`handle_reflect_gated` — the reflect step's Python handler.
  Reads the sibling ``triage`` arc's ``TriageResult``. When
  ``needs_synthesis == false`` it writes an empty ``ReflectionResult``
  and freezes — **no LLM call, no KB write**. When
  ``needs_synthesis == true`` it invokes the standard EXECUTOR agent
  dispatch (via :func:`carpenter.core.arcs.dispatch_handler._run_arc_agent`)
  to synthesise the ``ReflectionResult`` from the full-trajectory view.

- :func:`handle_save_reflection` — post-reflect step. Records
  provenance on this arc's state. The legacy per-day / per-arc KB
  "diary" writes (``reflections/by-day/{date}``,
  ``reflections/by-arc/{arc_id}``) were removed with the v2 pipeline;
  KB writes now flow *only* through ``dispatch-actions`` → kb-change
  action arcs (which produce reviewed, human-approved diffs). Any
  same-content re-write there is deduped by the platform's generic
  ``kb.write_entry`` handler on a content-hash check.

- :func:`handle_dispatch_actions` — post-save step. Reads the sibling
  ``reflect`` arc's ``ReflectionResult``, iterates its structured
  ``proposed_actions``, and dispatches each one through the platform's
  standard ``invoke_coding_change`` entry point. When
  ``kb_edit_targets`` is populated on the reflect output, edit-target
  paths take precedence over any per-action ``target_path`` for
  workflow selection (routing to ``kb-change`` on an existing path
  rather than creating a new entry).

All four are registered in ``__init__.py`` via :func:`register_handlers`.
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

logger = logging.getLogger(__name__)


GATHERED_ACTIVITY_OUTPUT = "gathered_activity"
REFLECTION_RESULT_OUTPUT = "reflection_result"
TRIAGE_RESULT_OUTPUT = "triage_result"

_GATHERED_ACTIVITY_CONTRACT = "data_models.reflection:GatheredActivity"
_TRIAGE_RESULT_CONTRACT = "data_models.reflection:TriageResult"
_REFLECTION_RESULT_CONTRACT = "data_models.reflection:ReflectionResult"


def _parse_agent_json_response(raw, contract_str, arc_id, fallback_summary_key=None):
    """Parse an EXECUTOR ``_agent_response`` as JSON matching ``contract_str``.

    Tolerates a ``` fence around the JSON. On any parse/validation
    failure returns ``None`` (caller decides the fallback).
    """
    if not raw:
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
                "arc %d: agent response is not valid JSON for contract %s",
                arc_id, contract_str,
            )
            return None
    else:
        payload = text
    try:
        return validate_contract(payload, contract_str)
    except (cattrs.errors.ClassValidationError, ValueError, TypeError):
        logger.warning(
            "arc %d: agent response does not validate against %s",
            arc_id, contract_str, exc_info=True,
        )
        return None


def _read_triage_result(triage_arc_id: int):
    """Return the sibling triage arc's ``TriageResult``, or a bias-toward-false default."""
    from data_models.reflection import TriageResult  # type: ignore

    raw = _get_arc_state(triage_arc_id, "_agent_response")
    parsed = _parse_agent_json_response(
        raw, _TRIAGE_RESULT_CONTRACT, triage_arc_id,
    )
    if parsed is None:
        # Bias-toward-false: any parse failure or missing response is
        # treated as "no synthesis needed" per the plan's
        # associative-memory intent.
        return TriageResult(
            needs_synthesis=False,
            reasons=["triage output missing or unparseable"],
            focus_pointers=[],
        )
    return parsed


def _read_reflection_result(reflect_arc_id: int):
    """Validate the reflect arc's ``_agent_response`` as a ``ReflectionResult``."""
    from data_models.reflection import ReflectionResult  # type: ignore

    raw = _get_arc_state(reflect_arc_id, "_agent_response")
    if not raw:
        logger.warning(
            "reflect arc %d has no _agent_response — treating as empty reflection",
            reflect_arc_id,
        )
        return None
    parsed = _parse_agent_json_response(
        raw, _REFLECTION_RESULT_CONTRACT, reflect_arc_id,
    )
    if parsed is None:
        # Preserve prior fallback behaviour: wrap plain text as an empty
        # ReflectionResult so save-reflection has something to log.
        return ReflectionResult(summary=str(raw), proposed_actions=[])
    return parsed


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
        triage_summary = "# Triage Summary — no subject specified\n"
        gathered = GatheredActivity(
            content=content, triage_summary=triage_summary,
        )
    else:
        content = activity_gatherer.gather_from_subject(subject)
        triage_summary = activity_gatherer.triage_summary_from_subject(subject)
        gathered = GatheredActivity(
            content=content,
            triage_summary=triage_summary,
            source_arc_ids=subject_arc_ids(subject),
            subject_kind=subject.get("kind", ""),
            subject_refs=list(subject.get("refs", []) or []) or None,
            window=subject.get("window") or None,
        )

    set_arc_output(
        arc_id, GATHERED_ACTIVITY_OUTPUT, cattrs.unstructure(gathered),
    )

    arc_manager.update_status(arc_id, "completed")
    arc_manager.freeze_arc(arc_id)

    from carpenter.core.arcs.dispatch_handler import _propagate_completion
    _propagate_completion(arc_id)

    logger.info(
        "gather-activity arc %d completed: %d chars full / %d chars triage",
        arc_id, len(content), len(triage_summary),
    )


async def handle_reflect_gated(arc_id: int, arc_info: dict) -> None:
    """Reflect step handler — gates on the sibling triage output.

    When triage returned ``needs_synthesis == false`` (or produced no
    parseable output — bias-toward-false), this handler writes an empty
    ``ReflectionResult`` to ``_agent_response`` and freezes without
    invoking the LLM. Downstream ``save-reflection`` and
    ``dispatch-actions`` steps see empty output and no-op — no KB write,
    no action dispatch, no token spend.

    When triage returned ``needs_synthesis == true`` the handler falls
    through to the standard EXECUTOR agent dispatch (via
    :func:`_run_arc_agent`), passing the pre-rendered goal from the
    ``gather-activity`` sibling's ``GatheredActivity.content``.
    """
    from data_models.reflection import ReflectionResult  # type: ignore

    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    parent_id = arc_info["parent_id"]

    triage_arc_id = find_sibling_arc_id(arc_id, "triage")
    triage_result = None
    if triage_arc_id is None:
        # No triage sibling means the template shape changed under us or
        # this is a legacy tree — bias-toward-false: skip synthesis.
        logger.warning(
            "reflect arc %d: no sibling triage arc under parent %d — "
            "skipping synthesis (bias-toward-false default)",
            arc_id, parent_id,
        )
    else:
        triage_result = _read_triage_result(triage_arc_id)

    should_synthesise = bool(
        triage_result and triage_result.needs_synthesis
    )

    if not should_synthesise:
        empty = ReflectionResult(
            summary="",
            proposed_actions=[],
            kb_edit_targets=[],
        )
        set_arc_state(
            arc_id, "_agent_response",
            json.dumps(cattrs.unstructure(empty)),
        )
        set_arc_state(arc_id, "_reflect_gated_skipped", True)
        arc_manager.update_status(arc_id, "completed")
        arc_manager.freeze_arc(arc_id)
        from carpenter.core.arcs.dispatch_handler import _propagate_completion
        _propagate_completion(arc_id)
        logger.info(
            "reflect arc %d: triage said skip (reasons=%s) — no LLM call, "
            "downstream steps will no-op",
            arc_id,
            (triage_result.reasons if triage_result else ["no-triage-sibling"])[:3],
        )
        return

    # Triage flagged — invoke the EXECUTOR agent to synthesise.
    logger.info(
        "reflect arc %d: triage flagged synthesis; %d focus pointer(s): %s",
        arc_id,
        len(triage_result.focus_pointers),
        triage_result.focus_pointers[:5],
    )

    # Reuse the platform's standard EXECUTOR dispatch path so the
    # reflect step's goal_template, prompt_template, and agent_model
    # config from reflection.yaml are honoured identically to any other
    # EXECUTOR step.
    from carpenter.core.arcs.dispatch_handler import (
        _render_goal_from_sibling_output,
        _run_arc_agent,
        _propagate_completion,
    )
    from carpenter.core.arcs.dispatch_model_resolver import resolve_dispatch_model

    rendered_goal = _render_goal_from_sibling_output(arc_id)
    goal = rendered_goal or (
        arc_info.get("goal") or arc_info.get("name") or f"Arc #{arc_id}"
    )
    agent_config, _fallbacks, _selected, _degraded = resolve_dispatch_model(
        arc_id, arc_info,
    )
    conv_id = _find_arc_conversation_id(arc_id)
    try:
        await _run_arc_agent(arc_id, goal, conv_id, agent_config=agent_config)
    except Exception:
        logger.exception(
            "reflect arc %d: LLM invocation failed — freezing without result",
            arc_id,
        )
    arc_manager.freeze_arc(arc_id)
    _propagate_completion(arc_id)


def _find_arc_conversation_id(arc_id: int) -> int | None:
    """Return the earliest linked conversation id for arc_id, or None."""
    with db_connection() as db:
        row = db.execute(
            "SELECT conversation_id FROM conversation_arcs "
            "WHERE arc_id = ? ORDER BY created_at ASC LIMIT 1",
            (arc_id,),
        ).fetchone()
    return int(row["conversation_id"]) if row else None


async def handle_save_reflection(arc_id: int, arc_info: dict) -> None:
    """Record reflection provenance on this arc's state.

    The v2 pipeline explicitly does NOT write a daily "diary" KB entry.
    KB knowledge only lands via ``dispatch-actions`` → reviewed
    kb-change action arcs. This handler exists to close out the step
    cleanly, capture the ReflectionResult on arc_state for auditability
    and any downstream chat notifications, and archive the parent's
    originating conversation as before.
    """
    from ._subject import get_subject

    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    parent_id = arc_info["parent_id"]
    subject = get_subject(parent_id)

    reflect_arc_id = find_sibling_arc_id(arc_id, "analyze")
    result = None
    if reflect_arc_id is not None:
        result = _read_reflection_result(reflect_arc_id)
    else:
        logger.warning(
            "save-reflection arc %d: no sibling reflect arc (role=analyze) "
            "under parent %d",
            arc_id, parent_id,
        )

    summary = (result.summary if result is not None else "") or ""
    n_actions = len(result.proposed_actions) if result is not None else 0
    n_edit_targets = len(result.kb_edit_targets) if result is not None else 0

    provenance = {
        "summary": summary,
        "proposed_action_count": n_actions,
        "kb_edit_target_count": n_edit_targets,
        "subject_kind": (subject or {}).get("kind"),
        "model": model_resolver.get_model_for_role("reflection"),
    }
    set_arc_state(arc_id, "_agent_response", provenance)

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
        "save-reflection arc %d completed: %d proposed action(s), "
        "%d kb_edit_target(s) — no diary KB write (v2)",
        arc_id, n_actions, n_edit_targets,
    )


async def handle_dispatch_actions(arc_id: int, arc_info: dict) -> None:
    """Fan out reflection-proposed actions through the standard coding-change pipeline.

    Each proposed action is routed to ``handle_invoke_coding_change``,
    which picks the appropriate workflow template
    (``coding-change`` / ``yaml-change`` / ``kb-change``) via
    :func:`select_workflow_for_paths` and applies the standard
    force-human gate for T1/T0 paths.

    When the reflect output populates ``kb_edit_targets``, dispatch
    passes those paths as ``affected_paths`` in preference to the
    per-action ``target_path`` so the workflow selector routes the
    action to an ``kb-change`` on an existing entry rather than
    creating a new one.
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
    kb_edit_targets = list(result.kb_edit_targets) if result is not None else []

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

        # Parent the spawned change arc under THIS dispatch-actions arc.
        invoke_params: dict = {
            "source_dir": "platform",
            "prompt": action_desc,
            "parent_id": arc_id,
        }

        # Prefer edit-existing over new-write: when the reflect step
        # populated kb_edit_targets AND this is a kb-type action AND
        # the action either targets one of the edit targets or has no
        # target of its own, route via the kb_edit_targets so
        # select_workflow_for_paths picks kb-change on the existing
        # entry.
        effective_paths: list[str] = []
        if action_type == "kb" and kb_edit_targets:
            if action_target and action_target in kb_edit_targets:
                effective_paths = [action_target]
            elif not action_target:
                effective_paths = list(kb_edit_targets)
            else:
                effective_paths = [action_target]
        elif action_target:
            effective_paths = [action_target]

        if effective_paths:
            invoke_params["affected_paths"] = effective_paths

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
        if effective_paths:
            set_arc_state(
                child_arc_id, "action_effective_paths", effective_paths,
            )
        set_arc_state(child_arc_id, "reflection_parent_arc_id", parent_id)

        spawned_arcs.append(child_arc_id)
        action_types.append(action_type)

    set_arc_state(arc_id, "_agent_response", {
        "spawned_arcs": spawned_arcs,
        "action_types": action_types,
        "total_proposed": total_proposed,
        "truncated": truncated_count,
        "kb_edit_targets": kb_edit_targets,
    })

    arc_manager.update_status(arc_id, "completed")
    arc_manager.freeze_arc(arc_id)

    from carpenter.core.arcs.dispatch_handler import _propagate_completion
    _propagate_completion(arc_id)

    logger.info(
        "dispatch-actions arc %d: spawned %d child arcs from %d proposed "
        "actions (%d truncated by cap=%d, %d kb_edit_targets)",
        arc_id, len(spawned_arcs), total_proposed, truncated_count, cap,
        len(kb_edit_targets),
    )
