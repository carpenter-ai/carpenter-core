"""Web tool backend — handles HTTP requests from executors."""
import json
import logging
import os
from typing import Dict, Any
import httpx
from urllib.parse import urlparse

from .. import config
from ..core.resources import (
    create_resource,
    hash_file,
    link_arc_resource,
    resource_storage_path,
    set_resource_file_path,
    update_resource_content_stats,
)

logger = logging.getLogger(__name__)

# Defaults kept as module-level fallbacks; runtime values come from config.
_DEFAULT_WEB_REQUEST_TIMEOUT = 30.0
_DEFAULT_WEB_RESPONSE_MAX_CHARS = 10000
_DEFAULT_WEB_FETCH_MAX_BYTES = 1_000_000


def _web_request_default_timeout() -> float:
    """Return the default HTTP timeout for web tool requests (seconds)."""
    return config.get_config("web_request_default_timeout", _DEFAULT_WEB_REQUEST_TIMEOUT)


def _web_response_max_chars() -> int:
    """Return the max chars to return from web GET/POST responses."""
    return config.get_config("web_response_max_chars", _DEFAULT_WEB_RESPONSE_MAX_CHARS)


def _web_fetch_max_bytes() -> int:
    """Return the max bytes for webpage fetch content."""
    return config.get_config("web_fetch_max_bytes", _DEFAULT_WEB_FETCH_MAX_BYTES)


def handle_get(params: dict) -> dict:
    """HTTP GET request. Params: url, headers (opt), timeout (opt)."""
    url = params["url"]
    headers = params.get("headers", {})
    timeout = params.get("timeout", _web_request_default_timeout())

    try:
        response = httpx.get(url, headers=headers, timeout=timeout)
        max_chars = _web_response_max_chars()
        return {
            "status_code": response.status_code,
            "text": response.text[:max_chars],
            "headers": dict(response.headers),
        }
    except Exception as e:  # broad catch: HTTP client may raise anything
        logger.exception("web.get failed for url=%s", url)
        return {"error": str(e)}


def handle_post(params: dict) -> dict:
    """HTTP POST request. Params: url, data (opt), json_data (opt), headers (opt), timeout (opt)."""
    url = params["url"]
    headers = params.get("headers", {})
    timeout = params.get("timeout", _web_request_default_timeout())
    json_data = params.get("json_data")
    data = params.get("data")

    try:
        response = httpx.post(
            url, headers=headers, json=json_data, data=data, timeout=timeout,
        )
        max_chars = _web_response_max_chars()
        return {
            "status_code": response.status_code,
            "text": response.text[:max_chars],
            "headers": dict(response.headers),
        }
    except Exception as e:  # broad catch: HTTP client may raise anything
        logger.exception("web.post failed for url=%s", url)
        return {"error": str(e)}


