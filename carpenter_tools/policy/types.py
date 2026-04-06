"""Policy-typed literal classes for verified flow analysis.

Each class represents a trusted reference value that can be compared
against CONSTRAINED data. The comparison C == T(policy-typed) yields
a TRUSTED result (the decomposition pattern).

Two modes of operation:
- Verification mode (CARPENTER_VERIFICATION_MODE=1): Constructor validates
  the value against security policies via platform callback. Raises
  PolicyValidationError on failure.
- Runtime mode (normal): Lightweight wrapper, no validation (code already
  verified and hash-trusted).
"""

import os
from typing import Any


def _is_verification_mode() -> bool:
    """Check if we're running in verification mode."""
    return os.environ.get("CARPENTER_VERIFICATION_MODE") == "1"


class PolicyLiteral:
    """Base class for all policy-typed literals.

    Subclasses set _policy_type to identify their category.
    """

    _policy_type: str = ""

    def __init__(self, value: Any):
        self._value = value
        if _is_verification_mode():
            self._validate()

    def _validate(self) -> None:
        """Validate against security policies via platform callback."""
        from ._validate import validate_policy_value
        validate_policy_value(self._policy_type, self._serialized_value())

    def _serialized_value(self) -> str:
        """Return the value as a string for policy validation."""
        return str(self._value)

    @property
    def value(self) -> Any:
        """Return the wrapped value."""
        return self._value

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, PolicyLiteral):
            return self._value == other._value
        return self._value == other

    def __hash__(self) -> int:
        return hash((self._policy_type, self._value))

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._value!r})"

    def __str__(self) -> str:
        return str(self._value)


class EmailPolicy(PolicyLiteral):
    """Policy-typed email address.

    Validates against the 'email' allowlist in security policies.
    Comparison is case-insensitive.
    """

    _policy_type = "email"

    def __init__(self, value: str):
        super().__init__(value.strip().lower())

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            return self._value == other.strip().lower()
        return super().__eq__(other)

    def __hash__(self) -> int:
        return super().__hash__()


class Domain(PolicyLiteral):
    """Policy-typed domain name.

    Validates against the 'domain' allowlist. Supports exact and suffix
    matching (e.g., Domain("example.com") matches "sub.example.com").
    """

    _policy_type = "domain"

    def __init__(self, value: str):
        super().__init__(value.strip().lower().rstrip("."))

    def matches(self, candidate: str) -> bool:
        """Check if a candidate domain matches this policy domain.

        Returns True for exact match or subdomain match.
        """
        normalized = candidate.strip().lower().rstrip(".")
        return normalized == self._value or normalized.endswith("." + self._value)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            return self.matches(other)
        if isinstance(other, Domain):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return super().__hash__()


class Url(PolicyLiteral):
    """Policy-typed URL prefix.

    Validates against the 'url' allowlist. Comparison uses prefix matching.
    """

    _policy_type = "url"

    def matches(self, candidate: str) -> bool:
        """Check if a candidate URL starts with this policy URL prefix."""
        return candidate.startswith(self._value)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            return self.matches(other)
        return super().__eq__(other)

    def __hash__(self) -> int:
        return super().__hash__()


class FilePath(PolicyLiteral):
    """Policy-typed file path prefix.

    Validates against the 'filepath' allowlist. Comparison uses prefix matching.
    """

    _policy_type = "filepath"

    def matches(self, candidate: str) -> bool:
        """Check if a candidate path starts with this policy path prefix."""
        return candidate.startswith(self._value)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            return self.matches(other)
        return super().__eq__(other)

    def __hash__(self) -> int:
        return super().__hash__()


class Command(PolicyLiteral):
    """Policy-typed command string (exact match)."""

    _policy_type = "command"

    def __hash__(self) -> int:
        return super().__hash__()


class IntRange(PolicyLiteral):
    """Policy-typed integer range.

    Constructor takes (lo, hi) defining an inclusive range [lo, hi].
    Comparison checks if the other value falls within the range.
    """

    _policy_type = "int_range"

    def __init__(self, lo: int, hi: int):
        self._lo = lo
        self._hi = hi
        super().__init__(f"{lo}:{hi}")

    def _serialized_value(self) -> str:
        return f"{self._lo}:{self._hi}"

    @property
    def lo(self) -> int:
        return self._lo

    @property
    def hi(self) -> int:
        return self._hi

    def contains(self, value: int) -> bool:
        """Check if an integer value falls within this range."""
        return self._lo <= value <= self._hi

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, int):
            return self.contains(other)
        if isinstance(other, IntRange):
            return self._lo == other._lo and self._hi == other._hi
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self._policy_type, self._lo, self._hi))

    def __repr__(self) -> str:
        return f"IntRange({self._lo}, {self._hi})"

    def __contains__(self, item: int) -> bool:
        return self.contains(item)


class Enum(PolicyLiteral):
    """Policy-typed enumerated value (exact string match)."""

    _policy_type = "enum"

    def __hash__(self) -> int:
        return super().__hash__()


class Bool(PolicyLiteral):
    """Policy-typed boolean value."""

    _policy_type = "bool"

    def __init__(self, value: bool):
        super().__init__(value)

    def _serialized_value(self) -> str:
        return str(self._value).lower()

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, bool):
            return self._value == other
        return super().__eq__(other)

    def __hash__(self) -> int:
        return super().__hash__()


class Pattern(PolicyLiteral):
    """Policy-typed regex pattern.

    The value is a regex pattern string. Comparison checks if the
    other value fully matches the pattern.
    """

    _policy_type = "pattern"

    def matches(self, candidate: str) -> bool:
        """Check if a candidate string fully matches this pattern."""
        import re
        try:
            return re.fullmatch(self._value, candidate) is not None
        except re.error:
            return False

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str) and not isinstance(other, Pattern):
            return self.matches(other)
        return super().__eq__(other)

    def __hash__(self) -> int:
        return super().__hash__()
