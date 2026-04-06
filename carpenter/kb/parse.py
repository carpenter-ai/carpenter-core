"""Markdown parsing utilities for the Knowledge Base.

Extracts [[wiki-links]], titles, descriptions, and auto-generated sections
from KB entry content.
"""

import re

# [[target]] or [[target|display text]]
_LINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]*?))?\]\]")

# <!-- auto:section_name source_file --> ... <!-- /auto -->
_AUTO_SECTION_RE = re.compile(
    r"<!--\s*auto:(\S+)\s+(\S+)\s*-->(.*?)<!--\s*/auto\s*-->",
    re.DOTALL,
)


def extract_links(content: str) -> list[tuple[str, str | None]]:
    """Extract [[target]] and [[target|text]] links.

    Returns:
        List of (target_path, display_text_or_None) tuples.
    """
    results = []
    for m in _LINK_RE.finditer(content):
        target = m.group(1).strip()
        text = m.group(2)
        if text is not None:
            text = text.strip()
        results.append((target, text))
    return results


def extract_auto_sections(content: str) -> list[dict]:
    """Find <!-- auto:name source --> ... <!-- /auto --> blocks.

    Returns:
        List of dicts: {name, source, content, start, end}.
    """
    results = []
    for m in _AUTO_SECTION_RE.finditer(content):
        results.append({
            "name": m.group(1),
            "source": m.group(2),
            "content": m.group(3),
            "start": m.start(),
            "end": m.end(),
        })
    return results


def replace_auto_sections(content: str, sections: dict[str, str]) -> str:
    """Replace auto section contents, preserving authored content.

    Args:
        content: Full entry markdown.
        sections: {section_name: new_content} mapping.

    Returns:
        Updated content with replaced auto sections.
    """
    def _replacer(m):
        name = m.group(1)
        source = m.group(2)
        if name in sections:
            return f"<!-- auto:{name} {source} -->{sections[name]}<!-- /auto -->"
        return m.group(0)

    return _AUTO_SECTION_RE.sub(_replacer, content)


def extract_title_and_description(content: str) -> tuple[str, str]:
    """Extract H1 as title, first non-heading paragraph as description.

    Returns:
        (title, description) tuple. Empty strings if not found.
    """
    title = ""
    description = ""

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# ") and not title:
            title = stripped[2:].strip()
            continue
        if stripped.startswith("#"):
            # Skip other headings
            continue
        if not description:
            description = stripped[:200]
            break

    return title, description
