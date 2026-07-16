"""Reflection persistence helpers.

**v2 pipeline (2026-07-12):** The daily "diary" write paths
(``reflections/by-day/{date}``, ``reflections/by-arc/{arc_id}``,
``reflections/{daily,weekly,monthly}/{date}``) have been **removed**.
KB is associative memory, not a diary — reflection knowledge only
lands in KB via reviewed ``kb-change`` action arcs spawned by
``dispatch-actions``. The generic platform ``kb.write_entry`` handler
now content-hash-dedupes identical writes.

- :func:`save_reflection` is retained for backward compatibility with a
  couple of tests but is now a **no-op that logs** — nothing is written
  to KB from this code path. Live reflection flow no longer calls it.
- :func:`get_reflections` still reads any legacy per-arc / per-day
  entries that pre-existed in KB from the v1 pipeline, so historical
  entries remain browsable. New entries are never written under these
  paths.
"""

from __future__ import annotations

import logging

from carpenter.db import db_connection

logger = logging.getLogger(__name__)


def save_reflection(
    subject_or_arc_id,
    content: str,
    proposed_actions: str | None = None,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """No-op in the v2 pipeline — retained for backward compatibility.

    v1 enqueued a KB write keyed on the subject
    (``reflections/by-day/*``, ``reflections/by-arc/*``, etc.). v2
    deliberately drops those diary writes. Live reflection flow does
    NOT call this function; a small number of legacy tests do, and this
    logs a warning to make the behaviour change loud.
    """
    logger.info(
        "save_reflection: v2 pipeline is a no-op (subject=%r, len=%d). "
        "KB writes now flow exclusively through dispatch-actions → "
        "kb-change action arcs.",
        subject_or_arc_id if not isinstance(subject_or_arc_id, dict)
        else subject_or_arc_id.get("kind"),
        len(content or ""),
    )


def get_reflections(limit: int = 5) -> list[dict]:
    """Return recent reflections from KB, newest first.

    v2 pipeline no longer writes new entries under
    ``reflections/by-*`` — this reader is kept so legacy entries from
    the v1 pipeline (per-arc, per-day, weekly, monthly) remain
    browsable. Returns an empty list once the legacy entries age out or
    are removed.

    Args:
        limit: Max results.

    Returns:
        List of reflection dicts with keys: content, period_start,
        period_end, model, proposed_actions, reflected_arc_id, source.
        ``source`` is the KB subfolder name (``by-arc``, ``daily``,
        ``weekly``, ``monthly``).
    """
    from carpenter.kb import get_store

    store = get_store()
    candidates: list[tuple[str, dict]] = []
    for source in ("by-arc", "daily", "weekly", "monthly"):
        for child in store.list_children(f"reflections/{source}"):
            if child.get("is_folder"):
                continue
            candidates.append((source, child))

    def _sort_key(sc):
        source, child = sc
        name = child["name"]
        if source == "by-arc":
            try:
                return (1, int(name))
            except ValueError:
                return (1, -1)
        return (0, name)

    candidates.sort(key=_sort_key, reverse=True)

    results = []
    for source, child in candidates[:limit]:
        entry = store.get_entry(child["path"])
        if not entry:
            continue
        meta, body = _parse_reflection_entry(entry["content"])
        results.append({
            "content": body,
            "period_start": meta.get("period_start", ""),
            "period_end": meta.get("period_end", ""),
            "model": meta.get("model", ""),
            "proposed_actions": meta.get("proposed_actions", ""),
            "reflected_arc_id": meta.get("reflected_arc_id"),
            "source": source,
        })
    return results


def _parse_reflection_entry(content: str) -> tuple[dict, str]:
    """Split a reflection KB entry into (frontmatter, body)."""
    import re
    import yaml

    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            fm_text = content[4:end]
            rest = content[end + 5:].lstrip("\n")
            try:
                meta = yaml.safe_load(fm_text) or {}
            except yaml.YAMLError:
                meta = {}
            if rest.startswith("# "):
                nl = rest.find("\n\n")
                if nl != -1:
                    rest = rest[nl + 2:]
            return meta, rest.rstrip() + "\n" if rest else ""

    # Legacy format: "**Period**: <start> to <end>" line in the body.
    meta: dict = {}
    m = re.search(
        r"^\*\*Period\*\*:\s*(\S+)\s+to\s+(\S+)\s*$", content, re.MULTILINE,
    )
    if m:
        meta["period_start"] = m.group(1)
        meta["period_end"] = m.group(2)
    return meta, content


# Preserved for imports elsewhere (e.g. tests importing db_connection).
__all__ = ["save_reflection", "get_reflections", "db_connection"]
