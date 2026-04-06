"""Prompt template loading and rendering via Jinja2.

Templates are loaded from external ``.md`` files.  The lookup order is:

1. **User overrides** -- ``{base_dir}/config/prompt-templates/{name}.md``
   (configurable via the ``prompt_templates_dir`` config key).
2. **Built-in defaults** -- ``config_seed/prompt-templates/{name}.md`` shipped
   with the repository.

This makes every prompt template user-overridable without touching the
package source.
"""

import os
import logging

from jinja2 import Template

from .. import config

logger = logging.getLogger(__name__)

# Built-in defaults live in config_seed/prompt-templates/ at repo root.
_BUILTIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "config_seed", "prompt-templates",
)


def _get_user_template_dir() -> str:
    """Return the user prompt-templates directory (may not exist yet)."""
    explicit = config.CONFIG.get("prompt_templates_dir", "")
    if explicit:
        return os.path.expanduser(explicit)
    base = config.CONFIG.get("base_dir", os.path.expanduser("~/carpenter"))
    return os.path.join(base, "config", "prompt-templates")


def get_template(name: str) -> Template:
    """Load a template by name.

    Checks the user override directory first, then the built-in package
    ``prompts/`` directory.

    Args:
        name: Template name (without extension).

    Returns:
        Jinja2 Template object.

    Raises:
        ValueError: If no template file is found in either location.
    """
    # 1. User override
    user_dir = _get_user_template_dir()
    user_file = os.path.join(user_dir, f"{name}.md")
    if os.path.isfile(user_file):
        with open(user_file) as f:
            logger.debug("Loaded user template override: %s", user_file)
            return Template(f.read())

    # 2. Built-in default
    builtin_file = os.path.join(_BUILTIN_DIR, f"{name}.md")
    if os.path.isfile(builtin_file):
        with open(builtin_file) as f:
            return Template(f.read())

    raise ValueError(f"Unknown template: {name}")


def render(template_name: str, **kwargs) -> str:
    """Render a template with the given variables.

    Args:
        template_name: Template name.
        **kwargs: Template variables.

    Returns:
        Rendered template string.
    """
    template = get_template(template_name)
    # Provide defaults for missing variables
    defaults = {
        "system_prompt": "",
        "active_arcs_summary": "No active arcs.",
        "conversation_history": "",
        "new_message": "",
        "prior_context_tail": "",
        "arc_context": "",
        "step_details": "",
        "code_under_review": "",
        "review_criteria": "",
        # arc_execute
        "arc_id": 0,
        "goal": "",
        "source_conv_id": 0,
        # revision_feedback / verification_feedback
        "original_prompt": "",
        "feedback": "",
        "rework_count": 0,
        "retry_count": 0,
        "max_retries": 0,
        "verification_feedback": "",
        # merge_resolve_conflicts
        "conflicting_files": "",
        "target_ref": "",
        "conflict_diff": "",
    }
    defaults.update(kwargs)
    return template.render(**defaults)
