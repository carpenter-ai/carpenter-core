"""Bearer token authentication middleware for Carpenter.

Gates protected endpoints when ui_token is configured.  Unprotected
endpoints (callbacks, webhooks, review detail pages) pass through.
"""

import hmac
from urllib.parse import urlparse, parse_qs

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import config

# Prefixes / paths that are NOT gated by the UI token
_OPEN_PREFIXES = (
    "/api/callbacks/",
    "/api/webhooks/",
    "/api/credentials/",
    "/hooks/",
)


def is_protected(path: str, method: str) -> bool:
    """Return True if the path+method combination requires a UI token."""
    for prefix in _OPEN_PREFIXES:
        if path.startswith(prefix):
            return False

    # Review detail and decide endpoints use one-time UUID auth
    # e.g. /api/review/550e8400-... and /api/review/550e8400-.../decide
    if path.startswith("/api/review/") and path != "/api/review/create":
        return False

    return True


def extract_token(request: Request) -> str:
    """Extract token from query param or Authorization header."""
    # Query param takes precedence
    token = request.query_params.get("token", "")
    if token:
        return token

    # Fall back to Authorization: Bearer <token>
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()

    return ""


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Require a valid UI token for protected endpoints.

    If ``ui_token`` in config is empty, all requests pass through.
    """

    async def dispatch(self, request: Request, call_next):
        ui_token = config.CONFIG.get("ui_token", "")

        # No token configured — open access
        if not ui_token:
            return await call_next(request)

        path = request.url.path
        method = request.method

        if not is_protected(path, method):
            return await call_next(request)

        provided = extract_token(request)
        if not provided or not hmac.compare_digest(provided, ui_token):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
            )

        return await call_next(request)
