"""Tests for carpenter.security.policies."""

import pytest

from carpenter.security.policies import (
    SecurityPolicies,
    POLICY_TYPES,
    _validate_policy_type,
)
from carpenter.security.exceptions import PolicyValidationError


# ── SecurityPolicies basics ──────────────────────────────────────────

class TestSecurityPoliciesBasics:

    def test_empty_policies_deny_all(self):
        """Default-deny: empty allowlists reject everything."""
        policies = SecurityPolicies()
        assert policies.is_allowed("email", "user@example.com") is False
        assert policies.is_allowed("domain", "example.com") is False
        assert policies.is_allowed("command", "ls") is False

    def test_add_and_validate(self):
        policies = SecurityPolicies()
        policies.add("email", "alice@example.com")
        assert policies.is_allowed("email", "alice@example.com") is True
        assert policies.is_allowed("email", "bob@example.com") is False

    def test_remove(self):
        policies = SecurityPolicies()
        policies.add("email", "alice@example.com")
        policies.remove("email", "alice@example.com")
        assert policies.is_allowed("email", "alice@example.com") is False

    def test_get_allowlist(self):
        policies = SecurityPolicies()
        policies.add("domain", "example.com")
        policies.add("domain", "test.org")
        al = policies.get_allowlist("domain")
        assert "example.com" in al
        assert "test.org" in al

    def test_clear_specific(self):
        policies = SecurityPolicies()
        policies.add("email", "a@b.com")
        policies.add("domain", "b.com")
        policies.clear("email")
        assert policies.get_allowlist("email") == frozenset()
        assert len(policies.get_allowlist("domain")) == 1

    def test_clear_all(self):
        policies = SecurityPolicies()
        policies.add("email", "a@b.com")
        policies.add("domain", "b.com")
        policies.clear()
        assert policies.get_allowlist("email") == frozenset()
        assert policies.get_allowlist("domain") == frozenset()

    def test_invalid_policy_type(self):
        policies = SecurityPolicies()
        with pytest.raises(ValueError, match="Unknown policy type"):
            policies.add("nonexistent", "value")

    def test_all_policy_types_exist(self):
        assert len(POLICY_TYPES) == 9
        expected = {"email", "domain", "url", "filepath", "command",
                    "int_range", "enum", "bool", "pattern"}
        assert POLICY_TYPES == expected


# ── Email validation ─────────────────────────────────────────────────

class TestEmailValidation:

    def test_case_insensitive(self):
        policies = SecurityPolicies()
        policies.add("email", "Alice@Example.COM")
        assert policies.is_allowed("email", "alice@example.com") is True
        assert policies.is_allowed("email", "ALICE@EXAMPLE.COM") is True

    def test_whitespace_stripped(self):
        policies = SecurityPolicies()
        policies.add("email", "  user@test.com  ")
        assert policies.is_allowed("email", "user@test.com") is True

    def test_raises_policy_validation_error(self):
        policies = SecurityPolicies()
        with pytest.raises(PolicyValidationError) as exc_info:
            policies.validate("email", "unknown@bad.com")
        assert exc_info.value.policy_type == "email"
        assert exc_info.value.value == "unknown@bad.com"


# ── Domain validation ────────────────────────────────────────────────

class TestDomainValidation:

    def test_exact_match(self):
        policies = SecurityPolicies()
        policies.add("domain", "example.com")
        assert policies.is_allowed("domain", "example.com") is True

    def test_subdomain_match(self):
        policies = SecurityPolicies()
        policies.add("domain", "example.com")
        assert policies.is_allowed("domain", "sub.example.com") is True
        assert policies.is_allowed("domain", "deep.sub.example.com") is True

    def test_no_partial_match(self):
        """'notexample.com' should NOT match 'example.com'."""
        policies = SecurityPolicies()
        policies.add("domain", "example.com")
        assert policies.is_allowed("domain", "notexample.com") is False

    def test_case_insensitive(self):
        policies = SecurityPolicies()
        policies.add("domain", "Example.COM")
        assert policies.is_allowed("domain", "example.com") is True

    def test_trailing_dot_stripped(self):
        policies = SecurityPolicies()
        policies.add("domain", "example.com.")
        assert policies.is_allowed("domain", "example.com") is True


# ── URL validation ───────────────────────────────────────────────────

