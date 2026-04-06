"""Shared fixtures and helpers for tests/agent/ test modules."""


def _mock_api_response(text, usage=None, model=None):
    """Create a mock Claude API response.

    This helper is used across multiple agent test files (compaction,
    invocation, etc.) to build the dict structure returned by
    ``_call_with_retries`` or ``claude_client.call``.

    Parameters
    ----------
    text : str
        The assistant reply text.
    usage : dict | None
        Token usage dict.  Defaults to ``{"input_tokens": 100, "output_tokens": 50}``.
    model : str | None
        Optional model name to include in the response.
    """
    resp = {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": usage or {"input_tokens": 100, "output_tokens": 50},
    }
    if model:
        resp["model"] = model
    return resp
