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
  standard ``invoke_coding_change`` entry point. Each action's
  ``target_path`` drives workflow selection directly — the workflow
  selector routes ``kb-change`` on an existing entry when the path
  resolves to one. Actions whose ``target_path`` matches a diary-shape
  pattern (``reflections/*``, ``by-day/*``, dated components, etc.)
  are dropped as safety-net protection against the "reintroduce a
  diary" failure mode; the drop is recorded on this arc's
  ``_dispatch_dropped_diary_targets`` state for provenance.

All four are registered in ``__init__.py`` via :func:`register_handlers`.
Reflection is driven by a daily cron (see :mod:`.daily_tick`).
"""

from __future__ import annotations

import json
import logging
import re

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

    # "Prefer edit over create": for each focus pointer surfaced by
    # triage, run ``KBStore.search`` and append a compact "Nearby KB
    # entries" block to the goal. The reflect prompt (see
    # ``reflect-goal.md``) instructs the agent to set a proposed
    # action's ``target_path`` to one of these nearby entries before
    # proposing a fresh path.
    nearby_block, nearby_paths = _build_nearby_kb_block(
        triage_result.focus_pointers,
    )
    if nearby_block:
        goal = f"{goal}\n\n{nearby_block}"
    # Provenance marker: record which KB paths were surfaced to the
    # reflect agent as edit candidates. Deterministic assertion surface
    # for acceptance stories, and useful for debugging why the agent
    # picked (or missed) a given ``kb_edit_target``.
    set_arc_state(arc_id, "_reflect_nearby_kb_paths", nearby_paths)

    agent_config, _fallbacks, _selected, _degraded = resolve_dispatch_model(
        arc_id, arc_info,
    )
    conv_id = _find_arc_conversation_id(arc_id)
    try:
        await _run_arc_agent(arc_id, goal, conv_id, agent_config=agent_config)
    except Exception as exc:
        # Record a small provenance marker so ``save-reflection`` can
        # distinguish "agent failed" from "agent returned nothing".
        # Without this, downstream reads of ``_agent_response`` return
        # empty and the failure is invisible in provenance.
        set_arc_state(
            arc_id, "_reflect_error", type(exc).__name__,
        )
        logger.exception(
            "reflect arc %d: LLM invocation failed — freezing without result",
            arc_id,
        )
    arc_manager.freeze_arc(arc_id)
    _propagate_completion(arc_id)


# Cap how many focus pointers we search for nearby entries, and how many
# results per pointer end up in the goal. Kept snug so the goal doesn't
# balloon on wide-net triage output.
_NEARBY_MAX_POINTERS = 5
_NEARBY_MAX_RESULTS_PER_POINTER = 3
# Character cap on a single ``description`` field when rendered.
_NEARBY_DESCRIPTION_MAX_CHARS = 160


def _build_nearby_kb_block(focus_pointers) -> tuple[str, list[str]]:
    """Return ``(markdown_block, matched_paths)`` for the reflect goal.

    For each focus pointer (arc id, KB path, or tool name), search the
    KB for topically-adjacent entries and list the top hits. The reflect
    prompt uses this list to prefer editing an existing entry over
    creating a new one.

    Returns ``("", [])`` when the KB store is unavailable, when there
    are no pointers, or when no queries produced any hits.
    ``matched_paths`` preserves discovery order, deduped, and is
    recorded on ``arc_state["_reflect_nearby_kb_paths"]`` for provenance
    and deterministic acceptance-test assertions.
    """
    if not focus_pointers:
        return "", []
    try:
        from carpenter.kb import get_store
        store = get_store()
    except Exception:  # pragma: no cover — degrades to no nearby-block
        logger.debug("nearby-KB lookup: KB store unavailable")
        return "", []

    seen_paths: set[str] = set()
    ordered_paths: list[str] = []
    lines: list[str] = []
    for pointer in list(focus_pointers)[:_NEARBY_MAX_POINTERS]:
        query = _pointer_to_query(pointer)
        if not query:
            continue
        try:
            hits = store.search(
                query, max_results=_NEARBY_MAX_RESULTS_PER_POINTER,
            )
        except Exception:
            logger.debug("nearby-KB search failed for pointer %r", pointer)
            continue
        for hit in hits or []:
            path = hit.get("path")
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            ordered_paths.append(path)
            title = hit.get("title") or path
            desc = (hit.get("description") or "").strip()
            if len(desc) > _NEARBY_DESCRIPTION_MAX_CHARS:
                desc = desc[:_NEARBY_DESCRIPTION_MAX_CHARS].rstrip() + "…"
            lines.append(
                f"- `{path}` — {title}"
                + (f": {desc}" if desc else "")
                + f"  (matched pointer: {pointer!r})"
            )
    if not lines:
        return "", []

    parts = [
        "## Nearby KB entries",
        "",
        (
            "For each focus pointer surfaced by triage, the platform "
            "searched the KB and found these topically-adjacent existing "
            "entries. Prefer editing one of these over creating a new "
            "entry — set the proposed action's ``target_path`` to a "
            "path below (verbatim) when the lesson belongs there."
        ),
        "",
    ]
    parts.extend(lines)
    return "\n".join(parts), ordered_paths


def _pointer_to_query(pointer) -> str:
    """Convert a triage focus pointer into a KB search query.

    - ``"#123"`` (arc id) → the arc's goal/name (best available signal).
    - ``"skills/foo/bar"`` (KB path) → the path itself + entry title if
      it exists.
    - anything else (tool name, free text) → used verbatim.
    """
    if not isinstance(pointer, str):
        return ""
    p = pointer.strip()
    if not p:
        return ""
    if p.startswith("#"):
        try:
            arc_id = int(p.lstrip("#"))
        except ValueError:
            return p
        arc = arc_manager.get_arc(arc_id)
        if not arc:
            return p
        return " ".join(
            s for s in (arc.get("name") or "", arc.get("goal") or "") if s
        ) or p
    return p


# Path-component prefixes that indicate a per-time-period diary entry.
# These are structural (KB-layout) diary shapes — kept in sync with the
# ``## Never write per-time-period diary entries`` section of
# ``reflect-goal.md``.
_DIARY_PREFIXES: tuple[str, ...] = (
    "reflections/",
    "by-day/",
    "by-arc/",
    "daily/",
    "weekly/",
    "monthly/",
)
# Any path component matching ISO-date-shaped ``YYYY-MM-DD`` also
# indicates a diary entry regardless of prefix.
_DATE_COMPONENT_RE = re.compile(r"(?:^|[/_-])\d{4}-\d{2}-\d{2}(?:[/_-]|$)")


def _is_diary_path(path: str) -> bool:
    """Return True when ``path`` looks like a per-time-period diary entry.

    Belt-and-braces safety net: the reflect prompt already forbids
    these; this predicate is the handler-level backstop that filters
    LLM output that ignored the prompt. Match rules:

    - starts with any prefix in ``_DIARY_PREFIXES`` (case-insensitive)
    - contains an ISO date component like ``2026-06-19``
    """
    if not path:
        return False
    lower = path.lower().lstrip("/")
    for prefix in _DIARY_PREFIXES:
        if lower.startswith(prefix):
            return True
    if _DATE_COMPONENT_RE.search(lower):
        return True
    return False


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

    # If the reflect step raised, ``_reflect_error`` was written on the
    # reflect arc so save-reflection can record the failure honestly
    # rather than logging an empty summary as a routine skip.
    reflect_error = None
    if reflect_arc_id is not None:
        reflect_error = _get_arc_state(reflect_arc_id, "_reflect_error")

    provenance = {
        "summary": summary,
        "proposed_action_count": n_actions,
        "subject_kind": (subject or {}).get("kind"),
        "model": model_resolver.get_model_for_role("reflection"),
    }
    if reflect_error:
        provenance["reflect_error"] = reflect_error
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
        "save-reflection arc %d completed: %d proposed action(s) — "
        "no diary KB write (v2)",
        arc_id, n_actions,
    )


async def handle_dispatch_actions(arc_id: int, arc_info: dict) -> None:
    """Fan out reflection-proposed actions through the standard coding-change pipeline.

    Each proposed action is routed to ``handle_invoke_coding_change``,
    which picks the appropriate workflow template
    (``coding-change`` / ``yaml-change`` / ``kb-change``) via
    :func:`select_workflow_for_paths` and applies the standard
    force-human gate for T1/T0 paths. Each action's ``target_path`` is
    the single source of truth for what the action touches; when it
    matches an existing KB entry the selector routes to ``kb-change``.

    Actions whose ``target_path`` looks like a per-time-period diary
    entry (see :func:`_is_diary_path`) are dropped before dispatch.
    The prompt already forbids these paths; this handler is the
    belt-and-braces safety net that records dropped targets on
    ``_dispatch_dropped_diary_targets`` for provenance and monitoring
    of LLM compliance.
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
    dropped_diary_targets: list[dict] = []
    kept: list = []
    for action in proposed:
        tp = (action.target_path or "").strip()
        if tp and _is_diary_path(tp):
            dropped_diary_targets.append({
                "target_path": tp,
                "action_type": action.action_type or "other",
                "description": (action.description or "").strip()[:200],
            })
            logger.warning(
                "dispatch-actions arc %d: dropping proposed action with "
                "diary-shaped target_path %r (LLM ignored the prompt's "
                "no-diary rule)",
                arc_id, tp,
            )
            continue
        kept.append(action)
    proposed = kept
    if dropped_diary_targets:
        set_arc_state(
            arc_id, "_dispatch_dropped_diary_targets", dropped_diary_targets,
        )

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
            "dropped_diary_targets": len(dropped_diary_targets),
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

        # The action's target_path is the sole source of truth for
        # what the action touches. When it names an existing KB entry
        # the workflow selector routes to kb-change on that entry;
        # when it names a new path the selector routes to a fresh
        # kb-change/coding-change as appropriate.
        effective_paths: list[str] = [action_target] if action_target else []
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
        "dropped_diary_targets": len(dropped_diary_targets),
    })

    arc_manager.update_status(arc_id, "completed")
    arc_manager.freeze_arc(arc_id)

    from carpenter.core.arcs.dispatch_handler import _propagate_completion
    _propagate_completion(arc_id)

    logger.info(
        "dispatch-actions arc %d: spawned %d child arcs from %d proposed "
        "actions (%d truncated by cap=%d, %d dropped as diary paths)",
        arc_id, len(spawned_arcs), total_proposed, truncated_count, cap,
        len(dropped_diary_targets),
    )
