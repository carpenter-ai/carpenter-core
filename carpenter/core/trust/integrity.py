"""Information-flow integrity lattice for Carpenter.

Three-level integrity lattice:
    TRUSTED ⊑ CONSTRAINED ⊑ UNTRUSTED

- TRUSTED: Platform code and verified agents. Full access.
- CONSTRAINED: Data extracted by quarantined LLM (Q-LLM) with Pydantic schema.
  Cannot influence control flow without deterministic policy check.
  Enforced the same as UNTRUSTED for now (conservative default).
- UNTRUSTED: Raw external data. Must pass through review pipeline.
"""

from enum import Enum


class IntegrityLevel(str, Enum):
    TRUSTED = "trusted"
    CONSTRAINED = "constrained"
    UNTRUSTED = "untrusted"


# Lattice ordering: TRUSTED < CONSTRAINED < UNTRUSTED
_ORDER = {
    IntegrityLevel.TRUSTED: 0,
    IntegrityLevel.CONSTRAINED: 1,
    IntegrityLevel.UNTRUSTED: 2,
}


def join(a: "IntegrityLevel | str", b: "IntegrityLevel | str") -> IntegrityLevel:
    """Return least trusted (join / least upper bound) of two levels.

    join(TRUSTED, CONSTRAINED) → CONSTRAINED
    join(CONSTRAINED, UNTRUSTED) → UNTRUSTED
    join(TRUSTED, UNTRUSTED) → UNTRUSTED
    """
    a_lvl = IntegrityLevel(a)
    b_lvl = IntegrityLevel(b)
    return a_lvl if _ORDER[a_lvl] >= _ORDER[b_lvl] else b_lvl


def is_trusted(level: "IntegrityLevel | str") -> bool:
    """Return True only for TRUSTED level."""
    return IntegrityLevel(level) == IntegrityLevel.TRUSTED


def is_non_trusted(level: "IntegrityLevel | str") -> bool:
    """Return True for CONSTRAINED or UNTRUSTED (anything that needs review)."""
    return IntegrityLevel(level) != IntegrityLevel.TRUSTED


def validate_integrity_level(value: str) -> str:
    """Validate and return an integrity level string. Raises ValueError if invalid."""
    try:
        return IntegrityLevel(value).value
    except ValueError:
        valid = ", ".join(lvl.value for lvl in IntegrityLevel)
        raise ValueError(f"Invalid integrity_level '{value}'. Must be one of: {valid}")
