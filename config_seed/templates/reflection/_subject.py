"""Reflection *subject* descriptor — what a reflection is about.

Reflections used to be hard-wired to a single completed arc (a scalar
``reflected_arc_id`` on the reflection parent arc). A subject generalises
that so a reflection can be about:

- ``arcs``   — one or more specific arcs (the legacy per-arc case is just
  ``{kind: arcs, refs: [id]}``).
- ``period`` — everything that completed in a time window (the daily
  cadence batch). ``refs`` are the arcs that populated the window;
  ``window`` carries ``from``/``to``/``date``.
- ``theme``  — a set of related updates under a KB path/slug.

The subject is stored as JSON in ``arc_state`` under the
``reflection_subject`` key on the reflection parent arc. For backward
compatibility, :func:`get_subject` synthesises an ``arcs`` subject from a
legacy ``reflected_arc_id`` when no subject is present.
"""

from __future__ import annotations

from carpenter.core.workflows._arc_state import get_arc_state as _get_arc_state

SUBJECT_KEY = "reflection_subject"

KIND_ARCS = "arcs"
KIND_PERIOD = "period"
KIND_THEME = "theme"
VALID_KINDS = (KIND_ARCS, KIND_PERIOD, KIND_THEME)


def get_subject(parent_id: int) -> dict | None:
    """Read the reflection subject off the parent arc, with legacy fallback."""
    subject = _get_arc_state(parent_id, SUBJECT_KEY)
    if isinstance(subject, dict) and subject.get("kind") in VALID_KINDS:
        return subject
    # Legacy: a scalar reflected_arc_id is an ``arcs`` subject of one.
    rid = _get_arc_state(parent_id, "reflected_arc_id")
    if rid is not None:
        try:
            return {"kind": KIND_ARCS, "refs": [int(rid)]}
        except (TypeError, ValueError):
            return None
    return None


def subject_arc_ids(subject: dict | None) -> list[int]:
    """Arc ids this subject covers (empty for non-arc subjects like theme)."""
    if not subject:
        return []
    if subject.get("kind") in (KIND_ARCS, KIND_PERIOD):
        out = []
        for r in subject.get("refs", []) or []:
            try:
                out.append(int(r))
            except (TypeError, ValueError):
                continue
        return out
    return []


def subject_kb_path(subject: dict | None) -> str:
    """KB path the reflection writes to, derived from the subject kind."""
    if not subject:
        return "reflections/unkeyed"
    kind = subject.get("kind")
    if kind == KIND_ARCS:
        ids = subject_arc_ids(subject)
        return f"reflections/by-arc/{ids[0]}" if ids else "reflections/by-arc/unknown"
    if kind == KIND_PERIOD:
        window = subject.get("window") or {}
        date = window.get("date") or (window.get("to") or "")[:10] or "unknown"
        return f"reflections/by-day/{date}"
    if kind == KIND_THEME:
        slug = subject.get("theme") or subject.get("slug") or "general"
        return f"reflections/by-theme/{slug}"
    return "reflections/unkeyed"


def subject_title(subject: dict | None) -> str:
    if not subject:
        return "Reflection"
    kind = subject.get("kind")
    if kind == KIND_ARCS:
        ids = subject_arc_ids(subject)
        return f"Reflection on arc #{ids[0]}" if ids else "Reflection"
    if kind == KIND_PERIOD:
        window = subject.get("window") or {}
        date = window.get("date") or (window.get("to") or "")[:10] or "period"
        n = len(subject_arc_ids(subject))
        return f"Daily Reflection — {date} ({n} arc(s))"
    if kind == KIND_THEME:
        return f"Theme Reflection — {subject.get('theme') or 'general'}"
    return "Reflection"


def subject_period(subject: dict | None) -> tuple[str, str]:
    """(period_start, period_end) for frontmatter/provenance."""
    if subject and subject.get("kind") == KIND_PERIOD:
        window = subject.get("window") or {}
        start = window.get("from", "") or ""
        end = window.get("to", "") or start
        return start, end
    return "", ""
