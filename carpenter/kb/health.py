"""KB graph health metrics.

Computes structural health metrics for the knowledge base: orphan entries,
broken links, oversized entries, stale access, and BFS reachability.
"""

import logging
from collections import deque

from .. import config
from ..db import get_db, db_connection

logger = logging.getLogger(__name__)


def graph_metrics(store=None) -> dict:
    """Compute KB health metrics.

    Args:
        store: Optional KBStore instance. If None, uses get_store().

    Returns dict with:
        total_entries: int
        total_links: int
        avg_links_per_entry: float
        orphan_entries: list[str]      — 0 inbound + 0 outbound links
        unreachable_entries: list[str]  — not reachable from _root via BFS
        oversized_entries: list[str]    — > max_entry_bytes config
        stale_entries: list[str]        — not accessed in staleness_days
        broken_links: list[str]         — [[links]] to nonexistent paths
    """
    with db_connection() as db:
        # All entries
        entry_rows = db.execute(
            "SELECT path, byte_count, last_accessed FROM kb_entries"
        ).fetchall()

        # All links
        link_rows = db.execute(
            "SELECT source_path, target_path FROM kb_links"
        ).fetchall()

    all_paths = {row["path"] for row in entry_rows}
    total_entries = len(all_paths)
    total_links = len(link_rows)

    # Build adjacency sets
    outbound: dict[str, set[str]] = {p: set() for p in all_paths}
    inbound: dict[str, set[str]] = {p: set() for p in all_paths}
    broken_links: list[str] = []

    for link in link_rows:
        src = link["source_path"]
        tgt = link["target_path"]
        if src in outbound:
            outbound[src].add(tgt)
        if tgt in inbound:
            inbound[tgt].add(src)
        if tgt not in all_paths:
            broken_links.append(f"{src} -> {tgt}")

    # Orphans: no inbound and no outbound links (excluding _root)
    orphan_entries = sorted(
        p for p in all_paths
        if not outbound[p] and not inbound[p] and p != "_root"
    )

    # Avg links per entry
    avg_links = total_links / total_entries if total_entries > 0 else 0.0

    # BFS reachability from _root
    unreachable_entries = []
    if "_root" in all_paths:
        visited: set[str] = set()
        queue: deque[str] = deque(["_root"])
        visited.add("_root")
        while queue:
            current = queue.popleft()
            for neighbor in outbound.get(current, set()):
                if neighbor not in visited and neighbor in all_paths:
                    visited.add(neighbor)
                    queue.append(neighbor)
        unreachable_entries = sorted(all_paths - visited)

    # Oversized entries
    kb_config = config.CONFIG.get("kb", {})
    max_bytes = kb_config.get("max_entry_bytes", 6000)
    oversized_entries = sorted(
        row["path"] for row in entry_rows
        if (row["byte_count"] or 0) > max_bytes
    )

    # Stale entries
    staleness_days = kb_config.get("staleness_days", 30)
    stale_entries = []
    if staleness_days > 0:
        db2 = get_db()
        try:
            stale_rows = db2.execute(
                "SELECT path FROM kb_entries "
                "WHERE last_accessed IS NOT NULL "
                "AND last_accessed < datetime('now', ?)",
                (f"-{staleness_days} days",),
            ).fetchall()
            stale_entries = sorted(row["path"] for row in stale_rows)
        finally:
            db2.close()

    return {
        "total_entries": total_entries,
        "total_links": total_links,
        "avg_links_per_entry": round(avg_links, 2),
        "orphan_entries": orphan_entries,
        "unreachable_entries": unreachable_entries,
        "oversized_entries": oversized_entries,
        "stale_entries": stale_entries,
        "broken_links": broken_links,
    }
