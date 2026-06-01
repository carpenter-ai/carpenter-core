"""Web/HTTP tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""
from ..tool_meta import tool


@tool(local=False, readonly=False, side_effects=True, trusted_output=False,
      param_policies={"url": "url"},
      param_types={"url": "URL"}, return_types={"text": "UnstructuredText"})
def get(url: str, headers: dict | None = None, timeout: float = 30.0) -> dict:
    """HTTP GET request. Returns dict with status_code, text, headers."""
    ...


@tool(local=False, readonly=False, side_effects=True, trusted_output=False,
      param_policies={"url": "url"},
      param_types={"url": "URL"}, return_types={"text": "UnstructuredText"})
def post(url: str, data: dict | None = None, json_data: dict | None = None,
         headers: dict | None = None, timeout: float = 30.0) -> dict:
    """HTTP POST request. Returns dict with status_code, text, headers."""
    ...


@tool(local=False, readonly=False, side_effects=True, trusted_output=False,
      param_policies={"url": "url"},
      param_types={"url": "URL"}, return_types={"content": "UnstructuredText"})
def fetch_webpage(url: str, headers: dict | None = None, timeout: float = 30.0) -> dict:
    """Fetch the contents of a webpage. Returns dict with content, status_code, headers, url."""
    ...
