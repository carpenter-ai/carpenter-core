"""Reflection persistence helpers.

KB is the sole persistence sink for reflections. The legacy
``reflections`` SQL table was retired in Phase D.

- :func:`save_reflection` builds a reflection-specific KB entry (path,
  frontmatter, body) via :func:`.kb_entry.build_reflection_entry` and
  enqueues the platform-generic ``kb.write_entry`` work item for the
  coordinator to persist asynchronously.
- :func:`get_reflections` reads recent reflections back from KB. Handles
  both the current per-arc layout (``reflections/by-arc/{arc_id}``) and
  the legacy cadence layout (``reflections/{daily,weekly,monthly}/{date}``).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from carpenter.db import db_connection

logger = logging.getLogger(__name__)


def save_reflection(
    reflected_arc_id: int,
    content: str,
    proposed_actions: str | None = None,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Enqueue an async KB write for a completed reflection.

    Args:
        reflected_arc_id: ID of the root arc the reflection analyses.
            Becomes the KB filename (``reflections/by-arc/{arc_id}``).
        content: Reflection markdown body.
        proposed_actions: Optional parsed action block (frontmatter).
        model: Model string (frontmatter / provenance).
        input_tokens: Reserved for future provenance.
        output_tokens: Reserved for future provenance.
    """
    with db_connection() as db:
        arc_row = db.execute(
            "SELECT created_at, updated_at FROM arcs WHERE id = ?",
            (reflected_arc_id,),
        ).fetchone()

    if arc_row:
        period_start = arc_row["created_at"] or ""
        period_end = arc_row["updated_at"] or period_start
    else:
        now = datetime.now(timezone.utc).isoformat()
        period_start = now
        period_end = now

    from .kb_entry import build_reflection_entry

    entry = build_reflection_entry(
        reflected_arc_id,
        content=content,
        proposed_actions=proposed_actions,
        model=model,
        period_start=period_start,
        period_end=period_end,
    )
    if entry is None:
        return

    try:
        from carpenter.core.engine import work_queue
        work_queue.enqueue(
            "kb.write_entry",
            entry,
            idempotency_key=f"refl-kb-by-arc-{reflected_arc_id}",
        )
    except (ImportError, sqlite3.Error):
        logger.exception(
            "Failed to enqueue kb.write_entry for reflection arc %d",
            reflected_arc_id,
        )


def get_reflections(limit: int = 5) -> list[dict]:
    """Return recent reflections from KB, newest first.

    Reads ``reflections/by-arc/*.md`` primarily. Falls back to legacy
    cadence-prefixed folders (``daily``/``weekly``/``monthly``) so
    pre-migration entries remain readable. Entries are parsed with YAML
    frontmatter; legacy entries without frontmatter fall back to regex
    extraction of ``**Period**: <start> to <end>``.

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
