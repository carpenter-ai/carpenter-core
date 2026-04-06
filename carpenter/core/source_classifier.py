"""Source directory classification for context-aware model selection.

Maps source directories to policy categories to determine which model policies
should be used for coding agents and code reviewers.
"""

import os
from pathlib import Path

from .. import config


def classify_source_dir(source_dir: str) -> str:
    """Classify a source directory into a policy category.

    Categories:
        "config" - User config/tools/kb (~/carpenter/tools, ~/carpenter/config/kb, etc.)
        "platform" - Platform source code (~/repos/carpenter-core)
        "external" - External repositories (everything else)

    Args:
        source_dir: Absolute path to the source directory

    Returns:
        Policy category string: "config", "platform", or "external"
    """
    if not source_dir:
        return "external"

    # Normalize path for comparison
    source_path = Path(source_dir).resolve()

    # Check if it's under the base_dir (user config/tools/skills)
    base_dir = config.CONFIG.get("base_dir", "")
    if base_dir:
        base_path = Path(base_dir).resolve()
        try:
            source_path.relative_to(base_path)
            return "config"
        except ValueError:
            pass

    # Check if it's under a platform source directory
    for key in ("platform_server_dir", "platform_source_dir"):
        platform_dir = config.CONFIG.get(key, "")
        if platform_dir:
            platform_path = Path(platform_dir).resolve()
            try:
                source_path.relative_to(platform_path)
                return "platform"
            except ValueError:
                pass

    # Everything else is external
    return "external"


def get_policy_for_category(category: str, policy_type: str = "model_policy") -> str:
    """Get the policy name for a given category and policy type.

    Args:
        category: Policy category ("config", "platform", "external")
        policy_type: Either "model_policy" (for coder) or "reviewer_policy"

    Returns:
        Policy preset name (e.g., "fast-chat", "careful-coding")
    """
    # Read from config
    coding_policies = config.CONFIG.get("coding_policies", {})

    # Get category-specific mapping
    category_policy = coding_policies.get(category, {})
    policy_name = category_policy.get(policy_type, "")

    if policy_name:
        return policy_name

    # Fallback defaults based on category
    if policy_type == "model_policy":
        return {
            "config": "fast-chat",        # Haiku for config/tools
            "platform": "careful-coding",  # Sonnet for platform
            "external": "fast-chat",       # Haiku for external
        }.get(category, "fast-chat")
    else:  # reviewer_policy
        return {
            "config": "fast-chat",        # Haiku reviewer for config/tools
            "platform": "careful-coding",  # Sonnet reviewer for platform
            "external": "fast-chat",       # Haiku reviewer for external
        }.get(category, "fast-chat")
