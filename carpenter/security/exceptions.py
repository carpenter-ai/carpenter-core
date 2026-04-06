"""Security-specific exceptions for Carpenter."""


class SecurityError(Exception):
    """Base exception for security subsystem errors."""


class PolicyValidationError(SecurityError):
    """Raised when a value fails validation against security policies.

    Attributes:
        policy_type: The type of policy that failed (e.g., 'email', 'domain').
        value: The value that was rejected.
        reason: Human-readable explanation.
    """

    def __init__(self, policy_type: str, value: str, reason: str = ""):
        self.policy_type = policy_type
        self.value = value
        self.reason = reason or f"Value '{value}' not in {policy_type} allowlist"
        super().__init__(self.reason)


class ConstrainedControlFlowError(SecurityError):
    """Raised when CONSTRAINED data is used in a control-flow position.

    This occurs when a Tracked wrapper's __bool__() is called and the
    underlying value has a non-TRUSTED integrity label.
    """

    def __init__(self, label: str, operation: str = "bool"):
        self.label = label
        self.operation = operation
        super().__init__(
            f"Cannot use {label}-labeled data in control flow ({operation}). "
            f"Use policy-typed literals for deterministic comparison."
        )
