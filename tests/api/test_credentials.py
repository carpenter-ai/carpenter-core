"""Tests for carpenter.api.credentials."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from carpenter.api.credentials import (
    create_credential_request,
    get_credential_request,
    clear_credential_requests,
    verify_credential,
    import_credential_file,
    _update_dot_env,
)


def setup_function():
    clear_credential_requests()


# ---------------------------------------------------------------------------
# create_credential_request
# ---------------------------------------------------------------------------


def test_create_credential_request():
    """Create a one-time credential request."""
    result = create_credential_request(
        key="FORGEJO_TOKEN",
        label="Forgejo API Token",
        description="Token for accessing the forge API.",
    )

    assert "request_id" in result
    assert result["url"].startswith("/api/credentials/")

    req = get_credential_request(result["request_id"])
    assert req is not None
    assert req["key"] == "FORGEJO_TOKEN"
    assert req["label"] == "Forgejo API Token"
    assert req["description"] == "Token for accessing the forge API."
    assert req["fulfilled"] is False


def test_create_multiple_requests():
    """Multiple requests get unique IDs."""
    r1 = create_credential_request(key="KEY_A")
    r2 = create_credential_request(key="KEY_B")
    assert r1["request_id"] != r2["request_id"]


# ---------------------------------------------------------------------------
# _update_dot_env
# ---------------------------------------------------------------------------


def test_update_dot_env_new_key(tmp_path, monkeypatch):
    """Write a new key to .env."""
    monkeypatch.setattr("carpenter.api.credentials.config.CONFIG",
                        {"base_dir": str(tmp_path)})

    _update_dot_env("NEW_KEY", "secret_value")

    dot_env = (tmp_path / ".env").read_text()
    assert "NEW_KEY=secret_value" in dot_env


def test_update_dot_env_update_existing(tmp_path, monkeypatch):
    """Update an existing key in .env."""
    (tmp_path / ".env").write_text("EXISTING_KEY=old_value\nOTHER=keep\n")
    monkeypatch.setattr("carpenter.api.credentials.config.CONFIG",
                        {"base_dir": str(tmp_path)})

    _update_dot_env("EXISTING_KEY", "new_value")

    dot_env = (tmp_path / ".env").read_text()
    assert "EXISTING_KEY=new_value" in dot_env
    assert "OTHER=keep" in dot_env
    assert "old_value" not in dot_env


# ---------------------------------------------------------------------------
# verify_credential
# ---------------------------------------------------------------------------


def test_verify_credential_not_set(monkeypatch):
    """Verify returns invalid when credential is not set."""
    monkeypatch.setattr("carpenter.api.credentials.config.CONFIG",
                        {"git_token": ""})
    result = verify_credential("GIT_TOKEN")
    assert result["valid"] is False
    assert "not set" in result["reason"]


def test_verify_credential_git_token_success(monkeypatch):
    """Verify git token calls the API and returns username."""
    monkeypatch.setattr("carpenter.api.credentials.config.CONFIG", {
        "git_token": "tok_abc",
        "git_server_url": "https://forge.example.com",
    })

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"login": "bot-user"}

    with patch("carpenter.api.credentials.httpx") as mock_httpx:
        mock_httpx.get.return_value = mock_response
        result = verify_credential("GIT_TOKEN")

    assert result["valid"] is True
    assert result["username"] == "bot-user"

    call_args = mock_httpx.get.call_args
    assert "/api/v1/user" in call_args[0][0]


def test_verify_credential_forgejo_token_backward_compat(monkeypatch):
    """Verify FORGEJO_TOKEN still works via backward compat."""
    monkeypatch.setattr("carpenter.api.credentials.config.CONFIG", {
        "git_token": "tok_abc",
        "git_server_url": "https://forge.example.com",
    })

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"login": "bot-user"}

    with patch("carpenter.api.credentials.httpx") as mock_httpx:
        mock_httpx.get.return_value = mock_response
        result = verify_credential("FORGEJO_TOKEN")

    assert result["valid"] is True
    assert result["username"] == "bot-user"


def test_verify_credential_git_token_failure(monkeypatch):
    """Verify git token returns invalid on HTTP error."""
    monkeypatch.setattr("carpenter.api.credentials.config.CONFIG", {
        "git_token": "bad_token",
        "git_server_url": "https://forge.example.com",
    })

    mock_response = MagicMock()
    mock_response.status_code = 401

    with patch("carpenter.api.credentials.httpx") as mock_httpx:
        mock_httpx.get.return_value = mock_response
        result = verify_credential("GIT_TOKEN")

    assert result["valid"] is False


def test_verify_credential_generic(monkeypatch):
    """Verify generic credential checks non-empty."""
    monkeypatch.setattr("carpenter.api.credentials.config.CONFIG",
                        {"some_key": "some_value"})
    monkeypatch.setattr("carpenter.api.credentials.config._CREDENTIAL_MAP",
                        {"SOME_KEY": "some_key"})
    result = verify_credential("SOME_KEY")
    assert result["valid"] is True


# ---------------------------------------------------------------------------
# import_credential_file
# ---------------------------------------------------------------------------


def test_import_credential_file(tmp_path, monkeypatch):
    """Import credential from file, store in .env, delete file."""
    monkeypatch.setattr("carpenter.api.credentials.config.CONFIG",
                        {"base_dir": str(tmp_path)})

    cred_file = tmp_path / "token.txt"
    cred_file.write_text("my_secret_token\n")

    result = import_credential_file(str(cred_file), "MY_TOKEN")

    assert result["stored"] is True
    assert result["key"] == "MY_TOKEN"
    assert not cred_file.exists()  # file deleted
    dot_env = (tmp_path / ".env").read_text()
    assert "MY_TOKEN=my_secret_token" in dot_env


def test_import_credential_file_not_found():
    """Import returns error for non-existent file."""
    result = import_credential_file("/nonexistent/path", "KEY")
    assert result["stored"] is False
    assert "not found" in result["error"]


def test_import_credential_file_empty(tmp_path, monkeypatch):
    """Import returns error for empty file."""
    monkeypatch.setattr("carpenter.api.credentials.config.CONFIG",
                        {"base_dir": str(tmp_path)})

    cred_file = tmp_path / "empty.txt"
    cred_file.write_text("  \n")

    result = import_credential_file(str(cred_file), "KEY")
    assert result["stored"] is False
    assert "empty" in result["error"]


# ---------------------------------------------------------------------------
# Credential request lifecycle (provide)
# ---------------------------------------------------------------------------


def test_credential_request_lifecycle(tmp_path, monkeypatch):
    """Full flow: create request -> provide -> fulfilled."""
    monkeypatch.setattr("carpenter.api.credentials.config.CONFIG",
                        {"base_dir": str(tmp_path)})

    # Create request
    result = create_credential_request(key="API_KEY", label="API Key")
    request_id = result["request_id"]

    req = get_credential_request(request_id)
    assert req["fulfilled"] is False

    # Simulate providing the credential
    _update_dot_env("API_KEY", "the_value")
    req["fulfilled"] = True

    assert req["fulfilled"] is True
    dot_env = (tmp_path / ".env").read_text()
    assert "API_KEY=the_value" in dot_env


def test_credential_request_not_found():
    """Get returns None for unknown request ID."""
    assert get_credential_request("nonexistent-uuid") is None
