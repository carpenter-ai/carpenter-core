"""Tests for seed KB structure — entries parse, links resolve, no broken links."""

import os
import re
from pathlib import Path

from carpenter.kb.parse import (
    extract_links,
    extract_title_and_description,
)


def _strip_code(content: str) -> str:
    """Remove fenced code blocks and inline code so example [[links]] aren't counted."""
    content = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
    content = re.sub(r"`[^`]+`", "", content)
    return content


# Resolve the seed directory relative to the repo root.
_SEED_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config_seed", "kb",
)


def _all_entries():
    """Walk seed directory, return dict of kb_path -> content."""
    entries = {}
    for dirpath, _dirs, filenames in os.walk(_SEED_DIR):
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, _SEED_DIR).replace(os.sep, "/")

            # Derive KB path
            if rel == "_root.md":
                kb_path = "_root"
            elif rel.endswith("/_index.md"):
                kb_path = rel[: -len("/_index.md")]
            elif rel.endswith(".md"):
                kb_path = rel[:-3]
            else:
                continue

            with open(full) as f:
                entries[kb_path] = f.read()
    return entries


class TestSeedEntriesExist:
    def test_seed_dir_exists(self):
        assert os.path.isdir(_SEED_DIR), f"seed dir missing: {_SEED_DIR}"

    def test_has_root(self):
        assert os.path.isfile(os.path.join(_SEED_DIR, "_root.md"))

    def test_minimum_entry_count(self):
        md_files = list(Path(_SEED_DIR).rglob("*.md"))
        assert len(md_files) >= 25, f"expected >= 25 seed entries, got {len(md_files)}"


class TestSeedEntriesParse:
    def test_all_entries_have_title(self):
        entries = _all_entries()
        for kb_path, content in entries.items():
            title, _desc = extract_title_and_description(content)
            assert title, f"seed entry {kb_path} has no H1 title"

    def test_all_entries_have_description(self):
        entries = _all_entries()
        for kb_path, content in entries.items():
            _title, desc = extract_title_and_description(content)
            assert desc, f"seed entry {kb_path} has no description paragraph"


class TestSeedLinksResolve:
    def test_no_broken_links(self):
        entries = _all_entries()
        all_paths = set(entries.keys())

        # Also treat folder names as valid paths (for [[scheduling]] style links)
        for p in list(all_paths):
            parts = p.split("/")
            if len(parts) > 1:
                all_paths.add(parts[0])

        # Auto-generated entries exist at runtime but not in the seed dir.
        # Include their paths so links from seed files to auto-gen targets
        # are not flagged as broken.
        from carpenter.kb.autogen import scan_tools, scan_config, scan_templates
        for entries_map in [scan_tools(), scan_config(), scan_templates()]:
            all_paths.update(entries_map.keys())

        broken = []
        for kb_path, content in entries.items():
            links = extract_links(_strip_code(content))
            for target, _text in links:
                if target not in all_paths:
                    broken.append((kb_path, target))

        assert not broken, (
            f"Broken links in seed KB:\n"
            + "\n".join(f"  {src} -> {tgt}" for src, tgt in broken)
        )


class TestSeedEntrySize:
    def test_entries_within_soft_cap(self):
        max_bytes = 6000  # matches config default
        # Skill entries are exempt — they contain full instruction sets
        # that are intentionally larger than the soft cap for normal KB entries.
        skill_max_bytes = 50000
        entries = _all_entries()
        oversized = []
        for kb_path, content in entries.items():
            size = len(content.encode("utf-8"))
            limit = skill_max_bytes if kb_path.startswith("skills/") else max_bytes
            if size > limit:
                oversized.append((kb_path, size))
        assert not oversized, (
            f"Oversized seed entries:\n"
            + "\n".join(f"  {p}: {s} bytes" for p, s in oversized)
        )
