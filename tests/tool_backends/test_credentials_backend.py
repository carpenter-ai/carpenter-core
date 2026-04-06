"""Tests for credentials tool backend."""
import pytest

from carpenter.tool_backends.credentials import (
    handle_request,
    handle_verify,
    handle_import_file,
)


class TestCredentialsBackend:

    def test_request_missing_key(self):
        result = handle_request({})
        assert "error" in result

    def test_request_creates_link(self, monkeypatch):
        monkeypatch.setattr("carpenter.api.credentials.config.CONFIG", {"base_dir": "/tmp"})
        result = handle_request({"key": "TEST_TOKEN"})
        assert "request_id" in result
        assert "url" in result

    def test_verify_missing_key(self):
        result = handle_verify({})
        assert "error" in result

    def test_verify_not_set(self, monkeypatch):
        monkeypatch.setattr("carpenter.api.credentials.config.CONFIG", {})
        monkeypatch.setattr("carpenter.api.credentials.config._CREDENTIAL_MAP", {})
        result = handle_verify({"key": "NONEXISTENT_KEY"})
        assert result["valid"] is False

    def test_import_file_missing_path(self):
        result = handle_import_file({"key": "TEST_TOKEN"})
        assert "error" in result

    def test_import_file_missing_key(self):
        result = handle_import_file({"path": "/tmp/test"})
        assert "error" in result

    def test_import_file_not_found(self):
        result = handle_import_file({"path": "/tmp/nonexistent_cred_file", "key": "TEST"})
        assert result["stored"] is False
        assert "not found" in result.get("error", "")
