"""Data models for the reflection workflow.

These attrs models define the structured data contracts between
reflection arc steps:

- ``GatheredActivity`` — what ``gather-activity`` produces for the
  ``triage`` step (and, on flagged batches, the ``reflect`` step).
- ``TriageResult`` — what ``triage`` produces to gate the reflect step.
- ``ReflectionResult`` (with ``ProposedAction``) — what ``reflect``
  produces for ``save-reflection`` and ``dispatch-actions``.
"""
from __future__ import annotations

from typing import Any

import attrs


@attrs.define
class GatheredActivity:
    """Activity data prepared for a reflection.

    ``content`` is the full markdown block (today produced by
    :func:`activity_gatherer.gather_from_subject`) that frames what the
    reflect-step agent should analyse. ``triage_summary`` is a
    lightweight-but-sufficient view — chat prompts, agent responses,
    and arc-tree signals — that the triage step reads to decide whether
    synthesis is warranted at all. The remaining fields capture the
    subject shape so downstream steps can key reflections, route
    actions, and apply taint rules without re-reading parent state.
    """

    content: str
    triage_summary: str = ""
    source_arc_ids: list[int] = attrs.Factory(list)
    subject_kind: str = ""
    subject_refs: list[int] | None = None
    window: dict[str, Any] | None = None


@attrs.define
class TriageResult:
    """Output of the triage step.

    - ``needs_synthesis`` — should the reflect step run at all?
    - ``reasons`` — short human-readable justifications (for logs / KB
      provenance when synthesis does run).
    - ``focus_pointers`` — concrete pointers (arc ids, KB paths, tool
      names) that Stage B (``reflect``) should zoom in on.

    On ``needs_synthesis == False``, ``focus_pointers`` may be empty and
    the reflect / save / dispatch steps become no-ops for the batch.
    """

    needs_synthesis: bool
    reasons: list[str] = attrs.Factory(list)
    focus_pointers: list[str] = attrs.Factory(list)


@attrs.define
class ProposedAction:
    """A single action proposed by the reflection."""

    description: str
    target_path: str | None = None
    action_type: str = "other"


@attrs.define
class ReflectionResult:
    """Output of the reflect step.

    ``summary`` is the free-form reflection text (kept for provenance;
    no longer written to KB as a diary entry). ``proposed_actions`` are
    the structured follow-ups the ``dispatch-actions`` step fans out
    into child arcs. ``kb_edit_targets`` lists existing KB paths the
    reflect agent proposes editing — dispatch-actions prefers routing to
    edit-existing-entry actions over new-entry actions when populated.
    """

    summary: str
    proposed_actions: list[ProposedAction] = attrs.Factory(list)
    kb_edit_targets: list[str] = attrs.Factory(list)
