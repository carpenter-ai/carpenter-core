"""Tests for carpenter.api.oauth — generic OAuth-callback flow."""

from unittest.mock import MagicMock, patch

import httpx
import pytest
from starlette.testclient import TestClient

from carpenter.api import oauth
from carpenter.api.http import create_app


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def setup_function():
    """Reset module-level state between tests."""
    oauth.clear_flows()


@pytest.fixture
def env_dir(tmp_path, monkeypatch):
    """Point config.CONFIG at a tmp ``.env`` and stub reload_config out.

    The OAuth module writes via ``credentials._update_dot_env``, which
    expects ``base_dir`` and reloads config.  We override CONFIG with a
    plain dict so we can inspect what got written without invoking the
    full config-reload pipeline.
    """
    cfg: dict = {"base_dir": str(tmp_path), "public_base_url": "https://carp.example.com"}

    monkeypatch.setattr("carpenter.api.credentials.config.CONFIG", cfg)
    monkeypatch.setattr("carpenter.api.oauth.config.CONFIG", cfg)
    monkeypatch.setattr(
        "carpenter.api.credentials.config.reload_config", lambda: None,
    )
    return tmp_path, cfg


def _start_basic_flow(env_key_prefix="GMAIL_OAUTH"):
    return oauth.start_flow(
        provider="google",
        client_id="cid-123",
        client_secret="csec-456",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        env_key_prefix=env_key_prefix,
        package_name="carpenter-gmail",
        extra_authorize_params={"access_type": "offline", "prompt": "consent"},
    )


# ---------------------------------------------------------------------------
# start_flow
# ---------------------------------------------------------------------------


def test_start_flow_returns_url_and_id(env_dir):
    result = _start_basic_flow()

    assert "flow_id" in result
    assert "authorize_url" in result
    assert result["authorize_url"].startswith(
        "https://accounts.google.com/o/oauth2/v2/auth?",
    )

    flow = oauth.get_flow(result["flow_id"])
    assert flow is not None
    assert flow.fulfilled is False
    assert flow.scopes == (
        "https://www.googleapis.com/auth/gmail.readonly",
    )

    # Authorize URL embeds the redirect_uri, state, and scopes.
    url = result["authorize_url"]
    assert "client_id=cid-123" in url
    assert "response_type=code" in url
    assert "state=" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url


def test_start_flow_state_and_id_are_unique(env_dir):
    a = _start_basic_flow()
    b = _start_basic_flow()
    assert a["flow_id"] != b["flow_id"]
    flow_a = oauth.get_flow(a["flow_id"])
    flow_b = oauth.get_flow(b["flow_id"])
    assert flow_a.state != flow_b.state


def test_start_flow_default_redirect_uri(env_dir):
    result = _start_basic_flow()
    flow = oauth.get_flow(result["flow_id"])
    assert flow.redirect_uri == (
        f"https://carp.example.com/api/oauth/callback/{result['flow_id']}"
    )


def test_start_flow_explicit_redirect_uri(env_dir):
    result = oauth.start_flow(
        provider="x",
        client_id="cid",
        client_secret="csec",
        authorize_url="https://x.example/auth",
        token_url="https://x.example/token",
        scopes=["read"],
        env_key_prefix="X_OAUTH",
        redirect_uri="https://other.example/cb",
    )
    flow = oauth.get_flow(result["flow_id"])
    assert flow.redirect_uri == "https://other.example/cb"


def test_start_flow_validation(env_dir):
    with pytest.raises(ValueError):
        oauth.start_flow(
            provider="", client_id="c", client_secret="s",
            authorize_url="https://a", token_url="https://t",
            scopes=["x"], env_key_prefix="P",
        )
    with pytest.raises(ValueError):
        oauth.start_flow(
            provider="p", client_id="c", client_secret="s",
            authorize_url="https://a", token_url="https://t",
            scopes=[], env_key_prefix="P",
        )
    with pytest.raises(ValueError):
        oauth.start_flow(
            provider="p", client_id="c", client_secret="s",
            authorize_url="https://a", token_url="https://t",
            scopes=["x"], env_key_prefix="",
        )


