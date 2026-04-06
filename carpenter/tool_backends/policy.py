"""Platform-side handler for policy.validate callback.

Validates policy-typed literal values against the security policies
configured on the platform. This is the server-side counterpart to
carpenter_tools/policy/_validate.py.
"""

import logging

from ..security.policies import get_policies
from ..security.exceptions import PolicyValidationError

logger = logging.getLogger(__name__)


def handle_validate(params: dict) -> dict:
    """Validate a value against security policies.

    Args:
        params: {"policy_type": str, "value": str}

    Returns:
        {"allowed": True} or {"allowed": False, "reason": str}
    """
    policy_type = params.get("policy_type", "")
    value = params.get("value", "")

    if not policy_type:
        return {"allowed": False, "reason": "Missing policy_type"}

    policies = get_policies()

    try:
        policies.validate(policy_type, value)
        return {"allowed": True}
    except PolicyValidationError as e:
        return {"allowed": False, "reason": str(e)}
    except ValueError as e:
        return {"allowed": False, "reason": str(e)}
