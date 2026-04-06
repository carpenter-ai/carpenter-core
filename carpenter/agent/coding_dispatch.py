"""Coding agent dispatcher.

Routes coding requests to the built-in or external agent based on the
configured profile type.
"""

import logging

from .. import config
from . import coding_agent, external_coding_agent

logger = logging.getLogger(__name__)


def get_profile(agent_name: str | None = None) -> tuple[str, dict]:
    """Look up a coding agent profile by name.

    Returns (name, profile_dict).

    Raises:
        ValueError: If the named profile is not found.
    """
    agents = config.CONFIG.get("coding_agents", {})
    if not agent_name:
        agent_name = config.CONFIG.get("default_coding_agent", "builtin")

    profile = agents.get(agent_name)
    if profile is None:
        raise ValueError(
            f"Coding agent profile '{agent_name}' not found. "
            f"Available: {list(agents.keys())}"
        )
    return agent_name, profile


def invoke_coding_agent(
    workspace: str,
    prompt: str,
    agent_name: str | None = None,
) -> dict:
    """Invoke the named (or default) coding agent.

    Args:
        workspace: Absolute path to the workspace directory.
        prompt: The user's coding instruction.
        agent_name: Name of the coding agent profile. None = use default.

    Returns:
        Result dict from the agent (stdout, exit_code, etc.)

    Raises:
        ValueError: If profile not found or has unknown type.
    """
    name, profile = get_profile(agent_name)
    agent_type = profile.get("type", "builtin")

    logger.info("Invoking coding agent: name=%s, type=%s", name, agent_type)

    if agent_type == "builtin":
        return coding_agent.run(workspace, prompt, profile)
    elif agent_type == "external":
        return external_coding_agent.run(workspace, prompt, profile)
    else:
        raise ValueError(f"Unknown coding agent type: {agent_type}")
