"""Prompt template loader for Carpenter.

Loads system prompt sections from user-editable markdown files in
{base_dir}/prompts/. Each file is plain markdown with optional YAML
front-matter for ordering and compaction metadata.

The coordinator must call install_prompt_defaults() at startup to ensure
the prompts directory exists. There is no in-code fallback.

Also provides load_prompt_template() for loading individual named
templates (e.g. reflection prompts) from subdirectories.
"""

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Default prompt templates ship in config_seed/prompts/ at repo root
_DEFAULTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config_seed", "prompts",
)

# Coding agent prompt defaults ship in config_seed/coding-prompts/ at repo root
_CODING_DEFAULTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config_seed", "coding-prompts",
)


@dataclass
class PromptSection:
    """A single prompt section loaded from a template file."""
    name: str
    content: str
    compact: bool
    order: int


def _install_defaults(defaults_dir: str, target_dir: str, label: str) -> dict:
    """Copy a defaults directory to target_dir if it doesn't exist.

    Shared implementation for install_prompt_defaults() and
    install_coding_prompt_defaults().

    Returns:
        {"status": "installed"|"exists"|"no_defaults", "copied": int}
    """
    if os.path.isdir(target_dir):
        return {"status": "exists", "copied": 0}

    if not os.path.isdir(defaults_dir):
        logger.warning("%s defaults directory not found: %s", label, defaults_dir)
        return {"status": "no_defaults", "copied": 0}

    try:
        shutil.copytree(defaults_dir, target_dir)
        count = sum(1 for _ in Path(target_dir).glob("*.md"))
        logger.info("Installed %s defaults: %d files to %s", label, count, target_dir)
        return {"status": "installed", "copied": count}
    except OSError as e:
        logger.error("Failed to install %s defaults: %s", label, e)
        return {"status": "error", "error": str(e), "copied": 0}


def install_prompt_defaults(prompts_dir: str) -> dict:
    """Copy config_seed/prompts/ to prompts_dir if it doesn't exist.

    Same pattern as kb.install_seed(). Only copies on first install.

    Returns:
        {"status": "installed"|"exists"|"no_defaults", "copied": int}
    """
    return _install_defaults(_DEFAULTS_DIR, prompts_dir, "Prompt")


def install_coding_prompt_defaults(coding_prompts_dir: str) -> dict:
    """Copy config_seed/coding-prompts/ to coding_prompts_dir if it doesn't exist.

    Same pattern as install_prompt_defaults(). Only copies on first install.

    Returns:
        {"status": "installed"|"exists"|"no_defaults", "copied": int}
    """
    return _install_defaults(_CODING_DEFAULTS_DIR, coding_prompts_dir, "Coding prompt")


def _parse_front_matter(content: str) -> tuple[dict, str]:
    """Parse optional YAML front-matter delimited by --- lines.

    Returns:
        (metadata_dict, body_content)
    """
    if not content.startswith("---"):
        return {}, content

    lines = content.split("\n")
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return {}, content

    front_matter_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:]).lstrip("\n")

    try:
        import yaml
        metadata = yaml.safe_load(front_matter_text)
        if not isinstance(metadata, dict):
            metadata = {}
    except (ImportError, ValueError, TypeError) as _exc:
        # Fall back to simple key: value parsing
        metadata = {}
        for line in front_matter_text.split("\n"):
            line = line.strip()
            if ":" in line:
                key, _, val = line.partition(":")
                val = val.strip()
                if val.lower() in ("true", "yes"):
                    metadata[key.strip()] = True
                elif val.lower() in ("false", "no"):
                    metadata[key.strip()] = False
                else:
                    metadata[key.strip()] = val

    return metadata, body


def load_prompt_sections(prompts_dir: str) -> list[PromptSection]:
    """Load all .md files from prompts_dir, sorted by filename.

    Each file's name determines ordering: 00-identity.md -> order=0.
    Section name is derived from the filename after the numeric prefix.

    Returns:
        List of PromptSection objects sorted by order.
    """
    if not os.path.isdir(prompts_dir):
        return []

    sections = []
    for md_file in sorted(Path(prompts_dir).glob("*.md")):
        content = md_file.read_text()
        metadata, body = _parse_front_matter(content)

        # Derive order from filename prefix
        stem = md_file.stem  # e.g., "05-arc-planning"
        parts = stem.split("-", 1)
        try:
            order = int(parts[0])
        except (ValueError, IndexError):
            order = 99

        # Derive section name from filename
        name = parts[1] if len(parts) > 1 else parts[0]
        name = name.replace("-", "_")

        sections.append(PromptSection(
            name=name,
            content=body.strip(),
            compact=metadata.get("compact", False),
            order=order,
        ))

    sections.sort(key=lambda s: s.order)
    return sections


