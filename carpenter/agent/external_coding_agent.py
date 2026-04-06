"""External coding agent runner.

Runs an external coding agent as a subprocess
in an isolated workspace directory.
"""

import logging
import os
import subprocess
import tempfile

from .. import config

logger = logging.getLogger(__name__)


def _substitute(template: str, variables: dict) -> str:
    """Substitute {key} placeholders in a template string.

    Only substitutes keys present in the variables dict; unknown placeholders
    are left as-is.
    """
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


def run(workspace: str, prompt: str, profile: dict) -> dict:
    """Run an external coding agent as a subprocess.

    Args:
        workspace: Absolute path to the workspace directory.
        prompt: The user's coding instruction.
        profile: Agent profile dict from config (type, command, timeout, env).

    Returns:
        dict with keys: stdout (str), stderr (str), exit_code (int)
    """
    timeout = profile.get("timeout", 600)

    # Write prompt to a temp file in the workspace
    prompt_file = os.path.join(workspace, ".tc_prompt.txt")
    with open(prompt_file, "w") as f:
        f.write(prompt)

    # Build substitution variables
    variables = {
        "workspace": workspace,
        "prompt_file": prompt_file,
        "claude_api_key": config.CONFIG.get("claude_api_key", ""),
    }

    # Substitute command template
    command = _substitute(profile["command"], variables)

    # Build environment
    env = dict(os.environ)
    profile_env = profile.get("env", {})
    for key, value in profile_env.items():
        env[key] = _substitute(str(value), variables)

    logger.info(
        "Running external coding agent: command=%s, workspace=%s, timeout=%d",
        command, workspace, timeout,
    )

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

        logger.info(
            "External agent finished: exit_code=%d, stdout_len=%d, stderr_len=%d",
            result.returncode, len(result.stdout), len(result.stderr),
        )

        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        logger.warning("External agent timed out after %ds", timeout)
        return {
            "stdout": "",
            "stderr": f"Timed out after {timeout}s",
            "exit_code": -1,
        }
    except FileNotFoundError as e:
        logger.error("External agent command not found: %s", e)
        return {
            "stdout": "",
            "stderr": f"Command not found: {e}",
            "exit_code": -1,
        }
    finally:
        # Clean up prompt file
        if os.path.exists(prompt_file):
            os.remove(prompt_file)
