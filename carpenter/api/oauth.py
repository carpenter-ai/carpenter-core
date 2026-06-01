"""Generic OAuth 2.0 authorization-code callback flow.

This module is the platform-side complement to the
``tool_backends/credentials.py`` one-time-link flow.  It handles the
OAuth round-trip for any capability package that needs user-consent
credentials (Gmail, Calendar, Drive, Slack, ...).

Flow shape::

    1. Caller (a chat tool, or the package installer) calls
       :func:`start_flow` with provider details, scopes, and an
       ``env_key_prefix``.  Receives back a ``flow_id`` and an
       ``authorize_url``.
    2. The platform surfaces ``authorize_url`` to the user — typically
       via a chat message like *"Click here to authorize..."*.
    3. User clicks the link, lands on the provider's consent page,
       grants the requested scopes.
    4. Provider redirects back to ``GET /api/oauth/callback/{flow_id}?
       code=...&state=...``.
    5. The callback handler verifies ``state`` matches the stored
       opaque token, exchanges ``code`` for tokens at the provider's
       token endpoint, and writes them to the platform ``.env`` under
       ``<env_key_prefix>_ACCESS_TOKEN`` / ``_REFRESH_TOKEN`` /
       ``_TOKEN_EXPIRES_AT``.

The same code path serves all OAuth providers — only the URLs and
scope strings differ.  See ``docs/capability-packages-howto.md`` §5
for the package-author view.

Trust boundary: this is platform code (trusted core).  The
``_oauth_flows`` registry is process-local and ephemeral, mirroring
the existing ``_credential_requests`` pattern.

NOT covered here (out of scope for this PR):

- PKCE (Authorization Code with PKCE).  The current providers we
  target (Google, Slack) accept the plain confidential-client flow
  with a stored ``client_secret``.  PKCE is a follow-up.
- Implicit/device-code flows.
- Multi-account (per-account env-key suffixing).  Phase 1 of the
  email package is single-account; revisit when a second account
  matters.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .. import config
from .credentials import _update_dot_env

logger = logging.getLogger(__name__)


# Default flow expiry — the user has this long between clicking the
# authorize link and the provider redirecting back.  Long enough for a
# slow consent screen, short enough that abandoned flows expire.
DEFAULT_FLOW_TTL_SECONDS = 15 * 60

# Default token-exchange timeout (seconds).  Keep it tight — the token
# endpoint is normally <500ms; slow responses indicate provider issues
# and we'd rather surface them as errors than block the request.
DEFAULT_TOKEN_TIMEOUT_SECONDS = 15.0


# Query-parameter names that the platform owns when building the
# authorize URL.  Operator-supplied ``extra_authorize_params`` is NOT
# allowed to override these — doing so would silently disable the
# state-token CSRF defense, redirect users to attacker-controlled URIs,
# or alter the OAuth flow shape.  Reject loudly at flow-start instead.
_RESERVED_AUTHORIZE_PARAMS = frozenset({
    "client_id",
    "redirect_uri",
    "response_type",
    "scope",
    "state",
})


@dataclass
class OAuthFlow:
    """In-memory state for a single in-flight OAuth flow.

    Attributes are intentionally narrow: only what the callback handler
    needs to complete the round-trip.  ``client_secret`` is stored
    in-memory for the duration of the flow and discarded after the
    token exchange — it is *not* embedded in the redirect URL.
    """

    flow_id: str
    state: str
    provider: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    redirect_uri: str
    env_key_prefix: str
    package_name: str
    expires_at: float
    fulfilled: bool = False
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    error: str | None = None


# Process-local flow registry.  Same lifecycle pattern as
# ``_credential_requests`` in :mod:`carpenter.api.credentials`.
_oauth_flows: dict[str, OAuthFlow] = {}


def _now() -> float:
    """Indirection so tests can monkeypatch the clock."""
    return time.time()


def _redact_secret(text: str, secret: str) -> str:
    """Replace every occurrence of ``secret`` in ``text`` with ``***``.

    No-ops if ``secret`` is empty or short enough that masking would be
    meaningless (and could hint at the secret's length).  We keep this
    deliberately simple — exact-substring replacement, no regex.
    Providers may echo the refresh token in error bodies; this scrubs
    it before the body is returned to the caller (and likely logged).
    """
    if not secret or len(secret) < 4:
        return text
    return text.replace(secret, "***")


def _gc_expired_flows() -> None:
    """Drop fulfilled or expired flows to keep the registry bounded."""
    now = _now()
    stale = [
        fid for fid, flow in _oauth_flows.items()
        if flow.fulfilled or flow.expires_at < now
    ]
    for fid in stale:
        _oauth_flows.pop(fid, None)


def start_flow(
    *,
    provider: str,
    client_id: str,
    client_secret: str,
    authorize_url: str,
    token_url: str,
    scopes: list[str] | tuple[str, ...],
    env_key_prefix: str,
    package_name: str = "",
    redirect_uri: str | None = None,
    extra_authorize_params: dict[str, str] | None = None,
    ttl_seconds: int = DEFAULT_FLOW_TTL_SECONDS,
) -> dict[str, str]:
    """Begin an OAuth authorization-code flow.

    Args:
        provider: Human-readable provider name (e.g. ``"google"``).
            Used only for logging and error messages.
        client_id: OAuth client identifier (operator-supplied, normally
            already in ``.env``).
        client_secret: OAuth client secret (operator-supplied).  Held
            only in process memory; never written to disk by the
            callback handler.
        authorize_url: Provider's authorization endpoint URL.
        token_url: Provider's token endpoint URL.
        scopes: List of OAuth scope strings.
        env_key_prefix: Prefix for the env vars the resulting tokens
            will be written under.  Convention: ``UPPER_SNAKE``, e.g.
            ``GMAIL_OAUTH``.  Will produce ``GMAIL_OAUTH_ACCESS_TOKEN``,
            ``GMAIL_OAUTH_REFRESH_TOKEN``,
            ``GMAIL_OAUTH_TOKEN_EXPIRES_AT``.
        package_name: Optional package identifier for logging and the
            per-package status helper.
        redirect_uri: The redirect URI registered with the provider.
            Defaults to ``{public_base_url}/api/oauth/callback/{flow_id}``
            when the platform's ``public_base_url`` config is set.
        extra_authorize_params: Extra query params to append to the
            authorize URL (e.g. Google's ``access_type=offline`` and
            ``prompt=consent`` to force refresh-token issuance).
        ttl_seconds: How long the flow remains valid before expiry.

    Returns:
        Dict with ``flow_id`` and ``authorize_url`` (the URL the user
        should open in their browser).

    Raises:
        ValueError: If required arguments are empty or malformed.
    """
    _gc_expired_flows()

    if not provider.strip():
        raise ValueError("provider is required")
    if not client_id.strip():
        raise ValueError("client_id is required")
    if not client_secret.strip():
        raise ValueError("client_secret is required")
    if not authorize_url.strip():
        raise ValueError("authorize_url is required")
    if not token_url.strip():
        raise ValueError("token_url is required")
    if not env_key_prefix.strip():
        raise ValueError("env_key_prefix is required")
    if not scopes:
        raise ValueError("at least one scope is required")

    if extra_authorize_params:
        reserved = sorted(
            set(extra_authorize_params) & _RESERVED_AUTHORIZE_PARAMS,
        )
        if reserved:
            raise ValueError(
                "extra_authorize_params cannot override reserved keys: "
                f"{reserved}.  These are owned by the platform "
                "(state, redirect_uri, client_id, response_type, scope) "
                "and overriding them would disable CSRF protection or "
                "alter the OAuth flow shape.",
            )

    flow_id = secrets.token_urlsafe(16)
    state = secrets.token_urlsafe(24)

    if redirect_uri is None:
        public_base = config.CONFIG.get("public_base_url", "").rstrip("/")
        if not public_base:
            raise ValueError(
                "redirect_uri not provided and config.public_base_url is "
                "not set; cannot derive callback URL"
            )
        redirect_uri = f"{public_base}/api/oauth/callback/{flow_id}"

    flow = OAuthFlow(
        flow_id=flow_id,
        state=state,
        provider=provider,
        client_id=client_id,
        client_secret=client_secret,
        authorize_url=authorize_url,
        token_url=token_url,
        scopes=tuple(scopes),
        redirect_uri=redirect_uri,
        env_key_prefix=env_key_prefix,
        package_name=package_name,
        expires_at=_now() + ttl_seconds,
        extra_authorize_params=dict(extra_authorize_params or {}),
    )
    _oauth_flows[flow_id] = flow

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
    }
    params.update(flow.extra_authorize_params)
    full_authorize_url = f"{authorize_url}?{urlencode(params)}"

    logger.info(
        "oauth flow started: provider=%s package=%s prefix=%s flow=%s",
        provider, package_name or "-", env_key_prefix, flow_id[:8],
    )

    return {
        "flow_id": flow_id,
        "authorize_url": full_authorize_url,
        "redirect_uri": redirect_uri,
    }


def _exchange_code_for_tokens(flow: OAuthFlow, code: str) -> dict[str, Any]:
    """POST to the provider's token endpoint and return the JSON body.

    Raises:
        HTTPException: On any non-2xx response.  The detail string
            includes the provider's error body (truncated) — this is
            user-visible on the callback page.
    """
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": flow.client_id,
        "client_secret": flow.client_secret,
        "redirect_uri": flow.redirect_uri,
    }
    try:
        resp = httpx.post(
            flow.token_url,
            data=body,
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TOKEN_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.exception(
            "oauth token exchange transport error: provider=%s flow=%s",
            flow.provider, flow.flow_id[:8],
        )
        raise HTTPException(
            status_code=502,
            detail=f"OAuth token exchange transport error: {exc}",
        ) from exc

    if resp.status_code >= 400:
        snippet = resp.text[:500]
        logger.warning(
            "oauth token exchange failed: provider=%s flow=%s status=%s body=%r",
            flow.provider, flow.flow_id[:8], resp.status_code, snippet,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"OAuth token exchange failed: provider={flow.provider} "
                f"status={resp.status_code} body={snippet}"
            ),
        )

    try:
        return resp.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"OAuth token endpoint returned non-JSON: {exc}",
        ) from exc


def _persist_tokens(
    env_key_prefix: str, token_response: dict[str, Any],
) -> dict[str, str]:
    """Write access/refresh/expires-at to the platform ``.env``.

    Returns the dict of env var names that were written (for logging).
    Missing fields are skipped — providers vary in what they return
    (e.g. some don't issue refresh tokens at all).
    """
    written: dict[str, str] = {}

    access_token = token_response.get("access_token")
    if access_token:
        key = f"{env_key_prefix}_ACCESS_TOKEN"
        _update_dot_env(key, str(access_token))
        written[key] = "***"

    refresh_token = token_response.get("refresh_token")
    if refresh_token:
        key = f"{env_key_prefix}_REFRESH_TOKEN"
        _update_dot_env(key, str(refresh_token))
        written[key] = "***"

    expires_in = token_response.get("expires_in")
    if expires_in is not None:
        try:
            expires_at = int(_now() + float(expires_in))
        except (TypeError, ValueError):
            expires_at = None
        if expires_at is not None:
            key = f"{env_key_prefix}_TOKEN_EXPIRES_AT"
            _update_dot_env(key, str(expires_at))
            written[key] = str(expires_at)

    token_type = token_response.get("token_type")
    if token_type:
        key = f"{env_key_prefix}_TOKEN_TYPE"
        _update_dot_env(key, str(token_type))
        written[key] = str(token_type)

    return written


async def oauth_callback(request: Request):
    """Handle ``GET /api/oauth/callback/{flow_id}``.

    Validates the ``state`` query param against the stored flow,
    exchanges the auth code for tokens, persists them under the
    flow's ``env_key_prefix``, and renders a confirmation page.
    """
    flow_id = request.path_params["flow_id"]
    flow = _oauth_flows.get(flow_id)

    if flow is None:
        raise HTTPException(status_code=404, detail="OAuth flow not found")
    if flow.fulfilled:
        raise HTTPException(
            status_code=410, detail="OAuth flow already completed",
        )
    if flow.expires_at < _now():
        flow.error = "expired"
        raise HTTPException(status_code=410, detail="OAuth flow expired")

    # Provider-supplied error (e.g. user denied consent).
    err = request.query_params.get("error", "")
    if err:
        err_desc = request.query_params.get("error_description", "")
        flow.error = f"{err}: {err_desc}".strip(": ")
        logger.info(
            "oauth flow declined: flow=%s provider=%s error=%s",
            flow_id[:8], flow.provider, flow.error,
        )
        return HTMLResponse(
            content=_render_failure_page(flow.provider, flow.error),
            status_code=400,
        )

    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")

    if not code:
        raise HTTPException(
            status_code=400, detail="OAuth callback missing 'code'",
        )
    if not state:
        raise HTTPException(
            status_code=400, detail="OAuth callback missing 'state'",
        )

    # Constant-time-ish state comparison.  ``secrets.compare_digest``
    # avoids leaking timing info on a guessable state.
    if not secrets.compare_digest(state, flow.state):
        logger.warning(
            "oauth flow state mismatch: flow=%s provider=%s",
            flow_id[:8], flow.provider,
        )
        raise HTTPException(
            status_code=400, detail="OAuth callback state mismatch",
        )

    token_response = _exchange_code_for_tokens(flow, code)
    written = _persist_tokens(flow.env_key_prefix, token_response)

    access_key = f"{flow.env_key_prefix}_ACCESS_TOKEN"
    if access_key not in written:
        logger.warning(
            "oauth flow exchanged but no access token written: flow=%s "
            "provider=%s response_keys=%s",
            flow_id[:8], flow.provider, sorted(token_response.keys()),
        )
        raise HTTPException(
            status_code=502,
            detail="OAuth token response missing access_token",
        )

    flow.fulfilled = True
    # Discard the secret after a successful exchange — it lives only
    # for the duration of the flow.
    flow.client_secret = ""
    logger.info(
        "oauth flow fulfilled: flow=%s provider=%s package=%s wrote=%s",
        flow_id[:8], flow.provider, flow.package_name or "-",
        sorted(written.keys()),
    )

    return HTMLResponse(
        content=_render_success_page(flow.provider, flow.package_name),
    )


def refresh_token(env_key_prefix: str) -> dict[str, Any]:
    """Refresh the access token for a previously-completed flow.

    Reads the stored refresh token from ``.env`` (via
    :data:`config.CONFIG`), POSTs to the provider's token endpoint
    using the per-flow ``token_url`` saved alongside the prefix, and
    writes back the new ``ACCESS_TOKEN`` / ``TOKEN_EXPIRES_AT`` (and
    ``REFRESH_TOKEN`` if the provider rotated it).

    The caller must supply ``client_id``, ``client_secret``, and
    ``token_url`` via the platform config — this helper does NOT keep
    them in memory between flows (the in-memory ``OAuthFlow`` is
    discarded after :func:`oauth_callback` completes).

    Args:
        env_key_prefix: The prefix used at flow start (e.g.
            ``"GMAIL_OAUTH"``).  This function reads
            ``<prefix>_REFRESH_TOKEN``,
            ``<prefix>_CLIENT_ID``,
            ``<prefix>_CLIENT_SECRET``,
            ``<prefix>_TOKEN_URL`` from config.

    Returns:
        Dict with ``ok: True`` on success, plus ``access_token`` and
        ``expires_at``.  Returns ``ok: False`` and ``error`` on
        failure (revoked refresh token, missing config, ...).
    """
    cfg = config.CONFIG

    def _get(name: str) -> str:
        # Try both the env-var-style key and the lowercased alias the
        # config loader maps known credentials to.
        return str(cfg.get(name, "") or cfg.get(name.lower(), "") or "")

    refresh = _get(f"{env_key_prefix}_REFRESH_TOKEN")
    client_id = _get(f"{env_key_prefix}_CLIENT_ID")
    client_secret = _get(f"{env_key_prefix}_CLIENT_SECRET")
    token_url = _get(f"{env_key_prefix}_TOKEN_URL")

    missing = [
        name for name, val in (
            ("REFRESH_TOKEN", refresh),
            ("CLIENT_ID", client_id),
            ("CLIENT_SECRET", client_secret),
            ("TOKEN_URL", token_url),
        ) if not val
    ]
    if missing:
        return {
            "ok": False,
            "error": f"missing config: {env_key_prefix}_{{{','.join(missing)}}}",
        }

    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    try:
        resp = httpx.post(
            token_url,
            data=body,
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TOKEN_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"transport error: {exc}"}

    if resp.status_code >= 400:
        # Some providers echo the refresh token back in error responses
        # (e.g. as part of a grant-summary).  Redact it before surfacing
        # the body — the caller may log this string and we don't want
        # the secret in logs.
        body_snippet = _redact_secret(resp.text[:500], refresh)
        return {
            "ok": False,
            "error": f"refresh failed: status={resp.status_code} "
                     f"body={body_snippet}",
        }

    try:
        token_response = resp.json()
    except ValueError as exc:
        return {"ok": False, "error": f"non-JSON response: {exc}"}

    written = _persist_tokens(env_key_prefix, token_response)
    if f"{env_key_prefix}_ACCESS_TOKEN" not in written:
        return {
            "ok": False,
            "error": "refresh response missing access_token",
        }

    return {
        "ok": True,
        "access_token_written": True,
        "refresh_token_rotated": (
            f"{env_key_prefix}_REFRESH_TOKEN" in written
        ),
        "expires_at_written": (
            f"{env_key_prefix}_TOKEN_EXPIRES_AT" in written
        ),
    }


def get_flow(flow_id: str) -> OAuthFlow | None:
    """Get an in-flight flow by ID (testing / inspection)."""
    return _oauth_flows.get(flow_id)


def clear_flows() -> None:
    """Clear all in-flight flows (for testing)."""
    _oauth_flows.clear()


def package_oauth_status(env_key_prefix: str) -> dict[str, bool]:
    """Report whether the env vars for a given prefix are populated.

    Used by the package installer / chat agent to decide whether to
    surface an authorize-link to the user.
    """
    cfg = config.CONFIG
    return {
        "access_token": bool(
            cfg.get(f"{env_key_prefix}_ACCESS_TOKEN")
            or cfg.get(f"{env_key_prefix}_ACCESS_TOKEN".lower()),
        ),
        "refresh_token": bool(
            cfg.get(f"{env_key_prefix}_REFRESH_TOKEN")
            or cfg.get(f"{env_key_prefix}_REFRESH_TOKEN".lower()),
        ),
        "client_id": bool(
            cfg.get(f"{env_key_prefix}_CLIENT_ID")
            or cfg.get(f"{env_key_prefix}_CLIENT_ID".lower()),
        ),
        "client_secret": bool(
            cfg.get(f"{env_key_prefix}_CLIENT_SECRET")
            or cfg.get(f"{env_key_prefix}_CLIENT_SECRET".lower()),
        ),
    }


# ---------------------------------------------------------------------------
# Minimal HTML response pages (no template engine — keeps this file
# self-contained and avoids cross-coupling with api/static).
# ---------------------------------------------------------------------------


def _render_success_page(provider: str, package_name: str) -> str:
    pkg = package_name or "the package"
    return (
        "<!doctype html><html><head><title>Authorization complete</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:42rem;"
        "margin:4rem auto;padding:0 1rem;color:#222}h1{color:#2c7a3e}</style>"
        "</head><body>"
        f"<h1>Authorization complete</h1>"
        f"<p>{provider} authorization for <strong>{pkg}</strong> is now "
        f"active.  You can close this tab and return to the chat.</p>"
        "</body></html>"
    )


def _render_failure_page(provider: str, error: str) -> str:
    return (
        "<!doctype html><html><head><title>Authorization failed</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:42rem;"
        "margin:4rem auto;padding:0 1rem;color:#222}h1{color:#a23030}"
        "pre{background:#f4f4f4;padding:.6rem;border-radius:.3rem;"
        "white-space:pre-wrap}</style></head><body>"
        f"<h1>Authorization failed</h1>"
        f"<p>{provider} declined or could not complete the authorization.</p>"
        f"<pre>{error}</pre>"
        "<p>You can close this tab and ask the chat agent to start a "
        "new authorization flow.</p></body></html>"
    )


routes = [
    Route(
        "/api/oauth/callback/{flow_id}",
        oauth_callback,
        methods=["GET"],
    ),
]
