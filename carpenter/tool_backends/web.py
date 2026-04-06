"""Web tool backend — handles HTTP requests from executors."""
import logging
from typing import Dict, Any
import httpx
from urllib.parse import urlparse

from .. import config

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
