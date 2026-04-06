"""Tests for carpenter_tools.policy types (policy-typed literals)."""

import os
import pytest
from unittest.mock import patch

from carpenter_tools.policy import (
    EmailPolicy, Domain, Url, FilePath, Command,
    IntRange, Enum, Bool, Pattern, PolicyLiteral,
)


# ── PolicyLiteral base ───────────────────────────────────────────────

class TestPolicyLiteralBase:

    def test_value_property(self):
        lit = PolicyLiteral("hello")
        assert lit.value == "hello"

    def test_eq_same_type(self):
        a = PolicyLiteral("x")
        b = PolicyLiteral("x")
        assert a == b

    def test_eq_raw_value(self):
        lit = PolicyLiteral("x")
        assert lit == "x"

    def test_repr(self):
        lit = PolicyLiteral("test")
        assert "PolicyLiteral" in repr(lit)
        assert "test" in repr(lit)

    def test_str(self):
        lit = PolicyLiteral(42)
        assert str(lit) == "42"

    def test_hash(self):
        a = PolicyLiteral("x")
        b = PolicyLiteral("x")
        assert hash(a) == hash(b)
        assert {a, b} == {a}

    def test_no_validation_in_runtime_mode(self):
        """In normal mode (no CARPENTER_VERIFICATION_MODE), no validation occurs."""
        # This should not raise even though no policies are configured
        EmailPolicy("nobody@nowhere.com")
        Domain("nonexistent.example")
        Command("rm -rf /")


# ── Email ────────────────────────────────────────────────────────────

class TestEmailPolicy:

    def test_case_insensitive_comparison(self):
        e = EmailPolicy("Alice@Example.COM")
        assert e == "alice@example.com"
        assert e == "ALICE@EXAMPLE.COM"

    def test_whitespace_stripped(self):
        e = EmailPolicy("  user@test.com  ")
        assert e.value == "user@test.com"

    def test_repr(self):
        e = EmailPolicy("a@b.com")
        assert repr(e) == "EmailPolicy('a@b.com')"


# ── Domain ───────────────────────────────────────────────────────────

class TestDomain:

    def test_exact_match(self):
        d = Domain("example.com")
        assert d == "example.com"

    def test_subdomain_match(self):
        d = Domain("example.com")
        assert d == "sub.example.com"
        assert d == "deep.sub.example.com"

    def test_no_partial_match(self):
        d = Domain("example.com")
        assert d != "notexample.com"

    def test_matches_method(self):
        d = Domain("api.example.com")
        assert d.matches("api.example.com") is True
        assert d.matches("v2.api.example.com") is True
        assert d.matches("example.com") is False

    def test_trailing_dot_stripped(self):
        d = Domain("example.com.")
        assert d.value == "example.com"
        assert d == "example.com"


# ── Url ──────────────────────────────────────────────────────────────

class TestUrl:

    def test_prefix_match(self):
        u = Url("https://api.example.com/v1")
        assert u == "https://api.example.com/v1/users"
        assert u == "https://api.example.com/v1"
        assert u != "https://api.example.com/v2"

    def test_matches_method(self):
        u = Url("https://example.com")
        assert u.matches("https://example.com/path") is True
        assert u.matches("http://example.com") is False


# ── FilePath ─────────────────────────────────────────────────────────

class TestFilePath:

    def test_prefix_match(self):
        fp = FilePath("/home/user/safe/")
        assert fp == "/home/user/safe/file.txt"
        assert fp != "/home/user/unsafe/file.txt"

    def test_matches_method(self):
        fp = FilePath("/tmp/")
        assert fp.matches("/tmp/test") is True
        assert fp.matches("/var/tmp") is False


# ── Command ──────────────────────────────────────────────────────────

class TestCommand:

    def test_exact_match(self):
        c = Command("git pull")
        assert c == "git pull"
        assert c != "git push"


# ── IntRange ─────────────────────────────────────────────────────────

class TestIntRange:

    def test_contains(self):
        r = IntRange(1, 100)
        assert 50 in r
        assert 1 in r
        assert 100 in r
        assert 0 not in r
        assert 101 not in r

    def test_eq_int(self):
        r = IntRange(80, 443)
        assert r == 80
        assert r == 443
        assert r == 200
        assert r != 79

    def test_eq_range(self):
        r1 = IntRange(1, 10)
        r2 = IntRange(1, 10)
        r3 = IntRange(1, 20)
        assert r1 == r2
        assert r1 != r3

    def test_repr(self):
        r = IntRange(0, 65535)
        assert repr(r) == "IntRange(0, 65535)"

    def test_lo_hi_properties(self):
        r = IntRange(10, 20)
        assert r.lo == 10
        assert r.hi == 20


# ── Enum ─────────────────────────────────────────────────────────────

class TestEnum:

    def test_exact_match(self):
        e = Enum("red")
        assert e == "red"
        assert e != "blue"


# ── Bool ─────────────────────────────────────────────────────────────

class TestBool:

    def test_true(self):
        b = Bool(True)
        assert b == True  # noqa: E712
        assert b != False  # noqa: E712

    def test_false(self):
        b = Bool(False)
        assert b == False  # noqa: E712
        assert b != True  # noqa: E712

    def test_value(self):
        assert Bool(True).value is True
        assert Bool(False).value is False


# ── Pattern ──────────────────────────────────────────────────────────

class TestPattern:

    def test_regex_match(self):
        p = Pattern(r"\d{3}-\d{4}")
        assert p == "123-4567"
        assert p != "abc-defg"

    def test_full_match_required(self):
        p = Pattern(r"\d+")
        assert p == "123"
        assert p != "123abc"

    def test_matches_method(self):
        p = Pattern(r"[a-z]+@[a-z]+\.[a-z]+")
        assert p.matches("user@example.com") is True
        assert p.matches("USER@EXAMPLE.COM") is False


# ── Verification mode ────────────────────────────────────────────────

class TestVerificationMode:

    def test_verification_mode_calls_validate(self):
        """In verification mode, constructor triggers validation."""
        with patch.dict(os.environ, {"CARPENTER_VERIFICATION_MODE": "1"}):
            with patch("carpenter_tools.policy._validate.validate_policy_value") as mock:
                mock.return_value = True
                e = EmailPolicy("test@example.com")
                mock.assert_called_once_with("email", "test@example.com")

    def test_runtime_mode_skips_validation(self):
        """Without CARPENTER_VERIFICATION_MODE, no validation is called."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove the env var if it exists
            os.environ.pop("CARPENTER_VERIFICATION_MODE", None)
            with patch("carpenter_tools.policy._validate.validate_policy_value") as mock:
                EmailPolicy("test@example.com")
                mock.assert_not_called()


# ── Hash and set behavior ────────────────────────────────────────────

class TestHashBehavior:

    def test_different_types_different_hash(self):
        """Different policy types with same value have different hashes."""
        e = EmailPolicy("test")
        c = Command("test")
        assert hash(e) != hash(c)

    def test_set_dedup(self):
        """Same type and value dedup in sets."""
        s = {EmailPolicy("a@b.com"), EmailPolicy("a@b.com")}
        assert len(s) == 1

    def test_dict_key(self):
        """Policy literals can be used as dict keys."""
        d = {EmailPolicy("a@b.com"): "found"}
        assert d[EmailPolicy("a@b.com")] == "found"