class TestUrlValidation:

    def test_prefix_match(self):
        policies = SecurityPolicies()
        policies.add("url", "https://api.example.com/v1")
        assert policies.is_allowed("url", "https://api.example.com/v1/users") is True
        assert policies.is_allowed("url", "https://api.example.com/v2/users") is False

    def test_exact_match(self):
        policies = SecurityPolicies()
        policies.add("url", "https://example.com")
        assert policies.is_allowed("url", "https://example.com") is True


# ── Filepath validation ──────────────────────────────────────────────

class TestFilepathValidation:

    def test_prefix_match(self):
        policies = SecurityPolicies()
        policies.add("filepath", "/home/user/safe/")
        assert policies.is_allowed("filepath", "/home/user/safe/file.txt") is True
        assert policies.is_allowed("filepath", "/home/user/unsafe/file.txt") is False


# ── Command validation ───────────────────────────────────────────────

class TestCommandValidation:

    def test_exact_match(self):
        policies = SecurityPolicies()
        policies.add("command", "git pull")
        assert policies.is_allowed("command", "git pull") is True
        assert policies.is_allowed("command", "git push") is False


# ── Int range validation ─────────────────────────────────────────────

class TestIntRangeValidation:

    def test_within_range(self):
        policies = SecurityPolicies()
        policies.add("int_range", "1:100")
        assert policies.is_allowed("int_range", 50) is True
        assert policies.is_allowed("int_range", 1) is True
        assert policies.is_allowed("int_range", 100) is True
        assert policies.is_allowed("int_range", 0) is False
        assert policies.is_allowed("int_range", 101) is False

    def test_multiple_ranges(self):
        policies = SecurityPolicies()
        policies.add("int_range", "1:10")
        policies.add("int_range", "20:30")
        assert policies.is_allowed("int_range", 5) is True
        assert policies.is_allowed("int_range", 25) is True
        assert policies.is_allowed("int_range", 15) is False

    def test_invalid_integer(self):
        policies = SecurityPolicies()
        policies.add("int_range", "1:10")
        with pytest.raises(PolicyValidationError, match="Not a valid integer"):
            policies.validate("int_range", "abc")


# ── Enum validation ──────────────────────────────────────────────────

class TestEnumValidation:

    def test_exact_match(self):
        policies = SecurityPolicies()
        policies.add("enum", "red")
        policies.add("enum", "green")
        policies.add("enum", "blue")
        assert policies.is_allowed("enum", "red") is True
        assert policies.is_allowed("enum", "yellow") is False


# ── Bool validation ──────────────────────────────────────────────────

class TestBoolValidation:

    def test_bool_allowed(self):
        policies = SecurityPolicies()
        policies.add("bool", "true")
        assert policies.is_allowed("bool", True) is True
        assert policies.is_allowed("bool", False) is False

    def test_both_bools(self):
        policies = SecurityPolicies()
        policies.add("bool", "true")
        policies.add("bool", "false")
        assert policies.is_allowed("bool", True) is True
        assert policies.is_allowed("bool", False) is True


# ── Pattern validation ───────────────────────────────────────────────

class TestPatternValidation:

    def test_regex_match(self):
        policies = SecurityPolicies()
        policies.add("pattern", r"\d{3}-\d{4}")
        assert policies.is_allowed("pattern", "123-4567") is True
        assert policies.is_allowed("pattern", "abc-defg") is False

    def test_full_match_required(self):
        policies = SecurityPolicies()
        policies.add("pattern", r"\d+")
        assert policies.is_allowed("pattern", "123") is True
        assert policies.is_allowed("pattern", "123abc") is False

    def test_invalid_regex_skipped(self):
        policies = SecurityPolicies()
        policies.add("pattern", r"[invalid")  # bad regex
        policies.add("pattern", r"\d+")
        # Bad regex is skipped, valid one still works
        assert policies.is_allowed("pattern", "123") is True


# ── validate_policy_type helper ──────────────────────────────────────

class TestValidatePolicyType:

    def test_valid_types(self):
        for pt in POLICY_TYPES:
            _validate_policy_type(pt)  # should not raise

    def test_invalid_type(self):
        with pytest.raises(ValueError, match="Unknown policy type"):
            _validate_policy_type("invalid")