@pytest.mark.parametrize(
    "reserved_key",
    ["state", "redirect_uri", "client_id", "response_type", "scope"],
)
def test_start_flow_rejects_reserved_extra_authorize_params(
    env_dir, reserved_key,
):
    """Operator-supplied extra_authorize_params cannot override platform-
    owned params.  Overriding ``state`` would silently disable the
    CSRF defense; overriding ``redirect_uri`` could redirect the
    consent code to an attacker.  Reject loudly at flow-start.
    """
    with pytest.raises(ValueError, match="reserved"):
        oauth.start_flow(
            provider="google",
            client_id="cid",
            client_secret="csec",
            authorize_url="https://a.example/auth",
            token_url="https://a.example/token",
            scopes=["x"],
            env_key_prefix="P",
            extra_authorize_params={reserved_key: "attacker-value"},
        )


def test_start_flow_allows_non_reserved_extra_params(env_dir):
    """Non-reserved keys (like Google's access_type/prompt) still work."""
    result = oauth.start_flow(
        provider="google",
        client_id="cid",
        client_secret="csec",
        authorize_url="https://a.example/auth",
        token_url="https://a.example/token",
        scopes=["x"],
        env_key_prefix="P",
        extra_authorize_params={
            "access_type": "offline",
            "prompt": "consent",
            "login_hint": "user@example.com",
        },
    )
    assert "access_type=offline" in result["authorize_url"]
    assert "prompt=consent" in result["authorize_url"]


def test_start_flow_no_public_base_url_raises(tmp_path, monkeypatch):
    cfg: dict = {"base_dir": str(tmp_path)}
    monkeypatch.setattr("carpenter.api.oauth.config.CONFIG", cfg)
    with pytest.raises(ValueError, match="public_base_url"):
        oauth.start_flow(
            provider="google", client_id="c", client_secret="s",
            authorize_url="https://a", token_url="https://t",
            scopes=["x"], env_key_prefix="P",
        )


# ---------------------------------------------------------------------------
# Callback — happy path
# ---------------------------------------------------------------------------