def render_prompt_sections(
    sections: list[PromptSection],
    context: dict | None = None,
) -> list[PromptSection]:
    """Render Jinja2 expressions in section content.

    No-op if no Jinja expressions are present. Safe to call without
    Jinja2 installed — returns sections unchanged.

    Args:
        sections: List of PromptSection objects.
        context: Template variables dict.

    Returns:
        New list of PromptSection objects with rendered content.
    """
    if context is None:
        context = {}

    try:
        from jinja2 import Environment
        env = Environment()
    except ImportError:
        return sections

    result = []
    for section in sections:
        if "{{" in section.content or "{%" in section.content:
            try:
                template = env.from_string(section.content)
                rendered = template.render(**context)
            except (ValueError, TypeError, KeyError) as _exc:
                rendered = section.content
        else:
            rendered = section.content

        result.append(PromptSection(
            name=section.name,
            content=rendered,
            compact=section.compact,
            order=section.order,
        ))

    return result


def _get_prompts_dir() -> str:
    """Resolve the prompts directory from config.

    Returns:
        The prompts directory path, or empty string if unavailable.
    """
    from . import config as cfg
    prompts_dir = cfg.CONFIG.get("prompts_dir", "")
    if not prompts_dir:
        base_dir = cfg.CONFIG.get("base_dir", "")
        if base_dir:
            prompts_dir = os.path.join(base_dir, "config", "prompts")
    return prompts_dir


def load_prompt_template(
    name: str,
    context: dict | None = None,
    *,
    subdirectory: str = "",
) -> str:
    """Load and render a single named prompt template.

    Looks for ``{prompts_dir}/{subdirectory}/{name}.md`` first (user override),
    then falls back to ``config_seed/prompts/{subdirectory}/{name}.md`` (repo default).

    The file body (after optional YAML front-matter) is rendered with Jinja2
    using the supplied *context* dict.

    Args:
        name: Template name without extension (e.g. ``"daily"``).
        context: Variables available in Jinja2 expressions.
        subdirectory: Optional subdirectory within the prompts dir
            (e.g. ``"reflections"``).

    Returns:
        Rendered template string.

    Raises:
        FileNotFoundError: If neither user override nor default template exists.
    """
    if context is None:
        context = {}

    filename = f"{name}.md"

    # Try user-customized prompts dir first
    prompts_dir = _get_prompts_dir()
    if prompts_dir:
        if subdirectory:
            user_path = os.path.join(prompts_dir, subdirectory, filename)
        else:
            user_path = os.path.join(prompts_dir, filename)
        if os.path.isfile(user_path):
            content = Path(user_path).read_text()
            _, body = _parse_front_matter(content)
            return _render_template_string(body.strip(), context)

    # Fall back to repo defaults
    if subdirectory:
        default_path = os.path.join(_DEFAULTS_DIR, subdirectory, filename)
    else:
        default_path = os.path.join(_DEFAULTS_DIR, filename)
    if os.path.isfile(default_path):
        content = Path(default_path).read_text()
        _, body = _parse_front_matter(content)
        return _render_template_string(body.strip(), context)

    raise FileNotFoundError(
        f"Prompt template not found: {subdirectory}/{filename} "
        f"(searched {prompts_dir!r} and {_DEFAULTS_DIR!r})"
    )


def _render_template_string(text: str, context: dict) -> str:
    """Render a string as a Jinja2 template with the given context.

    Falls back to the raw string if Jinja2 is not installed or if
    the template contains no expressions.
    """
    if "{{" not in text and "{%" not in text:
        return text

    try:
        from jinja2 import Environment
        env = Environment()
        template = env.from_string(text)
        return template.render(**context)
    except ImportError:
        return text
    except Exception:
        logger.warning("Failed to render prompt template, returning raw text")
        return text
def load_coding_prompt(coding_prompts_dir: str) -> str | None:
    """Load the coding agent system prompt from template files.

    Loads all .md files from the coding prompts directory, joins their
    content (after stripping YAML front-matter), and returns the combined
    text as a single string.

    Args:
        coding_prompts_dir: Path to the coding prompts directory.

    Returns:
        The combined prompt text, or None if the directory doesn't exist
        or contains no .md files.
    """
    sections = load_prompt_sections(coding_prompts_dir)
    if not sections:
        return None
    return "\n\n".join(s.content for s in sections if s.content)
