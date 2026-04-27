"""Security policies for Carpenter.

Default-deny allowlists for nine policy types. Values must be explicitly
added to the allowlist before they are accepted. Empty allowlist = deny all.

Policy types:
- email: Email addresses (exact match, case-insensitive)
- domain: Domain names (exact or suffix match)
- url: URL prefixes
- filepath: File path prefixes
- command: Exact command strings
- int_range: Integer ranges (stored as "min:max")
- enum: Enumerated string values
- bool: Boolean values (stored as "true" or "false")
- pattern: Regex patterns (stored as pattern strings)
"""

import logging
import re
import sqlite3
from typing import Any

from .exceptions import PolicyValidationError

logger = logging.getLogger(__name__)

# All supported policy types
POLICY_TYPES = frozenset({
    "email", "domain", "url", "filepath", "command",
    "int_range", "enum", "bool", "pattern",
})


class SecurityPolicies:
    """In-memory security policy engine with default-deny allowlists.

    Policies are loaded from the database at construction time and cached.
    Call reload() to refresh from DB after policy changes.
    """

    def __init__(self):
        self._allowlists: dict[str, set[str]] = {pt: set() for pt in POLICY_TYPES}

    def add(self, policy_type: str, value: str) -> None:
        """Add a value to an allowlist."""
        _validate_policy_type(policy_type)
        normalized = _normalize_value(policy_type, value)
        self._allowlists[policy_type].add(normalized)

    def remove(self, policy_type: str, value: str) -> bool:
        """Remove a value from an allowlist. Returns True if it was present."""
        _validate_policy_type(policy_type)
        normalized = _normalize_value(policy_type, value)
        try:
            self._allowlists[policy_type].discard(normalized)
            return True
        except KeyError:
            return False

    def get_allowlist(self, policy_type: str) -> frozenset[str]:
        """Return the current allowlist for a policy type."""
        _validate_policy_type(policy_type)
        return frozenset(self._allowlists[policy_type])

    def clear(self, policy_type: str | None = None) -> None:
        """Clear an allowlist (or all allowlists if policy_type is None)."""
        if policy_type is None:
            for pt in POLICY_TYPES:
                self._allowlists[pt] = set()
        else:
            _validate_policy_type(policy_type)
            self._allowlists[policy_type] = set()

    # ── Validation methods ───────────────────────────────────────────

    def validate(self, policy_type: str, value: Any) -> bool:
        """Check if a value is allowed by the given policy type.

        Returns True if allowed, raises PolicyValidationError if denied.
        """
        _validate_policy_type(policy_type)
        checker = _VALIDATORS.get(policy_type)
        if checker is None:
            raise PolicyValidationError(policy_type, str(value), f"Unknown policy type: {policy_type}")
        return checker(self, value)

    def is_allowed(self, policy_type: str, value: Any) -> bool:
        """Check if a value is allowed (returns bool, no exception)."""
        try:
            return self.validate(policy_type, value)
        except PolicyValidationError:
            return False

    # ── Per-type validators ──────────────────────────────────────────

    def _validate_email(self, value: str) -> bool:
        """Validate email against allowlist (exact match, case-insensitive)."""
        normalized = value.strip().lower()
        if normalized not in self._allowlists["email"]:
            raise PolicyValidationError("email", value)
        return True

    def _validate_domain(self, value: str) -> bool:
        """Validate domain (exact or suffix match, case-insensitive).

        "example.com" in the allowlist matches both "example.com" and
        "sub.example.com".
        """
        normalized = value.strip().lower().rstrip(".")
        for allowed in self._allowlists["domain"]:
            if normalized == allowed or normalized.endswith("." + allowed):
                return True
        raise PolicyValidationError("domain", value)

    def _validate_url(self, value: str) -> bool:
        """Validate URL against prefix allowlist."""
        for allowed_prefix in self._allowlists["url"]:
            if value.startswith(allowed_prefix):
                return True
        raise PolicyValidationError("url", value)

    def _validate_filepath(self, value: str) -> bool:
        """Validate file path against prefix allowlist."""
        for allowed_prefix in self._allowlists["filepath"]:
            if value.startswith(allowed_prefix):
                return True
        raise PolicyValidationError("filepath", value)

    def _validate_command(self, value: str) -> bool:
        """Validate command (exact match)."""
        if value not in self._allowlists["command"]:
            raise PolicyValidationError("command", value)
        return True

    def _validate_int_range(self, value: Any) -> bool:
        """Validate integer against range allowlist.

        Allowlist entries are "min:max" strings. Value must fall within
        at least one allowed range.
        """
        try:
            int_val = int(value)
        except (ValueError, TypeError):
            raise PolicyValidationError("int_range", str(value), "Not a valid integer")

        for range_str in self._allowlists["int_range"]:
            parts = range_str.split(":")
            if len(parts) == 2:
                lo, hi = int(parts[0]), int(parts[1])
                if lo <= int_val <= hi:
                    return True
        raise PolicyValidationError("int_range", str(value))

    def _validate_enum(self, value: str) -> bool:
        """Validate string against enum allowlist (exact match)."""
        if value not in self._allowlists["enum"]:
            raise PolicyValidationError("enum", value)
        return True

    def _validate_bool(self, value: Any) -> bool:
        """Validate boolean against allowlist.

        Allowlist can contain "true", "false", or both.
        """
        bool_str = str(bool(value)).lower()
        if bool_str not in self._allowlists["bool"]:
            raise PolicyValidationError("bool", str(value))
        return True

    def _validate_pattern(self, value: str) -> bool:
        """Validate string against regex pattern allowlist.

        Value must match at least one allowed pattern (full match).
        """
        for pattern_str in self._allowlists["pattern"]:
            try:
                if re.fullmatch(pattern_str, value):
                    return True
            except re.error:
                logger.warning("Invalid regex pattern in allowlist: %s", pattern_str)
        raise PolicyValidationError("pattern", value)


