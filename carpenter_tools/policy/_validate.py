"""Executor-side policy validation via platform callback.

In verification mode, policy-typed literals validate their values
against the platform's security policies by calling the policy.validate
callback endpoint.
"""

import json
import os

import httpx


def validate_policy_value(policy_type: str, value: str) -> bool:
    """Validate a policy value by calling the platform callback.

    Raises PolicyValidationError (via HTTP error) if denied.
    Returns True if allowed.
    """
    callback_url = os.environ.get("CALLBACK_URL", "http://localhost:7842")
    callback_token = os.environ.get("CALLBACK_TOKEN", "")

    headers = {}
    if callback_token:
        headers["Authorization"] = f"Bearer {callback_token}"

    try:
        resp = httpx.post(
            f"{callback_url}/api/callbacks/policy.validate",
            json={
                "policy_type": policy_type,
                "value": value,
            },
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            result = resp.json()
            if result.get("allowed"):
                return True
            reason = result.get("reason", f"Value not in {policy_type} allowlist")
            from carpenter.security.exceptions import PolicyValidationError
            raise PolicyValidationError(policy_type, value, reason)
        else:
            from carpenter.security.exceptions import PolicyValidationError
            raise PolicyValidationError(
                policy_type, value,
                f"Policy validation failed: HTTP {resp.status_code}"
            )
    except httpx.HTTPError as e:
        from carpenter.security.exceptions import PolicyValidationError
        raise PolicyValidationError(
            policy_type, value, f"Policy validation callback failed: {e}"
        )