def _mock_token_response(status=200, body=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = body or {}
    resp.text = str(body or "")
    return resp


def test_callback_happy_path_writes_env(env_dir):
    tmp_path, _cfg = env_dir
    started = _start_basic_flow()
    flow = oauth.get_flow(started["flow_id"])

    token_body = {
        "access_token": "ya29.access",
        "refresh_token": "1//refresh",
        "expires_in": 3599,
        "token_type": "Bearer",
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
    }

    client = TestClient(create_app())
    with patch("carpenter.api.oauth.httpx.post") as mock_post:
        mock_post.return_value = _mock_token_response(200, token_body)
        resp = client.get(
            f"/api/oauth/callback/{started['flow_id']}",
            params={"code": "auth-code-xyz", "state": flow.state},
        )

    assert resp.status_code == 200
    assert "Authorization complete" in resp.text

    # Tokens written to .env
    dot_env = (tmp_path / ".env").read_text()
    assert "GMAIL_OAUTH_ACCESS_TOKEN=ya29.access" in dot_env
    assert "GMAIL_OAUTH_REFRESH_TOKEN=1//refresh" in dot_env
    assert "GMAIL_OAUTH_TOKEN_TYPE=Bearer" in dot_env
    assert "GMAIL_OAUTH_TOKEN_EXPIRES_AT=" in dot_env

    # Flow marked fulfilled, secret discarded
    assert flow.fulfilled is True
    assert flow.client_secret == ""

    # Token endpoint received the right form data
    call = mock_post.call_args
    assert call.args[0] == "https://oauth2.googleapis.com/token"
    assert call.kwargs["data"]["grant_type"] == "authorization_code"
    assert call.kwargs["data"]["code"] == "auth-code-xyz"
    assert call.kwargs["data"]["client_id"] == "cid-123"
    assert call.kwargs["data"]["client_secret"] == "csec-456"


def test_callback_state_mismatch_rejected(env_dir):
    started = _start_basic_flow()
    client = TestClient(create_app())
    resp = client.get(
        f"/api/oauth/callback/{started['flow_id']}",
        params={"code": "x", "state": "wrong-state"},
    )
    assert resp.status_code == 400
    assert "state mismatch" in resp.json()["detail"]
    flow = oauth.get_flow(started["flow_id"])
    assert flow.fulfilled is False


def test_callback_missing_code(env_dir):
    started = _start_basic_flow()
    flow = oauth.get_flow(started["flow_id"])
    client = TestClient(create_app())
    resp = client.get(
        f"/api/oauth/callback/{started['flow_id']}",
        params={"state": flow.state},
    )
    assert resp.status_code == 400
    assert "code" in resp.json()["detail"]


def test_callback_unknown_flow_404(env_dir):
    client = TestClient(create_app())
    resp = client.get(
        "/api/oauth/callback/nonexistent",
        params={"code": "x", "state": "y"},
    )
    assert resp.status_code == 404


def test_callback_already_fulfilled_410(env_dir):
    started = _start_basic_flow()
    flow = oauth.get_flow(started["flow_id"])
    flow.fulfilled = True
    client = TestClient(create_app())
    resp = client.get(
        f"/api/oauth/callback/{started['flow_id']}",
        params={"code": "x", "state": flow.state},
    )
    assert resp.status_code == 410


def test_callback_expired_410(env_dir, monkeypatch):
    started = _start_basic_flow()
    flow = oauth.get_flow(started["flow_id"])
    # Simulate "now" being far past expiry.
    monkeypatch.setattr(
        "carpenter.api.oauth._now",
        lambda: flow.expires_at + 1.0,
    )
    client = TestClient(create_app())
    resp = client.get(
        f"/api/oauth/callback/{started['flow_id']}",
        params={"code": "x", "state": flow.state},
    )
    assert resp.status_code == 410
    assert "expired" in resp.json()["detail"].lower()


def test_callback_provider_error_renders_failure(env_dir):
    started = _start_basic_flow()
    client = TestClient(create_app())
    resp = client.get(
        f"/api/oauth/callback/{started['flow_id']}",
        params={"error": "access_denied",
                "error_description": "user said no"},
    )
    assert resp.status_code == 400
    assert "Authorization failed" in resp.text
    assert "access_denied" in resp.text


def test_callback_token_endpoint_5xx_returns_502(env_dir):
    started = _start_basic_flow()
    flow = oauth.get_flow(started["flow_id"])
    bad = MagicMock(spec=httpx.Response)
    bad.status_code = 500
    bad.text = "internal server error"

    client = TestClient(create_app())
    with patch("carpenter.api.oauth.httpx.post") as mock_post:
        mock_post.return_value = bad
        resp = client.get(
            f"/api/oauth/callback/{started['flow_id']}",
            params={"code": "x", "state": flow.state},
        )
    assert resp.status_code == 502


def test_callback_token_endpoint_transport_error_returns_502(env_dir):
    started = _start_basic_flow()
    flow = oauth.get_flow(started["flow_id"])
    client = TestClient(create_app())
    with patch("carpenter.api.oauth.httpx.post") as mock_post:
        mock_post.side_effect = httpx.ConnectError("boom")
        resp = client.get(
            f"/api/oauth/callback/{started['flow_id']}",
            params={"code": "x", "state": flow.state},
        )
    assert resp.status_code == 502
    assert "transport error" in resp.json()["detail"]


def test_callback_no_access_token_in_response_502(env_dir):
    started = _start_basic_flow()
    flow = oauth.get_flow(started["flow_id"])

    client = TestClient(create_app())
    with patch("carpenter.api.oauth.httpx.post") as mock_post:
        mock_post.return_value = _mock_token_response(
            200, {"refresh_token": "only-refresh"},
        )
        resp = client.get(
            f"/api/oauth/callback/{started['flow_id']}",
            params={"code": "x", "state": flow.state},
        )
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# refresh_token helper
# ---------------------------------------------------------------------------


def test_refresh_token_happy_path(env_dir):
    tmp_path, cfg = env_dir
    cfg.update({
        "GMAIL_OAUTH_REFRESH_TOKEN": "old-refresh",
        "GMAIL_OAUTH_CLIENT_ID": "cid",
        "GMAIL_OAUTH_CLIENT_SECRET": "csec",
        "GMAIL_OAUTH_TOKEN_URL": "https://oauth2.googleapis.com/token",
    })
    new_body = {
        "access_token": "new-access",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    with patch("carpenter.api.oauth.httpx.post") as mock_post:
        mock_post.return_value = _mock_token_response(200, new_body)
        result = oauth.refresh_token("GMAIL_OAUTH")

    assert result["ok"] is True
    assert result["access_token_written"] is True
    assert result["refresh_token_rotated"] is False  # no new refresh in body

    dot_env = (tmp_path / ".env").read_text()
    assert "GMAIL_OAUTH_ACCESS_TOKEN=new-access" in dot_env

    call = mock_post.call_args
    assert call.kwargs["data"]["grant_type"] == "refresh_token"
    assert call.kwargs["data"]["refresh_token"] == "old-refresh"


def test_refresh_token_rotated(env_dir):
    tmp_path, cfg = env_dir
    cfg.update({
        "GMAIL_OAUTH_REFRESH_TOKEN": "old-refresh",
        "GMAIL_OAUTH_CLIENT_ID": "cid",
        "GMAIL_OAUTH_CLIENT_SECRET": "csec",
        "GMAIL_OAUTH_TOKEN_URL": "https://oauth2.googleapis.com/token",
    })
    new_body = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_in": 3600,
    }
    with patch("carpenter.api.oauth.httpx.post") as mock_post:
        mock_post.return_value = _mock_token_response(200, new_body)
        result = oauth.refresh_token("GMAIL_OAUTH")
    assert result["ok"] is True
    assert result["refresh_token_rotated"] is True
    dot_env = (tmp_path / ".env").read_text()
    assert "GMAIL_OAUTH_REFRESH_TOKEN=new-refresh" in dot_env


def test_refresh_token_invalid_grant(env_dir):
    _, cfg = env_dir
    cfg.update({
        "GMAIL_OAUTH_REFRESH_TOKEN": "revoked",
        "GMAIL_OAUTH_CLIENT_ID": "cid",
        "GMAIL_OAUTH_CLIENT_SECRET": "csec",
        "GMAIL_OAUTH_TOKEN_URL": "https://oauth2.googleapis.com/token",
    })
    bad = MagicMock(spec=httpx.Response)
    bad.status_code = 400
    bad.text = '{"error":"invalid_grant"}'
    with patch("carpenter.api.oauth.httpx.post") as mock_post:
        mock_post.return_value = bad
        result = oauth.refresh_token("GMAIL_OAUTH")
    assert result["ok"] is False
    assert "invalid_grant" in result["error"]


def test_refresh_token_missing_config(env_dir):
    _, cfg = env_dir  # nothing populated
    result = oauth.refresh_token("GMAIL_OAUTH")
    assert result["ok"] is False
    assert "missing config" in result["error"]


def test_refresh_token_redacts_refresh_token_from_error_body(env_dir):
    """If the provider echoes the refresh token in an error body, the
    token must be scrubbed before the body is included in the returned
    error string.  Otherwise the secret leaks into caller logs.
    """
    _, cfg = env_dir
    cfg.update({
        "GMAIL_OAUTH_REFRESH_TOKEN": "1//super-secret-refresh-token-value",
        "GMAIL_OAUTH_CLIENT_ID": "cid",
        "GMAIL_OAUTH_CLIENT_SECRET": "csec",
        "GMAIL_OAUTH_TOKEN_URL": "https://oauth2.googleapis.com/token",
    })

    bad = MagicMock(spec=httpx.Response)
    bad.status_code = 400
    # Simulate a provider echoing the refresh token in its error body.
    bad.text = (
        '{"error":"invalid_grant","error_description":"refresh token '
        '1//super-secret-refresh-token-value has been revoked"}'
    )
    with patch("carpenter.api.oauth.httpx.post") as mock_post:
        mock_post.return_value = bad
        result = oauth.refresh_token("GMAIL_OAUTH")

    assert result["ok"] is False
    # The error must surface the provider's status, but NOT the secret.
    assert "1//super-secret-refresh-token-value" not in result["error"]
    assert "invalid_grant" in result["error"]
    assert "***" in result["error"]


def test_redact_secret_helper():
    """Direct unit test of _redact_secret behavior."""
    assert oauth._redact_secret("foo SECRET baz", "SECRET") == "foo *** baz"
    # Empty / short secrets are no-ops to avoid revealing length info.
    assert oauth._redact_secret("foo bar", "") == "foo bar"
    assert oauth._redact_secret("foo abc bar", "abc") == "foo abc bar"
    # Multi-occurrence: all replaced.
    assert oauth._redact_secret("xxxxx and xxxxx", "xxxxx") == "*** and ***"


# ---------------------------------------------------------------------------
# package_oauth_status
# ---------------------------------------------------------------------------


def test_package_oauth_status_all_missing(env_dir):
    status = oauth.package_oauth_status("GMAIL_OAUTH")
    assert status == {
        "access_token": False, "refresh_token": False,
        "client_id": False, "client_secret": False,
    }


def test_package_oauth_status_partial(env_dir):
    _, cfg = env_dir
    cfg["GMAIL_OAUTH_CLIENT_ID"] = "cid"
    cfg["GMAIL_OAUTH_ACCESS_TOKEN"] = "tok"
    status = oauth.package_oauth_status("GMAIL_OAUTH")
    assert status["client_id"] is True
    assert status["access_token"] is True
    assert status["client_secret"] is False
    assert status["refresh_token"] is False
