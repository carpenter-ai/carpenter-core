"""Secure credential intake API endpoint.

Provides one-time UUID-gated forms for users to submit credentials
(API tokens, secrets) without exposing them in chat. The credential
is stored directly in {base_dir}/.env and the config is reloaded.

Pattern mirrors review.py's one-time UUID links.
"""

import logging
import uuid
from pathlib import Path

import httpx
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
import attrs
import cattrs

from .. import config
from .static import read_asset, load_template

logger = logging.getLogger(__name__)

# In-memory store: request_id -> {key, label, description, fulfilled, value_env_var}
_credential_requests: dict[str, dict] = {}


@attrs.define
class CredentialProvide:
    value: str


def create_credential_request(
    key: str,
    label: str = "",
    description: str = "",
) -> dict:
    """Create a one-time credential request link.

    Args:
        key: The env var name (e.g. "FORGEJO_TOKEN", "ANTHROPIC_API_KEY").
        label: Human-readable label for the form.
        description: Explanation of what the credential is used for.

    Returns:
        Dict with request_id and url.
    """
    request_id = str(uuid.uuid4())
    _credential_requests[request_id] = {
        "key": key,
        "label": label or key,
        "description": description,
        "fulfilled": False,
    }
    return {
        "request_id": request_id,
        "url": f"/api/credentials/{request_id}",
    }


def _update_dot_env(key: str, value: str) -> None:
    """Write a credential to {base_dir}/.env and reload config."""
    import re

    base_dir = config.CONFIG.get("base_dir", "")
    if not base_dir:
        raise RuntimeError("base_dir not configured")

    dot_env_path = Path(base_dir) / ".env"
    existing_lines: list[str] = []
    if dot_env_path.is_file():
        existing_lines = dot_env_path.read_text().splitlines()

    new_lines: list[str] = []
    updated = False
    for line in existing_lines:
        if re.match(rf'^{re.escape(key)}\s*=', line.strip()):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")  # blank separator
        new_lines.append(f"{key}={value}")

    dot_env_path.parent.mkdir(parents=True, exist_ok=True)
    dot_env_path.write_text("\n".join(new_lines) + "\n")

    # Reload config so the new credential is immediately available
    config.reload_config()


def verify_credential(key: str) -> dict:
    """Verify a stored credential by testing it.

    For git_token/forgejo_token: calls GET {git_server_url}/api/v1/user.
    For other keys: checks that the value is non-empty.

    Never returns the credential value.
    """
    config_key = config._CREDENTIAL_MAP.get(key, key)
    value = config.CONFIG.get(config_key, "")

    if not value:
        return {"valid": False, "reason": "credential not set"}

    if key in ("GIT_TOKEN", "FORGEJO_TOKEN") or config_key in ("git_token", "forgejo_token"):
        server_url = config.CONFIG.get("git_server_url", "") or config.CONFIG.get("forgejo_url", "")
        if not server_url:
            return {"valid": False, "reason": "git_server_url not configured"}

        url = server_url.rstrip("/") + "/api/v1/user"
        try:
            response = httpx.get(
                url,
                headers={"Authorization": f"token {value}"},
                timeout=15.0,
            )
            if response.status_code == 200:
                data = response.json()
                return {
                    "valid": True,
                    "username": data.get("login", ""),
                }
            else:
                return {"valid": False, "reason": f"HTTP {response.status_code}"}
        except (OSError, ValueError, KeyError) as e:
            return {"valid": False, "reason": str(e)}

    # Generic: non-empty means valid
    return {"valid": True}


def import_credential_file(path: str, key: str) -> dict:
    """Read a credential from a file, store in .env, delete the file.

    For non-TLS environments where the credential link isn't secure.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return {"stored": False, "error": f"file not found: {path}"}

    value = file_path.read_text().strip()
    if not value:
        return {"stored": False, "error": "file is empty"}

    try:
        _update_dot_env(key, value)
        file_path.unlink()
        return {"stored": True, "key": key}
    except (OSError, ValueError) as e:
        return {"stored": False, "error": str(e)}


async def list_pending_credentials(request: Request):
    """List unfulfilled credential requests.

    Returns a JSON array of pending requests (request_id, key, label,
    description).  Used by acceptance test harnesses and external
    processes that need to discover credential requests created by the
    agent.
    """
    pending = [
        {"request_id": rid, "key": req["key"], "label": req["label"],
         "description": req.get("description", "")}
        for rid, req in _credential_requests.items()
        if not req["fulfilled"]
    ]
    return JSONResponse(content={"pending": pending})


async def credential_form(request: Request):
    """Render a secure HTML form for credential submission."""
    request_id = request.path_params["request_id"]
    req = _credential_requests.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Credential request not found")
    if req["fulfilled"]:
        raise HTTPException(status_code=410, detail="Credential already provided")

    label = req["label"]
    description = req.get("description", "")
    desc_html = f'<p class="desc">{description}</p>' if description else ""

    css = read_asset("credentials.css")
    js = read_asset("credentials.js").replace(
        "__PROVIDE_URL__", f"/api/credentials/{request_id}/provide"
    )

    html = load_template(
        "credentials.html",
        label=label,
        css=css,
        js=js,
        desc_html=desc_html,
    )
    return HTMLResponse(content=html)


async def provide_credential(request: Request):
    """Receive the credential value, store in .env, reload config."""
    request_id = request.path_params["request_id"]
    body = cattrs.structure(await request.json(), CredentialProvide)

    req = _credential_requests.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Credential request not found")
    if req["fulfilled"]:
        raise HTTPException(status_code=410, detail="Credential already provided")

    key = req["key"]
    value = body.value.strip()

    if not value:
        raise HTTPException(status_code=400, detail="Credential value cannot be empty")

    try:
        _update_dot_env(key, value)
        req["fulfilled"] = True
        logger.info("Credential %s provided via request %s", key, request_id[:8])
        return JSONResponse(content={"stored": True, "key": key})
    except (OSError, ValueError) as e:
        logger.exception("Failed to store credential %s", key)
        raise HTTPException(status_code=500, detail=str(e))


async def reload_config_endpoint(request: Request):
    """Reload config from disk (config.yaml + .env).

    Used by acceptance test harnesses after writing to .env or config.yaml
    outside the normal credential flow.  Unprotected (same as credential
    endpoints) since it is read-only from a security perspective — it
    re-reads existing files, it does not accept new values.
    """
    config.reload_config()
    logger.info("Config reloaded via /api/credentials/reload-config")
    return JSONResponse(content={"reloaded": True})


def get_credential_request(request_id: str) -> dict | None:
    """Get credential request data by ID (for testing)."""
    return _credential_requests.get(request_id)


def clear_credential_requests():
    """Clear all credential requests (for testing)."""
    _credential_requests.clear()


routes = [
    Route("/api/credentials/pending", list_pending_credentials, methods=["GET"]),
    Route("/api/credentials/reload-config", reload_config_endpoint, methods=["POST"]),
    Route("/api/credentials/{request_id}/provide", provide_credential, methods=["POST"]),
    Route("/api/credentials/{request_id}", credential_form, methods=["GET"]),
]
