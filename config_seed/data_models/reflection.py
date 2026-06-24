"""Data models for the reflection workflow.

These attrs models define the structured data contracts between
reflection arc steps:

- ``GatheredActivity`` — what ``gather-activity`` produces for the
  ``reflect`` step.
- ``ReflectionResult`` (with ``ProposedAction``) — what ``reflect``
  produces for ``save-reflection`` and ``dispatch-actions``.
"""
from __future__ import annotations

from typing import Any

import attrs


@attrs.define
class GatheredActivity:
    """Activity data prepared for a reflection.

    ``content`` is the markdown block (today produced by
    :func:`activity_gatherer.gather_from_subject`) that frames what
    the reflect-step agent should analyse. The remaining fields capture
    the subject shape so downstream steps can key reflections, route
    actions, and apply taint rules without re-reading parent state.
    """

    content: str
    source_arc_ids: list[int] = attrs.Factory(list)
    subject_kind: str = ""
    subject_refs: list[int] | None = None
    window: dict[str, Any] | None = None


@attrs.define
class ProposedAction:
    """A single action proposed by the reflection."""

    description: str
    target_path: str | None = None
    action_type: str = "other"


@attrs.define
class ReflectionResult:
    """Output of the reflect step.

    ``summary`` is the free-form reflection text persisted to KB by the
    ``save-reflection`` step. ``proposed_actions`` are the structured
    follow-ups the ``dispatch-actions`` step fans out into child arcs.
    """

    summary: str
    proposed_actions: list[ProposedAction] = attrs.Factory(list)
