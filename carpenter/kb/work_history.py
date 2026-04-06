"""Work history summaries — completed root arcs → AI summary → KB entry.

When a root arc (no parent) completes, generates an AI summary of
the work performed and writes it as a KB entry under work/.

No pruning — deferred to the reflections phase.
"""

import logging
import re
import sqlite3

from .. import config
from ..db import get_db, db_connection

logger = logging.getLogger(__name__)


def should_summarize(arc_id: int) -> bool:
    """Check if this arc should get a work history summary.

    Returns False for: sentinel (id=0), child arcs, arcs with no
    children, arcs whose name starts with '_'.
    """
    if arc_id == 0:
        return False

    kb_config = config.CONFIG.get("kb", {})
    if not kb_config.get("work_history_enabled", True):
        return False

    with db_connection() as db:
        row = db.execute(
            "SELECT parent_id, name FROM arcs WHERE id = ?", (arc_id,)
        ).fetchone()
        if row is None:
            return False
        if row["parent_id"] is not None:
            return False
        if row["name"].startswith("_"):
            return False

        # Must have children (otherwise not a real workflow)
        child = db.execute(
            "SELECT 1 FROM arcs WHERE parent_id = ? LIMIT 1", (arc_id,)
        ).fetchone()
        return child is not None


def generate_work_summary(arc_id: int) -> str | None:
    """Generate a markdown summary of completed arc work using the cheapest model.

    Reads the arc tree structure and calls AI to produce a brief summary.
    Returns markdown string, or None on failure.
    """
    with db_connection() as db:
        # Read root arc
        root = db.execute(
            "SELECT id, name, goal, status FROM arcs WHERE id = ?", (arc_id,)
        ).fetchone()
        if root is None:
            return None

        # Read children
        children = db.execute(
            "SELECT id, name, goal, status, step_order FROM arcs "
            "WHERE parent_id = ? ORDER BY step_order",
            (arc_id,),
        ).fetchall()

    # Build context for summarization
    parts = [f"Root arc: {root['name']}"]
    if root["goal"]:
        parts.append(f"Goal: {root['goal']}")
    parts.append(f"Status: {root['status']}")

    if children:
        parts.append("\nChild arcs:")
        for child in children:
            line = f"  - {child['name']} ({child['status']})"
            if child["goal"]:
                line += f": {child['goal']}"
            parts.append(line)

    arc_text = "\n".join(parts)

    prompt = (
        "Summarize this completed workflow in 2-3 sentences. "
        "Focus on what was accomplished, not the technical structure.\n\n"
        f"{arc_text}"
    )

    try:
        from ..agent import model_resolver
        model_str = model_resolver.get_model_for_role("summary")
        client_mod = model_resolver.create_client_for_model(model_str)
        _, bare_model = model_resolver.parse_model_string(model_str)

        resp = client_mod.call(
            "You generate brief work history summaries.",
            [{"role": "user", "content": prompt}],
            model=bare_model,
            max_tokens=300,
            temperature=0.3,
        )
        summary = client_mod.extract_text(resp).strip()
        return summary if summary else None
    except (sqlite3.Error, KeyError, ValueError) as _exc:
        logger.exception("Failed to generate work summary for arc %d", arc_id)
        return None


def _sanitize_name(name: str) -> str:
    """Convert an arc name to a filesystem-safe KB path segment."""
    # Lowercase, replace spaces/punctuation with hyphens, strip extras
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:50] if s else "unnamed"


def create_work_entry(arc_id: int, store) -> str | None:
    """Generate work summary and write KB entry.

    Writes to work/{arc_id}-{sanitized_name} with entry_type="work_history".

    Args:
        arc_id: The completed root arc ID.
        store: KBStore instance.

    Returns:
        KB path if created, None otherwise.
    """
    summary = generate_work_summary(arc_id)
    if not summary:
        return None

    # Get arc name for the path
    with db_connection() as db:
        row = db.execute(
            "SELECT name FROM arcs WHERE id = ?", (arc_id,)
        ).fetchone()

    arc_name = row["name"] if row else "unknown"
    safe_name = _sanitize_name(arc_name)
    kb_path = f"work/{arc_id}-{safe_name}"

    content = f"# {arc_name}\n\n{summary}\n"
    description = summary.split(".")[0].strip() + "." if "." in summary else summary[:100]

    store.write_entry(
        path=kb_path,
        content=content,
        description=description,
        entry_type="work_history",
        validate_links=False,
    )

    logger.info("Created work history KB entry: %s", kb_path)
    return kb_path
