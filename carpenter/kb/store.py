"""Knowledge Base store — CRUD, sync, link management, access logging.

The KBStore manages entries on the filesystem at {base_dir}/kb/ and
keeps a SQLite index (kb_entries, kb_links, kb_embeddings) in sync.
"""

import hashlib
import logging
import os
from datetime import datetime, timezone

from .. import config
from ..db import get_db, db_connection, db_transaction
from . import parse
from .search import get_search_backend

logger = logging.getLogger(__name__)


class KBStore:
    """Knowledge Base filesystem + DB store."""

    def __init__(self, kb_dir: str | None = None):
        if kb_dir is None:
            kb_config = config.CONFIG.get("kb", {})
            kb_dir = kb_config.get("dir", "")
            if not kb_dir:
                base_dir = config.CONFIG.get("base_dir", "")
                kb_dir = os.path.join(base_dir, "config", "kb")
        self.kb_dir = kb_dir
        backend_name = config.CONFIG.get("kb", {}).get("search_backend", "fts5")
        self._search = get_search_backend(backend_name)

    def _fs_path(self, path: str) -> str:
        """Convert a KB path (e.g. 'scheduling/tools') to filesystem path."""
        # A path like 'scheduling' might be a folder (scheduling/_index.md)
        # or a leaf (scheduling.md). Check folder first.
        folder_path = os.path.join(self.kb_dir, path, "_index.md")
        if os.path.isfile(folder_path):
            return folder_path
        # Try as a leaf file
        leaf_path = os.path.join(self.kb_dir, path + ".md")
        if os.path.isfile(leaf_path):
            return leaf_path
        # Special case: root
        if path in ("", "_root"):
            root_path = os.path.join(self.kb_dir, "_root.md")
            if os.path.isfile(root_path):
                return root_path
        return ""

    def entry_exists(self, path: str) -> bool:
        """Check if a KB entry exists (filesystem check, no content read)."""
        return bool(self._fs_path(path))

    def _read_file(self, fs_path: str) -> str:
        """Read file content, return empty string on error."""
        try:
            with open(fs_path) as f:
                return f.read()
        except OSError:
            return ""

    def get_entry(self, path: str) -> dict | None:
        """Read entry from filesystem, return content + metadata.

        Returns dict with: path, title, description, content, byte_count,
        entry_type, trust_level, links, access_count.
        Returns None if entry doesn't exist.
        """
        if not path:
            path = "_root"

        fs_path = self._fs_path(path)
        if not fs_path:
            return None

        content = self._read_file(fs_path)
        if not content:
            return None

        title, description = parse.extract_title_and_description(content)
        links = parse.extract_links(content)

        # Get metadata from DB
        with db_connection() as db:
            row = db.execute(
                "SELECT * FROM kb_entries WHERE path = ?", (path,)
            ).fetchone()

        return {
            "path": path,
            "title": title or path.split("/")[-1],
            "description": description,
            "content": content,
            "byte_count": len(content.encode("utf-8")),
            "entry_type": dict(row)["entry_type"] if row else "knowledge",
            "trust_level": dict(row)["trust_level"] if row else "trusted",
            "links": links,
            "access_count": dict(row)["access_count"] if row else 0,
        }

    def list_children(self, path: str) -> list[dict]:
        """List immediate children of a folder path.

        Returns list of dicts: [{name, path, description, byte_count, is_folder}].
        """
        if not path:
            dir_path = self.kb_dir
        else:
            dir_path = os.path.join(self.kb_dir, path)

        if not os.path.isdir(dir_path):
            return []

        children = []
        for entry in sorted(os.listdir(dir_path)):
            if entry.startswith("."):
                continue
            full = os.path.join(dir_path, entry)

            if os.path.isdir(full):
                # Folder child — read its _index.md
                index_path = os.path.join(full, "_index.md")
                child_path = f"{path}/{entry}" if path else entry
                if os.path.isfile(index_path):
                    content = self._read_file(index_path)
                    title, desc = parse.extract_title_and_description(content)
                    children.append({
                        "name": entry,
                        "path": child_path,
                        "title": title or entry,
                        "description": desc,
                        "byte_count": len(content.encode("utf-8")),
                        "is_folder": True,
                    })
                else:
                    children.append({
                        "name": entry,
                        "path": child_path,
                        "title": entry,
                        "description": "",
                        "byte_count": 0,
                        "is_folder": True,
                    })
            elif entry.endswith(".md") and entry != "_index.md" and entry != "_root.md":
                # Leaf child
                name = entry[:-3]  # strip .md
                child_path = f"{path}/{name}" if path else name
                content = self._read_file(full)
                title, desc = parse.extract_title_and_description(content)
                children.append({
                    "name": name,
                    "path": child_path,
                    "title": title or name,
                    "description": desc,
                    "byte_count": len(content.encode("utf-8")),
                    "is_folder": False,
                })

        return children

    def write_entry(
        self,
        path: str,
        content: str,
        description: str,
        entry_type: str = "knowledge",
        trust_level: str = "trusted",
        auto_source: str | None = None,
        validate_links: bool = True,
        conversation_id: int | None = None,
    ) -> str:
        """Write entry to filesystem + update index.

        Returns success/error message.
        """
        # Path validation
        if ".." in path or path.startswith("/"):
            return "Error: invalid path (no .. or absolute paths)."

        # Link validation — reject broken [[wiki-links]]
        if validate_links:
            links = parse.extract_links(content)
            missing = [target for target, _ in links if not self.entry_exists(target)]
            if missing:
                return f"Error: broken links — targets not found: {', '.join(missing)}"

        # Determine filesystem path
        fs_path = os.path.join(self.kb_dir, path + ".md")
        os.makedirs(os.path.dirname(fs_path), exist_ok=True)

        try:
            with open(fs_path, "w") as f:
                f.write(content)
        except OSError as e:
            return f"Error writing entry: {e}"

        # Update DB index
        title, desc_extracted = parse.extract_title_and_description(content)
        if not description:
            description = desc_extracted
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        byte_count = len(content.encode("utf-8"))

        self._upsert_entry(
            path=path,
            title=title or path.split("/")[-1],
            description=description,
            content_hash=content_hash,
            entry_type=entry_type,
            trust_level=trust_level,
            byte_count=byte_count,
            auto_source=auto_source,
        )

        # Update links
        links = parse.extract_links(content)
        self._update_links(path, links)

        # Update search index
        self._search.update_entry(path, title or path, description, content)

        # Update linked_byte_counts for targets
        self._update_linked_byte_counts(path)

        # Trigger skill-KB review for agent-initiated writes to skills/ path
        if path.startswith("skills/") and auto_source is None:
            self._trigger_skill_kb_review(path, content_hash, conversation_id)

        return f"Wrote KB entry: {path}"

    def delete_entry(self, path: str) -> str:
        """Delete from filesystem + index.

        Returns success/error message.
        """
        # Check if auto-generated
        with db_connection() as db:
            row = db.execute(
                "SELECT auto_source FROM kb_entries WHERE path = ?", (path,)
            ).fetchone()
            if row and row["auto_source"]:
                return "Error: cannot delete auto-generated entries."

        # Delete filesystem
        fs_path = os.path.join(self.kb_dir, path + ".md")
        if os.path.isfile(fs_path):
            os.remove(fs_path)

        # Delete from DB
        with db_transaction() as db:
            db.execute("DELETE FROM kb_entries WHERE path = ?", (path,))
            db.execute("DELETE FROM kb_links WHERE source_path = ?", (path,))

        # Remove from search index
        self._search.remove_entry(path)

        return f"Deleted KB entry: {path}"

    def search(
        self, query: str, max_results: int = 5, path_prefix: str | None = None,
    ) -> list[dict]:
        """Search the KB using the configured backend.

        Args:
            path_prefix: If set, only return entries whose path starts with this prefix
                (e.g. "conversations/", "reflections/", "work/").

        Returns list of dicts: [{path, title, description, score}].
        """
        results = self._search.query(query, max_results, path_prefix=path_prefix)
        if not results:
            return []

        with db_connection() as db:
            output = []
            for result_path, score in results:
                row = db.execute(
                    "SELECT path, title, description FROM kb_entries WHERE path = ?",
                    (result_path,),
                ).fetchone()
                if row:
                    output.append({
                        "path": row["path"],
                        "title": row["title"],
                        "description": row["description"],
                        "score": score,
                    })
            return output

    def get_inbound_links(self, path: str) -> list[dict]:
        """List entries that link TO this path.

        Returns list of dicts: [{source_path, link_text, title, description}].
        """
        with db_connection() as db:
            rows = db.execute(
                "SELECT l.source_path, l.link_text, e.title, e.description "
                "FROM kb_links l "
                "LEFT JOIN kb_entries e ON l.source_path = e.path "
                "WHERE l.target_path = ? "
                "ORDER BY l.source_path",
                (path,),
            ).fetchall()
            return [
                {
                    "source_path": row["source_path"],
                    "link_text": row["link_text"],
                    "title": row["title"] or row["source_path"],
                    "description": row["description"] or "",
                }
                for row in rows
            ]

    def sync_from_filesystem(self) -> dict:
        """Scan filesystem, update index for any changes.

        Used ONLY on initial install. Returns change summary.
        """
        if not os.path.isdir(self.kb_dir):
            return {"added": 0, "updated": 0, "removed": 0}

        added = 0
        updated = 0

        # Walk filesystem
        for dirpath, _dirnames, filenames in os.walk(self.kb_dir):
            for filename in filenames:
                if not filename.endswith(".md"):
                    continue
                full_path = os.path.join(dirpath, filename)
                rel = os.path.relpath(full_path, self.kb_dir)

                # Convert filesystem path to KB path
                kb_path = self._rel_to_kb_path(rel)
                if kb_path is None:
                    continue

                content = self._read_file(full_path)
                if not content:
                    continue

                title, description = parse.extract_title_and_description(content)
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                byte_count = len(content.encode("utf-8"))

                # Check if already in DB
                with db_connection() as db:
                    existing = db.execute(
                        "SELECT content_hash FROM kb_entries WHERE path = ?",
                        (kb_path,),
                    ).fetchone()

                if existing is None:
                    self._upsert_entry(
                        path=kb_path,
                        title=title or kb_path.split("/")[-1],
                        description=description,
                        content_hash=content_hash,
                        entry_type="knowledge",
                        trust_level="trusted",
                        byte_count=byte_count,
                    )
                    added += 1
                elif existing["content_hash"] != content_hash:
                    self._upsert_entry(
                        path=kb_path,
                        title=title or kb_path.split("/")[-1],
                        description=description,
                        content_hash=content_hash,
                        entry_type="knowledge",
                        trust_level="trusted",
                        byte_count=byte_count,
                    )
                    updated += 1

                # Update links
                links = parse.extract_links(content)
                self._update_links(kb_path, links)

                # Update search index only for new or changed entries
                if existing is None or existing["content_hash"] != content_hash:
                    self._search.update_entry(
                        kb_path, title or kb_path, description, content,
                    )

        return {"added": added, "updated": updated, "removed": 0}

    def log_access(
        self, path: str, arc_id: int | None = None,
        conversation_id: int | None = None,
    ) -> None:
        """Record access in kb_access_log. Update last_accessed + access_count."""
        with db_transaction() as db:
            now = datetime.now(timezone.utc).isoformat()
            db.execute(
                "INSERT INTO kb_access_log (path, arc_id, conversation_id, accessed_at) "
                "VALUES (?, ?, ?, ?)",
                (path, arc_id, conversation_id, now),
            )
            db.execute(
                "UPDATE kb_entries SET last_accessed = ?, access_count = access_count + 1 "
                "WHERE path = ?",
                (now, path),
            )

    def queue_change(self, file_path: str, change_type: str) -> None:
        """Add to kb_change_queue. Idempotent (UNIQUE constraint)."""
        with db_transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO kb_change_queue (file_path, change_type) "
                "VALUES (?, ?)",
                (file_path, change_type),
            )

    def _trigger_skill_kb_review(
        self, path: str, content_hash: str, conversation_id: int | None = None,
    ) -> None:
        """Trigger a skill-KB review arc for a modified skill entry.

        Only fires if ``skill_kb_review.enabled`` is True in config.
        Failures are logged but never propagated to the caller.
        """
        try:
            from ..core.workflows.skill_kb_review_handler import trigger_review
            result = trigger_review(
                path=path, content_hash=content_hash,
                conversation_id=conversation_id,
            )
            logger.info(
                "Skill-KB review trigger for %s: arc_id=%s", path, result,
            )
        except (OSError, ValueError, KeyError) as _exc:
            logger.exception(
                "Failed to trigger skill-KB review for %s", path,
            )

    # ── Internal helpers ──────────────────────────────────────────────

    def _rel_to_kb_path(self, rel: str) -> str | None:
        """Convert relative filesystem path to KB path.

        Examples:
            '_root.md' -> '_root'
            'scheduling/_index.md' -> 'scheduling'
            'scheduling/tools.md' -> 'scheduling/tools'
        """
        # Normalize separators
        rel = rel.replace(os.sep, "/")

        if rel == "_root.md":
            return "_root"

        if rel.endswith("/_index.md"):
            return rel[:-len("/_index.md")]

        if rel.endswith(".md"):
            return rel[:-3]

        return None

    def _upsert_entry(
        self, *, path: str, title: str, description: str,
        content_hash: str, entry_type: str, trust_level: str,
        byte_count: int, auto_source: str | None = None,
    ) -> None:
        """Insert or update a kb_entries row."""
        now = datetime.now(timezone.utc).isoformat()
        with db_transaction() as db:
            db.execute(
                "INSERT INTO kb_entries "
                "(path, title, description, content_hash, trust_level, entry_type, "
                " auto_source, byte_count, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET "
                "title=excluded.title, description=excluded.description, "
                "content_hash=excluded.content_hash, trust_level=excluded.trust_level, "
                "entry_type=excluded.entry_type, auto_source=excluded.auto_source, "
                "byte_count=excluded.byte_count, updated_at=excluded.updated_at",
                (path, title, description, content_hash, trust_level, entry_type,
                 auto_source, byte_count, now, now),
            )

    def _update_links(self, source_path: str, links: list[tuple[str, str | None]]) -> None:
        """Replace all outbound links for a source path."""
        with db_transaction() as db:
            db.execute(
                "DELETE FROM kb_links WHERE source_path = ?", (source_path,)
            )
            for target, text in links:
                db.execute(
                    "INSERT OR IGNORE INTO kb_links (source_path, target_path, link_text) "
                    "VALUES (?, ?, ?)",
                    (source_path, target, text),
                )

    def _update_linked_byte_counts(self, path: str) -> None:
        """Recompute linked_byte_count for entries that link to/from this path."""
        with db_transaction() as db:
            # Get all paths that this entry links to
            link_rows = db.execute(
                "SELECT target_path FROM kb_links WHERE source_path = ?", (path,)
            ).fetchall()

            # Sum byte counts of direct link targets
            total = 0
            for link_row in link_rows:
                target = db.execute(
                    "SELECT byte_count FROM kb_entries WHERE path = ?",
                    (link_row["target_path"],),
                ).fetchone()
                if target:
                    total += target["byte_count"]

            db.execute(
                "UPDATE kb_entries SET linked_byte_count = ? WHERE path = ?",
                (total, path),
            )
