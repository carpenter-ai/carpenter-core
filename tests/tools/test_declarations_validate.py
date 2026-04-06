"""Tests for declaration validators (carpenter_tools/_declarations_validate.py)."""

import os
import pytest

from carpenter_tools._declarations_validate import validate_declaration
from carpenter_tools.declarations import (
    Label, Email, URL, WorkspacePath, SQL, JSON, UnstructuredText,
)


class TestLabelValidation:
    def test_valid_simple(self):
        validate_declaration("label", "status")

    def test_valid_with_hyphens(self):
        validate_declaration("label", "my-key")

    def test_valid_with_dots(self):
        validate_declaration("label", "path/name.ext")

    def test_valid_with_underscores(self):
        validate_declaration("label", "my_key_123")

    def test_invalid_too_long(self):
        with pytest.raises(ValueError, match="too long"):
            validate_declaration("label", "a" * 65)

    def test_invalid_spaces(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_declaration("label", "has space")

    def test_invalid_special_chars(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_declaration("label", "bad@char")

    def test_invalid_empty(self):
        with pytest.raises(ValueError, match="empty"):
            validate_declaration("label", "")

    def test_max_length_ok(self):
        validate_declaration("label", "a" * 64)


class TestEmailValidation:
    def test_valid_simple(self):
        validate_declaration("email", "user@example.com")

    def test_valid_subdomain(self):
        validate_declaration("email", "user@sub.example.com")

    def test_invalid_no_at(self):
        with pytest.raises(ValueError, match="Invalid email"):
            validate_declaration("email", "no-at-sign")

    def test_invalid_missing_local(self):
        with pytest.raises(ValueError, match="Invalid email"):
            validate_declaration("email", "@missing-local.com")

    def test_invalid_missing_domain(self):
        with pytest.raises(ValueError, match="Invalid email"):
            validate_declaration("email", "user@")

    def test_invalid_no_tld(self):
        with pytest.raises(ValueError, match="Invalid email"):
            validate_declaration("email", "user@domain")


class TestURLValidation:
    def test_valid_https(self):
        validate_declaration("url", "https://example.com")

    def test_valid_http(self):
        validate_declaration("url", "http://example.com/path")

    def test_valid_with_port(self):
        validate_declaration("url", "https://example.com:8080/path")

    def test_invalid_ftp(self):
        with pytest.raises(ValueError, match="scheme"):
            validate_declaration("url", "ftp://blocked.com")

    def test_invalid_no_scheme(self):
        with pytest.raises(ValueError, match="scheme"):
            validate_declaration("url", "example.com/path")

    def test_invalid_no_domain(self):
        with pytest.raises(ValueError, match="no domain"):
            validate_declaration("url", "https://")


class TestWorkspacePathValidation:
    def test_valid_simple(self):
        validate_declaration("workspace_path", "results.json")

    def test_valid_nested(self):
        validate_declaration("workspace_path", "subdir/file.txt")

    def test_invalid_traversal(self):
        with pytest.raises(ValueError, match="\\.\\."):
            validate_declaration("workspace_path", "../escape")

    def test_invalid_traversal_nested(self):
        with pytest.raises(ValueError, match="\\.\\."):
            validate_declaration("workspace_path", "dir/../escape")

    def test_invalid_absolute(self):
        with pytest.raises(ValueError, match="relative"):
            validate_declaration("workspace_path", "/etc/passwd")


class TestSQLValidation:
    def test_valid_select(self):
        validate_declaration("sql", "SELECT * FROM users WHERE id = ?")

    def test_valid_insert(self):
        validate_declaration("sql", "INSERT INTO users (name) VALUES (?)")

    def test_valid_update(self):
        validate_declaration("sql", "UPDATE users SET name = ? WHERE id = ?")

    def test_valid_delete(self):
        validate_declaration("sql", "DELETE FROM users WHERE id = ?")

    def test_valid_case_insensitive(self):
        validate_declaration("sql", "select * from users")

    def test_invalid_drop(self):
        with pytest.raises(ValueError, match="must start with"):
            validate_declaration("sql", "DROP TABLE users")

    def test_invalid_tautology(self):
        with pytest.raises(ValueError, match="tautology"):
            validate_declaration("sql", "SELECT * FROM users WHERE 1=1")

    def test_invalid_create(self):
        with pytest.raises(ValueError, match="must start with"):
            validate_declaration("sql", "CREATE TABLE evil (id int)")


class TestJSONValidation:
    def test_valid_object(self):
        validate_declaration("json", '{"key": "value"}')

    def test_valid_array(self):
        validate_declaration("json", "[1, 2, 3]")

    def test_valid_string(self):
        validate_declaration("json", '"hello"')

    def test_valid_number(self):
        validate_declaration("json", "42")

    def test_valid_null(self):
        validate_declaration("json", "null")

    def test_invalid_broken(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            validate_declaration("json", "{broken")

    def test_invalid_not_json(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            validate_declaration("json", "not json at all")


class TestUnstructuredTextValidation:
    def test_always_valid(self):
        validate_declaration("unstructured_text", "anything goes here")

    def test_empty_string(self):
        validate_declaration("unstructured_text", "")

    def test_special_characters(self):
        validate_declaration("unstructured_text", "!@#$%^&*()")


class TestVerificationMode:
    """Validation only fires in verification mode."""

    def test_invalid_label_passes_outside_verification(self):
        # Outside verification mode, constructors don't validate
        old = os.environ.pop("CARPENTER_VERIFICATION_MODE", None)
        try:
            label = Label("has space")  # would fail validation
            assert label == "has space"
        finally:
            if old is not None:
                os.environ["CARPENTER_VERIFICATION_MODE"] = old

    def test_invalid_label_fails_in_verification_mode(self):
        old = os.environ.get("CARPENTER_VERIFICATION_MODE")
        os.environ["CARPENTER_VERIFICATION_MODE"] = "1"
        try:
            with pytest.raises(ValueError):
                Label("has space")
        finally:
            if old is None:
                os.environ.pop("CARPENTER_VERIFICATION_MODE", None)
            else:
                os.environ["CARPENTER_VERIFICATION_MODE"] = old

    def test_valid_label_passes_in_verification_mode(self):
        old = os.environ.get("CARPENTER_VERIFICATION_MODE")
        os.environ["CARPENTER_VERIFICATION_MODE"] = "1"
        try:
            label = Label("valid-label")
            assert label == "valid-label"
        finally:
            if old is None:
                os.environ.pop("CARPENTER_VERIFICATION_MODE", None)
            else:
                os.environ["CARPENTER_VERIFICATION_MODE"] = old