def handle_fetch_webpage(params: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch the contents of a webpage from a given URL.

    Args:
        params: Dict with 'url' key containing the URL to fetch,
                optional 'timeout' (default from config), and optional 'headers'

    Returns:
        Dict with 'content' containing the HTML content, 'status_code',
        'headers', and 'url' (final URL after redirects), or 'error' if failed
    """
    url = params.get("url", "").strip()
    timeout = params.get("timeout", _web_request_default_timeout())
    headers = params.get("headers", {})

    # Validate URL
    if not url:
        return {"error": "URL parameter is required"}

    # Basic URL validation
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return {"error": "Invalid URL format. URL must include scheme (http:// or https://)"}
        if parsed.scheme not in ("http", "https"):
            return {"error": "Only HTTP and HTTPS URLs are supported"}
    except ValueError as e:
        return {"error": f"Invalid URL: {str(e)}"}

    # Set default headers for better compatibility
    default_headers = {
        "User-Agent": "Carpenter/1.0 (AI Agent Platform)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }

    # Merge user headers with defaults (user headers take precedence)
    final_headers = {**default_headers, **headers}

    try:
        # Make the HTTP request with follow_redirects=True
        response = httpx.get(
            url,
            headers=final_headers,
            timeout=timeout,
            follow_redirects=True
        )

        # Check if response is successful
        if response.status_code >= 400:
            return {
                "error": f"HTTP {response.status_code}: {response.reason_phrase}",
                "status_code": response.status_code,
                "url": str(response.url)
            }

        # Get the content, limiting size to prevent memory issues
        content = response.text
        max_bytes = _web_fetch_max_bytes()
        if len(content) > max_bytes:
            content = content[:max_bytes]
            logger.warning("Webpage content truncated to %d bytes for URL: %s", max_bytes, url)

        return {
            "content": content,
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "url": str(response.url),  # Final URL after redirects
            "encoding": response.encoding or "utf-8"
        }

    except httpx.TimeoutException:
        return {"error": f"Request timed out after {timeout} seconds"}
    except httpx.ConnectError:
        return {"error": "Failed to connect to the server. Check the URL and your internet connection."}
    except httpx.HTTPStatusError as e:
        return {
            "error": f"HTTP error {e.response.status_code}: {e.response.reason_phrase}",
            "status_code": e.response.status_code
        }
    except httpx.RequestError as e:
        return {"error": f"Request error: {str(e)}"}
    except UnicodeDecodeError:
        return {"error": "Unable to decode the webpage content. The page may contain binary data or use an unsupported encoding."}
    except (OSError, ValueError, RuntimeError) as e:
        logger.exception("Unexpected error fetching webpage: %s", url)
        return {"error": f"Unexpected error: {str(e)}"}


# ── Phase B PR B3: fetch-to-Resource helper ───────────────────────────
#
# ``web.fetch_webpage_to_resource`` is a dispatch tool (not a chat tool)
# that an arc can call to fetch a URL and persist the response body as a
# raw, ``produced_by_template=NULL`` Resource owned by the caller arc.
# It is a convenience wrapper around (HTTP GET) + ``resource.create`` +
# blob write + ``resource.finalize``, collapsed into a single trusted
# dispatch call so callers don't have to sequence four primitives by hand.
#
# Design choices documented below in the body.

# Default byte cap for fetch_webpage_to_resource when the caller doesn't
# override and no config value is set.  5 MB — generous enough for most
# HTML/JSON payloads without letting a single fetch pin megabytes of RAM.
_DEFAULT_WEB_RESOURCE_MAX_BYTES = 5_000_000


def _web_resource_max_bytes() -> int:
    """Return the default max bytes for the fetch_webpage_to_resource tool.

    Prefers ``web_resource_max_bytes`` (new key, byte-accurate) and falls
    back to ``web_fetch_max_bytes`` (existing, effectively-char limit on
    decoded text).  Keeping both means operators can tune the Resource-
    bound fetch cap independently of the in-memory fetch_webpage cap.
    """
    val = config.get_config("web_resource_max_bytes", None)
    if val is not None:
        return int(val)
    return int(config.get_config(
        "web_fetch_max_bytes", _DEFAULT_WEB_RESOURCE_MAX_BYTES,
    ))


def handle_fetch_webpage_to_resource(params: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch a URL and persist the body as a raw Resource owned by the caller arc.

    Params:
        url: str — the URL to fetch (required, http/https).
        content_type: str, optional (default ``"html"``) — free-form label
            stored on the Resource row.  NOT validated and NOT a trust
            claim: raw Resources are ``produced_by_template=NULL`` and
            therefore forever ``'untrusted'`` per ``resource_trust``.
        max_bytes: int, optional — cap bytes written to disk.  Defaults to
            ``web_resource_max_bytes`` (or ``web_fetch_max_bytes``, falling
            back to 5 MB).  If the response exceeds this, the write is
            truncated and the return value's ``truncated`` is True.
        timeout: float, optional — HTTP timeout seconds.
        headers: dict, optional — extra request headers.

    Returns (on success) a dict with:
        resource_id, file_path, byte_size, content_hash, status_code,
        final_url, truncated, content_type_declared.

    Raises ``ValueError`` / ``PermissionError`` for bad inputs or non-2xx
    responses.  On non-2xx HTTP status, NO Resource is created — the
    Resource store stays clean of failed fetches.  The caller can catch
    the exception and inspect its message for the status code.

    Called from the dispatch bridge; ``_caller_arc_id`` is injected by
    that bridge and is required.
    """
    url = (params.get("url") or "").strip()
    if not url:
        raise ValueError("web.fetch_webpage_to_resource requires non-empty url")

    caller_arc_id = params.get("_caller_arc_id")
    if caller_arc_id is None:
        raise ValueError(
            "web.fetch_webpage_to_resource requires arc context "
            "(_caller_arc_id missing); this tool is only callable from a "
            "running arc"
        )
    caller_arc_id = int(caller_arc_id)

    content_type = params.get("content_type", "html")
    if not isinstance(content_type, str) or not content_type:
        raise ValueError(
            "web.fetch_webpage_to_resource: content_type must be a non-empty string"
        )

    # URL validation — same surface as handle_fetch_webpage.
    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise ValueError(f"Invalid URL: {e}") from e
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(
            "Invalid URL format. URL must include scheme (http:// or https://)"
        )
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only HTTP and HTTPS URLs are supported")

    max_bytes = params.get("max_bytes")
    if max_bytes is None:
        max_bytes = _web_resource_max_bytes()
    max_bytes = int(max_bytes)
    if max_bytes <= 0:
        raise ValueError("max_bytes must be > 0")

    timeout = params.get("timeout", _web_request_default_timeout())
    headers = params.get("headers", {})
    default_headers = {
        "User-Agent": "Carpenter/1.0 (AI Agent Platform)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }
    final_headers = {**default_headers, **headers}

    # Perform the HTTP GET.  We use ``response.content`` (bytes) — unlike
    # handle_fetch_webpage which truncates decoded text — so the byte cap
    # is honoured exactly.  No sandbox is needed: this module runs in the
    # trusted tool-backend process, and the *output* (raw Resource) carries
    # the untrusted trust marker so downstream readers are already gated.
    response = httpx.get(
        url, headers=final_headers, timeout=timeout, follow_redirects=True,
    )

    # Explicit non-2xx policy: don't create a Resource for failed fetches.
    # Keeps the Resource store clean; callers handle the exception.
    if response.status_code >= 400:
        raise ValueError(
            f"HTTP {response.status_code}: {response.reason_phrase} "
            f"(final_url={response.url})"
        )

    body = response.content  # bytes
    truncated = False
    if len(body) > max_bytes:
        body = body[:max_bytes]
        truncated = True
        logger.warning(
            "fetch_webpage_to_resource: truncated to %d bytes for URL %s",
            max_bytes, url,
        )

    response_content_type = response.headers.get("content-type", "")
    source_descriptor = json.dumps({
        "url": url,
        "final_url": str(response.url),
        "status_code": response.status_code,
        "content_type_declared": response_content_type,
        "truncated": truncated,
    })

    # Register the row first so we know the id and can compute the path.
    resource_id = create_resource(
        content_type=content_type,
        file_path=None,
        produced_by_arc_id=caller_arc_id,
        source_descriptor=source_descriptor,
    )
    path = resource_storage_path(resource_id)
    os.makedirs(path.parent, exist_ok=True)
    set_resource_file_path(resource_id, str(path))

    with open(path, "wb") as f:
        f.write(body)

    byte_size, content_hash = hash_file(path)
    update_resource_content_stats(resource_id, byte_size, content_hash)

    link_arc_resource(
        arc_id=caller_arc_id, resource_id=resource_id, role="output",
    )

    return {
        "resource_id": resource_id,
        "file_path": str(path),
        "byte_size": byte_size,
        "content_hash": content_hash,
        "status_code": response.status_code,
        "final_url": str(response.url),
        "truncated": truncated,
        "content_type_declared": response_content_type,
    }
