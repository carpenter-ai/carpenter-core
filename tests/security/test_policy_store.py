"""Tests for carpenter.security.policy_store."""

import pytest

from carpenter.security import policy_store
from carpenter.security.policies import get_policies, reload_policies


class TestPolicyStoreCRUD:

    def test_add_to_allowlist(self):
        row_id = policy_store.add_to_allowlist("email", "test@example.com")
        assert row_id > 0

        entries = policy_store.get_allowlist("email")
        assert "test@example.com" in entries

    def test_add_duplicate_is_idempotent(self):
        policy_store.add_to_allowlist("email", "dup@test.com")
        policy_store.add_to_allowlist("email", "dup@test.com")
        entries = policy_store.get_allowlist("email")
        assert entries.count("dup@test.com") == 1

    def test_remove_from_allowlist(self):
        policy_store.add_to_allowlist("domain", "remove-me.com")
        assert policy_store.remove_from_allowlist("domain", "remove-me.com") is True
        entries = policy_store.get_allowlist("domain")
        assert "remove-me.com" not in entries

    def test_remove_nonexistent(self):
        assert policy_store.remove_from_allowlist("domain", "never-added.com") is False

    def test_get_allowlist_empty(self):
        entries = policy_store.get_allowlist("command")
        assert entries == []

    def test_get_all_policies(self):
        policy_store.add_to_allowlist("email", "all@test.com")
        policy_store.add_to_allowlist("domain", "all-test.com")
        result = policy_store.get_all_policies()
        assert "email" in result
        assert "domain" in result
        assert "all@test.com" in result["email"]

    def test_clear_allowlist(self):
        policy_store.add_to_allowlist("url", "https://clear-me.com")
        count = policy_store.clear_allowlist("url")
        assert count >= 1
        entries = policy_store.get_allowlist("url")
        assert "https://clear-me.com" not in entries

    def test_invalid_policy_type(self):
        with pytest.raises(ValueError, match="Unknown policy type"):
            policy_store.add_to_allowlist("nonexistent", "value")


class TestPolicyVersion:

    def test_version_starts_at_zero(self):
        version = policy_store.get_policy_version()
        assert version >= 0

    def test_version_increments_on_add(self):
        v1 = policy_store.get_policy_version()
        policy_store.add_to_allowlist("email", f"version-test-{v1}@example.com")
        v2 = policy_store.get_policy_version()
        assert v2 > v1

    def test_version_increments_on_remove(self):
        policy_store.add_to_allowlist("email", "version-rm@example.com")
        v1 = policy_store.get_policy_version()
        policy_store.remove_from_allowlist("email", "version-rm@example.com")
        v2 = policy_store.get_policy_version()
        assert v2 > v1


class TestSingletonSync:

    def test_add_syncs_to_singleton(self):
        policy_store.add_to_allowlist("email", "sync-test@example.com")
        policies = get_policies()
        assert policies.is_allowed("email", "sync-test@example.com") is True

    def test_remove_syncs_to_singleton(self):
        policy_store.add_to_allowlist("domain", "sync-remove.com")
        policy_store.remove_from_allowlist("domain", "sync-remove.com")
        policies = get_policies()
        assert policies.is_allowed("domain", "sync-remove.com") is False

    def test_reload_from_db(self):
        policy_store.add_to_allowlist("command", "reload-test-cmd")
        policies = reload_policies()
        assert policies.is_allowed("command", "reload-test-cmd") is True
