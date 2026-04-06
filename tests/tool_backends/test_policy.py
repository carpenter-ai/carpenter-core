"""Tests for carpenter.tool_backends.policy (platform-side handler)."""

import pytest

from carpenter.tool_backends.policy import handle_validate
from carpenter.security.policies import get_policies, reload_policies
from carpenter.security import policy_store


class TestHandleValidate:

    def test_allowed_value(self):
        policy_store.add_to_allowlist("email", "allowed@example.com")
        result = handle_validate({
            "policy_type": "email",
            "value": "allowed@example.com",
        })
        assert result["allowed"] is True

    def test_denied_value(self):
        result = handle_validate({
            "policy_type": "email",
            "value": "denied@nowhere.com",
        })
        assert result["allowed"] is False
        assert "reason" in result

    def test_unknown_policy_type(self):
        result = handle_validate({
            "policy_type": "nonexistent",
            "value": "test",
        })
        assert result["allowed"] is False

    def test_missing_policy_type(self):
        result = handle_validate({"value": "test"})
        assert result["allowed"] is False
        assert "Missing" in result["reason"]

    def test_domain_validation(self):
        policy_store.add_to_allowlist("domain", "example.com")
        assert handle_validate({
            "policy_type": "domain",
            "value": "sub.example.com",
        })["allowed"] is True

        assert handle_validate({
            "policy_type": "domain",
            "value": "evil.com",
        })["allowed"] is False

    def test_int_range_validation(self):
        policy_store.add_to_allowlist("int_range", "80:443")
        assert handle_validate({
            "policy_type": "int_range",
            "value": "200",
        })["allowed"] is True

        assert handle_validate({
            "policy_type": "int_range",
            "value": "8080",
        })["allowed"] is False

    def test_command_validation(self):
        policy_store.add_to_allowlist("command", "git status")
        assert handle_validate({
            "policy_type": "command",
            "value": "git status",
        })["allowed"] is True

        assert handle_validate({
            "policy_type": "command",
            "value": "rm -rf /",
        })["allowed"] is False
