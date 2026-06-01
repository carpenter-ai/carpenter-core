"""Credential management tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""
from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_types={"key": "Label", "label": "Label"})
def request(key: str, label: str = "", description: str = "") -> dict:
    """Create a one-time secure link for credential input.

    The credential is stored in .env and never visible in chat.
    Returns a dict with request_id and URL the user should visit.

    Args:
        key: Env var name (e.g. 'GIT_TOKEN', 'ANTHROPIC_API_KEY').
        label: Human-readable label for the form.
        description: Explanation of what the credential is used for.
    """
    ...


@tool(local=True, readonly=False, side_effects=True,
      param_types={"key": "Label"})
def verify(key: str) -> dict:
    """Test a stored credential by making a verification call.

    For GIT_TOKEN, delegates to the configured forge to check validity.
    For other keys, checks non-empty. Never returns the credential value.
    """
    ...


@tool(local=True, readonly=False, side_effects=True,
      param_types={"path": "WorkspacePath", "key": "Label"})
def import_file(path: str, key: str) -> dict:
    """Import a credential from a file, store in .env, delete the file.

    For non-TLS environments where the credential link isn't secure.

    Args:
        path: Absolute path to the file containing the credential.
        key: Env var name to store under (e.g. 'GIT_TOKEN').
    """
    ...
