"""Typed security declarations for verified flow analysis.

Every string value in coder-generated code must be wrapped in one of
these type constructors. The type system IS the classifier — no
heuristic classification of strings needed.

SecurityType subclasses ``str`` so instances work seamlessly wherever
a string is expected (dict keys, function args, comparisons) without
modifying tool implementations.

Two modes of operation (same pattern as PolicyLiteral):
- Verification mode (CARPENTER_VERIFICATION_MODE=1): Constructor
  validates the value via deterministic rules. Raises ValueError
  on invalid input.
- Runtime mode (normal): Lightweight wrapper, no validation.
"""

import os
from typing import Any


def _is_verification_mode() -> bool:
    """Check if we're running in verification mode."""
    return os.environ.get("CARPENTER_VERIFICATION_MODE") == "1"


class SecurityType(str):
    """Base class for typed security declarations.

    Subclasses ``str`` so instances are usable everywhere a string is
    expected.  Each subclass sets ``_type_name`` for dispatch to the
    appropriate validator.
    """

    _type_name: str = ""

    def __new__(cls, value: Any) -> "SecurityType":
        return str.__new__(cls, str(value))

    def __init__(self, value: Any) -> None:
        # str.__init__ is a no-op for str subclasses; validation only
        if _is_verification_mode():
            self._validate()

    def _validate(self) -> None:
        """Validate using deterministic rules (verification mode only)."""
        from ._declarations_validate import validate_declaration
        validate_declaration(self._type_name, str(self))

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({str.__repr__(self)})"


class Label(SecurityType):
    """Keys, status values, identifiers.

    Validation: length <= 64, charset [a-zA-Z0-9_\\-./], no spaces.
    """
    _type_name = "label"


class Email(SecurityType):
    """Email addresses (format validation, not allowlist).

    Validation: RFC-compliant format (contains @, valid domain syntax).
    """
    _type_name = "email"


class URL(SecurityType):
    """Network endpoints.

    Validation: urlparse succeeds, scheme in {http, https}, valid domain.
    """
    _type_name = "url"


class WorkspacePath(SecurityType):
    """File system paths within workspace.

    Validation: no .. components, relative or within workspace root.
    """
    _type_name = "workspace_path"


class SQL(SecurityType):
    """Database queries.

    Validation: starts with allowed keyword (SELECT/INSERT/UPDATE/DELETE),
    no tautologies, parameterized.
    """
    _type_name = "sql"


class JSON(SecurityType):
    """Structured data interchange.

    Validation: json.loads() succeeds.
    """
    _type_name = "json"


class UnstructuredText(SecurityType):
    """Free-form text (marker type).

    Always passes validation. Routes to progressive text review.
    """
    _type_name = "unstructured_text"