# Validator dispatch table
_VALIDATORS = {
    "email": SecurityPolicies._validate_email,
    "domain": SecurityPolicies._validate_domain,
    "url": SecurityPolicies._validate_url,
    "filepath": SecurityPolicies._validate_filepath,
    "command": SecurityPolicies._validate_command,
    "int_range": SecurityPolicies._validate_int_range,
    "enum": SecurityPolicies._validate_enum,
    "bool": SecurityPolicies._validate_bool,
    "pattern": SecurityPolicies._validate_pattern,
}


# ── Helpers ──────────────────────────────────────────────────────────

def _validate_policy_type(policy_type: str) -> None:
    """Raise ValueError if policy_type is not recognized."""
    if policy_type not in POLICY_TYPES:
        raise ValueError(
            f"Unknown policy type '{policy_type}'. "
            f"Must be one of: {', '.join(sorted(POLICY_TYPES))}"
        )


def _normalize_value(policy_type: str, value: str) -> str:
    """Normalize a policy value for storage/comparison."""
    if policy_type in ("email", "domain"):
        return value.strip().lower().rstrip(".")
    return value


# ── Module-level singleton ───────────────────────────────────────────

_singleton: SecurityPolicies | None = None


def get_policies(_db_conn=None) -> SecurityPolicies:
    """Return the module-level SecurityPolicies singleton.

    Creates a fresh instance on first call. Call reload_policies()
    to refresh from DB after policy changes.

    Args:
        _db_conn: Optional existing DB connection. Callers inside a
            ``db_transaction()`` MUST pass their connection so the
            first-call ``_load_from_db()`` doesn't open a second
            connection (which would deadlock on SQLite's WAL writer
            lock until the 30 s timeout).
    """
    global _singleton
    if _singleton is None:
        _singleton = SecurityPolicies()
        _load_from_db(_singleton, _db_conn=_db_conn)
        _load_from_config(_singleton)
    return _singleton


def reload_policies(_db_conn=None) -> SecurityPolicies:
    """Recreate the singleton from DB + config. Returns the new instance.

    Args:
        _db_conn: Optional existing DB connection (see ``get_policies``).
    """
    global _singleton
    _singleton = SecurityPolicies()
    _load_from_db(_singleton, _db_conn=_db_conn)
    _load_from_config(_singleton)
    return _singleton


def _load_from_db(policies: SecurityPolicies, _db_conn=None) -> None:
    """Load policies from security_policies DB table.

    Args:
        policies: SecurityPolicies instance to populate.
        _db_conn: Optional existing DB connection. When provided,
            the function uses it directly instead of opening a new
            one. Required when called from inside a ``db_transaction()``
            on the same thread.
    """
    try:
        from ..db import db_connection
        if _db_conn is not None:
            rows = _db_conn.execute(
                "SELECT policy_type, value FROM security_policies"
            ).fetchall()
        else:
            with db_connection() as db:
                rows = db.execute(
                    "SELECT policy_type, value FROM security_policies"
                ).fetchall()
        for row in rows:
            try:
                policies.add(row["policy_type"], row["value"])
            except ValueError:
                logger.warning(
                    "Skipping unknown policy type '%s' from DB", row["policy_type"]
                )
    except (sqlite3.Error, KeyError, ValueError) as _exc:
        logger.debug("Could not load policies from DB (table may not exist yet)")


def _load_from_config(policies: SecurityPolicies) -> None:
    """Load policies from config.yaml security section."""
    try:
        from .. import config
        security_cfg = config.CONFIG.get("security", {})
        for config_key, policy_type in _CONFIG_KEY_MAP.items():
            values = security_cfg.get(config_key, [])
            if isinstance(values, list):
                for v in values:
                    policies.add(policy_type, str(v))
    except (ImportError, KeyError, TypeError, ValueError) as _exc:
        logger.debug("Could not load policies from config")


# Mapping from config keys to policy types
_CONFIG_KEY_MAP = {
    "email_allowlist": "email",
    "domain_allowlist": "domain",
    "url_allowlist": "url",
    "filepath_allowlist": "filepath",
    "command_allowlist": "command",
}
