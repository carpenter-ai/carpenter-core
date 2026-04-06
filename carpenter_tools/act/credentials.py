"""Credential management tools. Tier 1: callback to platform."""
from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_types={"key": "Label", "label": "Label"})
def request(key: str, label: str = "", description: str = "") -> dict:
    """Create a one-time secure link for credential input.

    The credential is stored in .env and never visible in chat.
    Returns a dict with request_id and URL the user should visit.

    Args:
        key: Env var name (e.g. 'FORGEJO_TOKEN', 'ANTHROPIC_API_KEY').
        label: Human-readable label for the form.
        description: Explanation of what the credential is used for.
    """
    return callback("credentials.request", {
        "key": key,
        "label": label,
        "description": description,
    })


@tool(local=True, readonly=False, side_effects=True,
      param_types={"key": "Label"})
def verify(key: str) -> dict:
    """Test a stored credential by making a verification call.

    For FORGEJO_TOKEN, calls the forge API to check validity.
    For other keys, checks non-empty. Never returns the credential value.
    """
    return callback("credentials.verify", {"key": key})


@tool(local=True, readonly=False, side_effects=True,
      param_types={"path": "WorkspacePath", "key": "Label"})
def import_file(path: str, key: str) -> dict:
    """Import a credential from a file, store in .env, delete the file.

    For non-TLS environments where the credential link isn't secure.

    Args:
        path: Absolute path to the file containing the credential.
        key: Env var name to store under (e.g. 'FORGEJO_TOKEN').
    """
    return callback("credentials.import_file", {"path": path, "key": key})
