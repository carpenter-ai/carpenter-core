"""Reflection KB entry construction.

Feature-local helper that knows the reflection KB path shape and
frontmatter layout. Produces the formatted-content + metadata payload
consumed by the platform's generic ``kb.write_entry`` work-item
handler.

Two call shapes:

- **Per-arc (current)**: caller passes ``reflected_arc_id`` plus
  ``content`` and frontmatter fields directly. Writes to
  ``reflections/by-arc/{reflected_arc_id}``.
- **Legacy cadence**: caller passes ``cadence`` + ``period_*`` (with
  ``reflected_arc_id=None``) for legacy-shaped entries. Writes to
  ``reflections/{cadence}/{date}``. Retained for readback tests that
  seed the KB with legacy-shaped entries so ``get_reflections``' fallback
  logic remains exercised.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def build_reflection_entry(
    reflected_arc_id: int | None = None,
    *,
    subject: dict | None = None,
    content: str | None = None,
    proposed_actions: str | None = None,
    model: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    cadence: str | None = None,
) -> dict | None:
    """Build the payload for a reflection KB entry write.

    Three call shapes:

    - ``subject`` given — the generalised path: KB path/title/frontmatter
      derive from the typed subject (arcs / period / theme).
    - ``reflected_arc_id`` given — legacy per-arc shape
      (``reflections/by-arc/{id}``).
    - ``cadence`` given — legacy daily/weekly/monthly shape.

    Returns ``None`` when ``content`` is empty — the caller should skip.
    """
    if not content:
        return None

    import yaml

    frontmatter: dict = {
        "period_start": period_start or "",
        "period_end": period_end or "",
        "model": model or "unknown",
    }
    if proposed_actions:
        frontmatter["proposed_actions"] = proposed_actions

    if subject is not None:
        from ._subject import (
            subject_arc_ids, subject_kb_path, subject_period, subject_title,
        )
        kb_path = subject_kb_path(subject)
        title = subject_title(subject)
        s_start, s_end = subject_period(subject)
        if s_start:
            frontmatter["period_start"] = s_start
        if s_end:
            frontmatter["period_end"] = s_end
        frontmatter = {
            "subject_kind": subject.get("kind"),
            "subject_refs": subject_arc_ids(subject),
            **frontmatter,
        }
    elif reflected_arc_id is not None:
        kb_path = f"reflections/by-arc/{reflected_arc_id}"
        frontmatter = {
            "reflected_arc_id": reflected_arc_id,
            **frontmatter,
        }
        title = f"Reflection on arc #{reflected_arc_id}"
    else:
        # Legacy cadenced entries (daily/weekly/monthly shape).
        pe = period_end or "unknown"
        date_str = pe[:10] if len(pe) >= 10 else pe
        kb_path = f"reflections/{cadence}/{date_str}"
        frontmatter = {
            "cadence": cadence,
            **frontmatter,
        }
        title = f"{(cadence or '').title()} Reflection — {date_str}"

    parts = [
        "---",
        yaml.safe_dump(frontmatter, sort_keys=False).rstrip(),
        "---",
        "",
        f"# {title}",
        "",
        content,
        "",
    ]
    entry_content = "\n".join(parts)
    description = (
        content.split(".")[0].strip() + "."
        if "." in content
        else content[:100]
    )

    return {
        "kb_path": kb_path,
        "content": entry_content,
        "description": description,
        "entry_type": "reflection",
    }


def create_reflection_entry(
    store,
    reflected_arc_id: int | None = None,
    *,
    content: str | None = None,
    proposed_actions: str | None = None,
    model: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    cadence: str | None = None,
) -> str | None:
    """Synchronously write a reflection KB entry.

    Thin wrapper over :func:`build_reflection_entry` + ``store.write_entry``.
    Used by tests and any caller that needs a synchronous write. Live
    reflection flow enqueues the payload via the platform
    ``kb.write_entry`` work-item type — see
    :func:`reflection_storage.save_reflection`.
    """
    payload = build_reflection_entry(
        reflected_arc_id,
        content=content,
        proposed_actions=proposed_actions,
        model=model,
        period_start=period_start,
        period_end=period_end,
        cadence=cadence,
    )
    if payload is None:
        return None

    store.write_entry(
        path=payload["kb_path"],
        content=payload["content"],
        description=payload["description"],
        entry_type=payload["entry_type"],
        validate_links=False,
    )
    logger.info("Created reflection KB entry: %s", payload["kb_path"])
    return payload["kb_path"]
