"""Deterministic validators for typed security declarations.

Called during verification mode (CARPENTER_VERIFICATION_MODE=1) when a
SecurityType constructor is invoked. Each validator applies format-based
rules — no network calls, no allowlists.
"""

import json as _json_mod
import re
import urllib.parse
from pathlib import PurePosixPath


def validate_declaration(type_name: str, value: str) -> None:
    """Validate a declaration value by type.

    Raises ValueError if validation fails.
    """
    validator = _VALIDATORS.get(type_name)
    if validator is None:
        raise ValueError(f"Unknown declaration type: {type_name!r}")
    validator(value)


def _validate_label(value: str) -> None:
    if len(value) > 64:
        raise ValueError(
            f"Label too long ({len(value)} > 64): {value!r:.40}"
        )
    if not value:
        raise ValueError("Label cannot be empty")
    if not re.fullmatch(r'[a-zA-Z0-9_\-./]+', value):
        raise ValueError(
            f"Label contains invalid characters: {value!r:.40} "
            f"(allowed: a-zA-Z0-9_-./)"
        )


def _validate_email(value: str) -> None:
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', value):
        raise ValueError(f"Invalid email format: {value!r:.40}")


def _validate_url(value: str) -> None:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"URL scheme must be http or https, got {parsed.scheme!r}: {value!r:.60}"
        )
    if not parsed.netloc:
        raise ValueError(f"URL has no domain: {value!r:.60}")


def _validate_workspace_path(value: str) -> None:
    parts = PurePosixPath(value).parts
    if ".." in parts:
        raise ValueError(
            f"WorkspacePath cannot contain '..': {value!r:.40}"
        )
    if value.startswith("/"):
        raise ValueError(
            f"WorkspacePath must be relative (no leading /): {value!r:.40}"
        )


def _validate_sql(value: str) -> None:
    _ALLOWED_KEYWORDS = {"SELECT", "INSERT", "UPDATE", "DELETE"}
    stripped = value.strip()
    first_word = stripped.split()[0].upper() if stripped.split() else ""
    if first_word not in _ALLOWED_KEYWORDS:
        raise ValueError(
            f"SQL must start with {'/'.join(sorted(_ALLOWED_KEYWORDS))}, "
            f"got {first_word!r}: {value!r:.60}"
        )
    upper = stripped.upper()
    if "1=1" in upper or "'A'='A'" in upper.replace(" ", ""):
        raise ValueError(f"SQL contains tautology: {value!r:.60}")


def _validate_json(value: str) -> None:
    try:
        _json_mod.loads(value)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid JSON: {e}") from e


def _validate_unstructured_text(value: str) -> None:
    pass  # marker type — always valid


_VALIDATORS = {
    "label": _validate_label,
    "email": _validate_email,
    "url": _validate_url,
    "workspace_path": _validate_workspace_path,
    "sql": _validate_sql,
    "json": _validate_json,
    "unstructured_text": _validate_unstructured_text,
}
