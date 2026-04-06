"""Callback client for executor->platform RPC."""
import os
import httpx

# Executor reads these from environment
CALLBACK_URL = os.environ.get("CALLBACK_URL", "http://localhost:7842")
CALLBACK_TOKEN = os.environ.get("CALLBACK_TOKEN", "")
EXECUTION_SESSION = os.environ.get("CARPENTER_EXECUTION_SESSION", "")

# TLS CA cert for custom verification (self-signed certs).
# Empty = system CA bundle (correct for Let's Encrypt).
_TLS_CA_CERT = os.environ.get("CARPENTER_TLS_CA_CERT", "")

# Context env vars injected by code_manager so tools know which conversation/arc
# they are operating in.  Read once at import time (subprocess lifetime).
_CONVERSATION_ID = os.environ.get("TC_CONVERSATION_ID")
_ARC_ID = os.environ.get("TC_ARC_ID")

# Build verify parameter once at import time:
# - custom CA path when set (for self-signed certs)
# - True (system CA bundle) otherwise (works for Let's Encrypt)
_VERIFY: bool | str = _TLS_CA_CERT if _TLS_CA_CERT else True


def callback(tool_name: str, params: dict) -> dict:
    """Make synchronous HTTP(S) POST to platform callback endpoint."""
    # Auto-inject conversation/arc context so individual tools don't need to.
    if _CONVERSATION_ID and "conversation_id" not in params:
        params["conversation_id"] = int(_CONVERSATION_ID)
    if _ARC_ID:
        if "_caller_arc_id" not in params:
            params["_caller_arc_id"] = int(_ARC_ID)
        if "arc_id" not in params:
            params["arc_id"] = int(_ARC_ID)

    headers = {"X-Callback-Token": CALLBACK_TOKEN}
    if EXECUTION_SESSION:
        headers["X-Execution-Session-ID"] = EXECUTION_SESSION
    response = httpx.post(
        f"{CALLBACK_URL}/api/callbacks/{tool_name}",
        json=params,
        headers=headers,
        timeout=30.0,
        verify=_VERIFY,
    )
    response.raise_for_status()
    return response.json()
