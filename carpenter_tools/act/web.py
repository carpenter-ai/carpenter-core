"""Web/HTTP tools. Tier 1: callback to platform (external, network egress)."""
from .._callback import callback
from ..tool_meta import tool


@tool(local=False, readonly=False, side_effects=True, trusted_output=False,
      param_policies={"url": "url"},
      param_types={"url": "URL"}, return_types={"text": "UnstructuredText"})
def get(url: str, headers: dict | None = None, timeout: float = 30.0) -> dict:
    """HTTP GET request. Returns dict with status_code, text, headers."""
    params = {"url": url}
    if headers is not None:
        params["headers"] = headers
    if timeout != 30.0:
        params["timeout"] = timeout
    return callback("web.get", params)


@tool(local=False, readonly=False, side_effects=True, trusted_output=False,
      param_policies={"url": "url"},
      param_types={"url": "URL"}, return_types={"text": "UnstructuredText"})
def post(url: str, data: dict | None = None, json_data: dict | None = None,
         headers: dict | None = None, timeout: float = 30.0) -> dict:
    """HTTP POST request. Returns dict with status_code, text, headers."""
    params = {"url": url}
    if data is not None:
        params["data"] = data
    if json_data is not None:
        params["json_data"] = json_data
    if headers is not None:
        params["headers"] = headers
    if timeout != 30.0:
        params["timeout"] = timeout
    return callback("web.post", params)


@tool(local=False, readonly=False, side_effects=True, trusted_output=False,
      param_policies={"url": "url"},
      param_types={"url": "URL"}, return_types={"content": "UnstructuredText"})
def fetch_webpage(url: str, headers: dict | None = None, timeout: float = 30.0) -> dict:
    """Fetch the contents of a webpage. Returns dict with content, status_code, headers, url."""
    params = {"url": url}
    if headers is not None:
        params["headers"] = headers
    if timeout != 30.0:
        params["timeout"] = timeout
    return callback("web.fetch_webpage", params)
