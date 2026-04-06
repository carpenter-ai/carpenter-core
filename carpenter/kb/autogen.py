"""Auto-generation pipeline for KB entries.

Scans tool modules, config keys, and workflow templates to produce
reference KB entries that stay in sync with the actual source code.

Also provides file-change processing: mtime polling + change queue
for detecting and regenerating stale entries at runtime.
"""

import ast
import hashlib
import logging
import os
import time
from collections import defaultdict
from pathlib import Path

from .. import config
from ..db import get_db, db_connection, db_transaction

logger = logging.getLogger(__name__)

# Repository root (two levels up from this file)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Built-in theme map — maps tool module names to KB paths.
# User overrides live in config.yaml under kb.theme_map.
_BUILTIN_THEME_MAP = {
    "scheduling": "scheduling/tools",
    "messaging": "messaging/tools",
    "arc": "arcs/tools",
    "files": "files/tools",
    "git": "git/tools",
    "web": "web/tools",
    "lm": "ai/tools",
    "state": "arcs/state-tools",
    "config": "self-modification/config-tools",
    "webhook": "git/webhooks",
    "kb": "self-modification/kb-editing",
    "review": "security/review-tools",
    "plugin": "chat/utilities",
    "platform_time": "chat/utilities",
    "system_info": "chat/utilities",
    "conversation": "chat/utilities",
    "credentials": "credentials/tools",
    "platform": "chat/utilities",
}


def get_theme_map() -> dict[str, str]:
    """Return the effective theme map (built-in defaults merged with config overrides).

    Config overrides come from ``kb.theme_map`` in config.yaml.  Any keys
    present in the config override the built-in defaults; keys not overridden
    keep their built-in value.

    Also loads the legacy ``theme_map.yaml`` sidecar file (if present) as a
    middle layer: built-in < theme_map.yaml < config.yaml.
    """
    merged = dict(_BUILTIN_THEME_MAP)

    # Legacy layer: theme_map.yaml next to this file
    yaml_path = Path(__file__).parent / "theme_map.yaml"
    try:
        import yaml
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
            if isinstance(data, dict):
                merged.update(data)
    except (ImportError, OSError, ValueError) as _exc:
        pass

    # Config layer (highest precedence)
    kb_config = config.CONFIG.get("kb", {})
    overrides = kb_config.get("theme_map", {})
    if isinstance(overrides, dict):
        merged.update(overrides)

    return merged


# Backwards-compatible alias
_load_theme_map = get_theme_map


