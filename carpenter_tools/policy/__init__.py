"""Policy-typed literals for Carpenter verified flow analysis.

These classes wrap trusted reference values for comparing against
CONSTRAINED data. When CARPENTER_VERIFICATION_MODE=1, constructors
validate against the platform's security policies. In normal runtime
mode (after hash-and-trust), they are lightweight wrappers.

Usage in verified code::

    from carpenter_tools.policy import EmailPolicy, Domain, IntRange

    allowed_email = EmailPolicy("admin@example.com")
    allowed_domain = Domain("api.example.com")
    allowed_port = IntRange(80, 443)

    if extracted_email == allowed_email:  # C == T(policy) → T
        ...
"""

from .types import (
    EmailPolicy,
    Domain,
    Url,
    FilePath,
    Command,
    IntRange,
    Enum,
    Bool,
    Pattern,
    PolicyLiteral,
)

__all__ = [
    "EmailPolicy",
    "Domain",
    "Url",
    "FilePath",
    "Command",
    "IntRange",
    "Enum",
    "Bool",
    "Pattern",
    "PolicyLiteral",
]
