"""Credential tool backend — handles credential management callbacks."""
import logging

from ..api.credentials import (
    create_credential_request,
    verify_credential,
    import_credential_file,
)

logger = logging.getLogger(__name__)


def handle_request(params: dict) -> dict:
    """Create a one-time secure link for credential input.

    Params: key, label (opt), description (opt).
    """
    key = params.get("key", "")
    if not key:
        return {"error": "key is required"}
    return create_credential_request(
        key=key,
        label=params.get("label", ""),
        description=params.get("description", ""),
    )


def handle_verify(params: dict) -> dict:
    """Verify a stored credential. Params: key."""
    key = params.get("key", "")
    if not key:
        return {"error": "key is required"}
    return verify_credential(key)


def handle_import_file(params: dict) -> dict:
    """Import a credential from a file. Params: path, key."""
    path = params.get("path", "")
    key = params.get("key", "")
    if not path:
        return {"error": "path is required"}
    if not key:
        return {"error": "key is required"}
    return import_credential_file(path=path, key=key)