def _hash_file(path: str) -> str:
    """SHA-256 hash of file contents."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _scan_tool_dir(base_dir: Path) -> list[dict]:
    """AST-parse a tool directory and return tool metadata.

    Returns list of dicts: {module, name, args, docline, decorators}.
    """
    if not base_dir.is_dir():
        return []

    tools = []
    for py_file in sorted(base_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = py_file.stem
        try:
            tree = ast.parse(py_file.read_text())
        except (OSError, SyntaxError, ValueError) as _exc:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            is_tool = any(
                (isinstance(d, ast.Call) and isinstance(d.func, ast.Name)
                 and d.func.id == "tool")
                or (isinstance(d, ast.Name) and d.id == "tool")
                for d in node.decorator_list
            )
            if not is_tool:
                continue
            doc = ast.get_docstring(node) or ""
            first_line = doc.split("\n")[0].strip()
            args = [a.arg for a in node.args.args]
            tools.append({
                "module": module_name,
                "name": node.name,
                "args": args,
                "docline": first_line,
                "source_file": str(py_file),
            })
    return tools


def scan_tools() -> dict[str, str]:
    """Scan act/ and read/ tool dirs, group by theme, return KB entries.

    Returns {kb_path: markdown_content}.
    """
    theme_map = _load_theme_map()
    act_dir = _REPO_ROOT / "carpenter_tools" / "act"
    read_dir = _REPO_ROOT / "carpenter_tools" / "read"

    act_tools = _scan_tool_dir(act_dir)
    read_tools = _scan_tool_dir(read_dir)

    # Group tools by KB path
    grouped: dict[str, list[str]] = defaultdict(list)
    source_files: dict[str, set] = defaultdict(set)

    for tool_info in act_tools:
        kb_path = theme_map.get(tool_info["module"], f"other/{tool_info['module']}")
        sig = f"{tool_info['module']}.{tool_info['name']}({', '.join(tool_info['args'])})"
        entry = f"- `{sig}`"
        if tool_info["docline"]:
            entry += f" — {tool_info['docline']}"
        grouped[kb_path].append(("act", entry))
        source_files[kb_path].add(tool_info["source_file"])

    for tool_info in read_tools:
        kb_path = theme_map.get(tool_info["module"], f"other/{tool_info['module']}")
        sig = f"{tool_info['module']}.{tool_info['name']}({', '.join(tool_info['args'])})"
        entry = f"- `{sig}`"
        if tool_info["docline"]:
            entry += f" — {tool_info['docline']}"
        grouped[kb_path].append(("read", entry))
        source_files[kb_path].add(tool_info["source_file"])

    # Build markdown entries
    result = {}
    for kb_path, items in grouped.items():
        title = kb_path.split("/")[-1].replace("-", " ").title()
        lines = [f"# {title}\n"]

        act_items = [entry for kind, entry in items if kind == "act"]
        read_items = [entry for kind, entry in items if kind == "read"]

        if act_items:
            lines.append("## Action Tools (`carpenter_tools.act`)\n")
            lines.extend(act_items)
            lines.append("")

        if read_items:
            lines.append("## Read Tools (`carpenter_tools.read`)\n")
            lines.extend(read_items)
            lines.append("")

        # Related links — infer from theme_map siblings
        parent = kb_path.rsplit("/", 1)[0] if "/" in kb_path else ""
        related = [
            f"[[{p}]]" for p in sorted(set(grouped.keys()))
            if p != kb_path and parent and p.startswith(parent + "/")
        ]
        if related:
            lines.append("## Related\n")
            lines.append(" · ".join(related))
            lines.append("")

        result[kb_path] = "\n".join(lines)

    return result


def scan_config() -> dict[str, str]:
    """Scan config.py DEFAULTS and generate a config reference KB entry.

    Returns {kb_path: markdown_content}.
    """
    from ..config import DEFAULTS

    # Group keys by theme prefix
    groups: dict[str, list[tuple[str, object]]] = defaultdict(list)
    for key, val in sorted(DEFAULTS.items()):
        if isinstance(val, dict):
            # Nested config sections
            for subkey, subval in sorted(val.items()):
                groups[key].append((f"{key}.{subkey}", subval))
        else:
            # Top-level keys — group by prefix before first _
            prefix = key.split("_")[0] if "_" in key else "general"
            groups[prefix].append((key, val))

    lines = ["# Configuration Reference\n"]
    lines.append("All config keys from `carpenter/config.py` DEFAULTS.\n")

    for group_name in sorted(groups.keys()):
        lines.append(f"## {group_name}\n")
        lines.append("| Key | Default | Type |")
        lines.append("|-----|---------|------|")
        for key, val in groups[group_name]:
            type_name = type(val).__name__
            default_str = repr(val)
            if len(default_str) > 50:
                default_str = default_str[:47] + "..."
            lines.append(f"| `{key}` | `{default_str}` | {type_name} |")
        lines.append("")

    return {"self-modification/config-reference": "\n".join(lines)}


def scan_templates() -> dict[str, str]:
    """Scan templates/*.yaml and generate KB entry.

    Returns {kb_path: markdown_content}.
    """
    templates_dir = _REPO_ROOT / "config_seed" / "templates"
    if not templates_dir.is_dir():
        return {}

    try:
        import yaml
    except ImportError:
        return {}

    lines = ["# Workflow Templates\n"]
    lines.append("YAML-defined process constraints from `config_seed/templates/`.\n")

    for yaml_file in sorted(templates_dir.glob("*.yaml")):
        try:
            with open(yaml_file) as f:
                data = yaml.safe_load(f)
        except (OSError, ValueError) as _exc:
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name", yaml_file.stem)
        desc = data.get("description", "")
        steps = data.get("steps", [])
        lines.append(f"## {name}\n")
        if desc:
            lines.append(f"{desc}\n")
        if steps:
            lines.append("**Steps:**")
            for step in steps:
                step_name = step.get("name", "?")
                step_desc = step.get("description", "")
                lines.append(f"- `{step_name}` — {step_desc}")
            lines.append("")

    lines.append("## Related\n")
    lines.append("[[arcs/planning]] · [[git/webhooks]]")
    lines.append("")

    return {"arcs/templates": "\n".join(lines)}


def _get_source_hashes(source_paths: list[str]) -> dict[str, str]:
    """Read stored hashes from kb_source_hashes table."""
    if not source_paths:
        return {}
    with db_connection() as db:
        placeholders = ",".join("?" * len(source_paths))
        rows = db.execute(
            f"SELECT source_path, content_hash FROM kb_source_hashes "
            f"WHERE source_path IN ({placeholders})",
            source_paths,
        ).fetchall()
        return {row["source_path"]: row["content_hash"] for row in rows}


def _update_source_hash(source_path: str, content_hash: str) -> None:
    """Write/update a hash in kb_source_hashes."""
    with db_transaction() as db:
        db.execute(
            "INSERT INTO kb_source_hashes (source_path, content_hash, last_checked) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(source_path) DO UPDATE SET "
            "content_hash=excluded.content_hash, last_checked=excluded.last_checked",
            (source_path, content_hash),
        )


def _collect_source_files() -> dict[str, list[str]]:
    """Map KB paths to source files that contribute to them.

    Returns {kb_path: [source_file_paths]}.
    """
    theme_map = _load_theme_map()
    result: dict[str, list[str]] = defaultdict(list)

    act_dir = _REPO_ROOT / "carpenter_tools" / "act"
    read_dir = _REPO_ROOT / "carpenter_tools" / "read"

    for tool_dir in [act_dir, read_dir]:
        if not tool_dir.is_dir():
            continue
        for py_file in sorted(tool_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            module_name = py_file.stem
            kb_path = theme_map.get(module_name, f"other/{module_name}")
            result[kb_path].append(str(py_file))

    # Config source
    config_path = str(_REPO_ROOT / "carpenter" / "config.py")
    result["self-modification/config-reference"].append(config_path)

    # Template sources
    templates_dir = _REPO_ROOT / "config_seed" / "templates"
    if templates_dir.is_dir():
        for yaml_file in sorted(templates_dir.glob("*.yaml")):
            result["arcs/templates"].append(str(yaml_file))

    return dict(result)


def run_autogen(store) -> dict:
    """Run the full auto-generation pipeline.

    For each generated entry, compares source file hashes against
    kb_source_hashes table. Skips entries whose sources are unchanged.

    Args:
        store: KBStore instance.

    Returns:
        {generated: int, skipped: int}
    """
    generated = 0
    skipped = 0

    # Collect all entries from scanners
    all_entries: dict[str, str] = {}
    all_entries.update(scan_tools())
    all_entries.update(scan_config())
    all_entries.update(scan_templates())
    # Skills are now in config_seed/kb/ (no longer auto-generated from skills_dir)

    # Map KB paths to source files
    source_map = _collect_source_files()

    for kb_path, content in all_entries.items():
        # Hash the source files that produce this entry
        sources = source_map.get(kb_path, [])
        combined_hash = hashlib.sha256()
        for src in sorted(sources):
            combined_hash.update(_hash_file(src).encode())
        # Also hash the content itself (handles theme_map changes)
        combined_hash.update(content.encode())
        new_hash = combined_hash.hexdigest()

        # Check against stored hash
        stored = _get_source_hashes([kb_path])
        if stored.get(kb_path) == new_hash:
            skipped += 1
            continue

        # Write the entry
        description = content.split("\n")[0].lstrip("# ").strip()
        store.write_entry(
            path=kb_path,
            content=content,
            description=description,
            entry_type="reference",
            validate_links=False,
        )
        # Mark as auto-generated in DB
        with db_transaction() as db:
            db.execute(
                "UPDATE kb_entries SET auto_source = ? WHERE path = ?",
                ("autogen", kb_path),
            )

        _update_source_hash(kb_path, new_hash)
        generated += 1

    return {"generated": generated, "skipped": skipped}


# ── Feature 2: File Change Processing ──────────────────────────────


def check_source_hashes(source_paths: list[str]) -> set[str]:
    """Hash each file and compare against stored hashes.

    Returns set of paths whose content has changed.
    """
    if not source_paths:
        return set()

    stored = _get_source_hashes(source_paths)
    changed = set()
    for path in source_paths:
        current = _hash_file(path)
        if current and current != stored.get(path, ""):
            changed.add(path)
    return changed


def process_change_queue(store) -> int:
    """Process pending KB change queue items.

    For each unprocessed item, determines the affected KB path and
    regenerates the entry via the appropriate scanner.

    Returns count of items processed.
    """
    with db_connection() as db:
        rows = db.execute(
            "SELECT id, file_path, change_type FROM kb_change_queue "
            "WHERE processed_at IS NULL ORDER BY detected_at ASC"
        ).fetchall()

    if not rows:
        return 0

    # Build reverse map: source_file -> kb_path
    source_map = _collect_source_files()
    reverse_map: dict[str, str] = {}
    for kb_path, sources in source_map.items():
        for src in sources:
            reverse_map[src] = kb_path

    processed = 0
    for row in rows:
        file_path = row["file_path"]
        kb_path = reverse_map.get(file_path)
        if kb_path:
            # Regenerate just this entry
            all_entries: dict[str, str] = {}
            all_entries.update(scan_tools())
            all_entries.update(scan_config())
            all_entries.update(scan_templates())
            content = all_entries.get(kb_path)
            if content:
                description = content.split("\n")[0].lstrip("# ").strip()
                store.write_entry(
                    path=kb_path,
                    content=content,
                    description=description,
                    entry_type="reference",
                    validate_links=False,
                )
                # Mark as auto-generated
                db2 = get_db()
                try:
                    db2.execute(
                        "UPDATE kb_entries SET auto_source = ? WHERE path = ?",
                        ("autogen", kb_path),
                    )
                    db2.commit()
                finally:
                    db2.close()

                # Update source hash
                sources = source_map.get(kb_path, [])
                combined = hashlib.sha256()
                for src in sorted(sources):
                    combined.update(_hash_file(src).encode())
                combined.update(content.encode())
                _update_source_hash(kb_path, combined.hexdigest())

        # Mark as processed
        db3 = get_db()
        try:
            db3.execute(
                "UPDATE kb_change_queue SET processed_at = datetime('now') "
                "WHERE id = ?",
                (row["id"],),
            )
            # Prune old entries (> 24h)
            db3.execute(
                "DELETE FROM kb_change_queue "
                "WHERE processed_at IS NOT NULL "
                "AND detected_at < datetime('now', '-1 day')"
            )
            db3.commit()
        finally:
            db3.close()
        processed += 1

    return processed


# In-memory mtime cache for heartbeat polling
_mtime_cache: dict[str, float] = {}


def _mtime_poll_and_process() -> None:
    """Heartbeat hook: poll source file mtimes, queue and process changes.

    Keeps an in-memory {source_path: last_mtime} dict. On each call:
    - Stats source files, queues changes for modified files
    - Processes any pending queue items
    """
    kb_config = config.CONFIG.get("kb", {})
    if not kb_config.get("enabled", True):
        return

    source_map = _collect_source_files()
    all_sources = set()
    for sources in source_map.values():
        all_sources.update(sources)

    try:
        from . import get_store
        store = get_store()
    except (ImportError, OSError, ValueError) as _exc:
        return

    # Check mtimes
    for source_path in all_sources:
        try:
            mtime = os.path.getmtime(source_path)
        except OSError:
            continue

        cached_mtime = _mtime_cache.get(source_path)
        if cached_mtime is not None and mtime > cached_mtime:
            # File changed — queue it
            store.queue_change(source_path, "modified")

        _mtime_cache[source_path] = mtime

    # Process any pending queue items
    try:
        count = process_change_queue(store)
        if count:
            logger.info("KB change processing: %d entries regenerated", count)
    except (ImportError, OSError, ValueError) as _exc:
        logger.exception("KB change processing failed")


def queue_source_changes(applied_files: list[str], source_dir: str) -> int:
    """Queue KB regeneration for changed source files.

    Called after coding-change applies files to detect which KB entries
    need regeneration. Leverages the existing change queue + heartbeat
    processing.

    Args:
        applied_files: List of relative file paths that were applied.
        source_dir: The source directory files were applied to.

    Returns:
        Number of changes queued.
    """
    source_map = _collect_source_files()

    # Build reverse map: normalised source_file -> kb_path
    reverse_map: dict[str, str] = {}
    for kb_path, sources in source_map.items():
        for src in sources:
            reverse_map[os.path.normpath(src)] = kb_path

    queued = 0
    try:
        from . import get_store
        store = get_store()
    except (ImportError, OSError, ValueError) as _exc:
        return 0

    for rel_path in applied_files:
        abs_path = os.path.normpath(os.path.join(source_dir, rel_path))
        if abs_path in reverse_map:
            store.queue_change(abs_path, "modified")
            queued += 1

    if queued:
        logger.info("KB percolation: queued %d changes from coding-change", queued)

    return queued


def register_change_hook() -> None:
    """Register the mtime poll + process heartbeat hook."""
    from ..core.engine import main_loop
    main_loop.register_heartbeat_hook(_mtime_poll_and_process)
    logger.info("KB file change processing hook registered")
