"""Executor package — RestrictedPython + threading executor.

The only supported executor is the RestrictedExecutor, which runs code
in-process using RestrictedPython for sandboxing and threading for
isolation.  The old subprocess/Docker executors have been removed.

Use ``RestrictedExecutor`` directly via ``from carpenter.executor.restricted
import RestrictedExecutor``, or get_executor() for config-driven usage.
"""

import logging

from .restricted import RestrictedExecutor

_logger = logging.getLogger(__name__)


def register_executor(name: str, cls: type) -> None:
    """No-op shim for backward compatibility with platform packages.

    The subprocess/Docker executors have been removed. This function
    exists only so that older platform packages (e.g. carpenter-linux)
    don't crash on import. The registered executor is silently ignored.
    """
    _logger.info(
        "register_executor(%r) called but ignored — only the "
        "RestrictedExecutor is supported", name,
    )


def get_executor(executor_type: str | None = None) -> RestrictedExecutor:
    """Factory: return the RestrictedExecutor.

    Args:
        executor_type: Must be ``"restricted"`` or ``None``.

    Returns:
        A RestrictedExecutor instance.

    Raises:
        ValueError: If executor_type is not ``"restricted"``.
    """
    if executor_type is None:
        executor_type = "restricted"

    if executor_type != "restricted":
        raise ValueError(
            f"Unknown executor_type: {executor_type!r}. "
            f"Only 'restricted' is supported. The subprocess and Docker "
            f"executors have been removed."
        )

    return RestrictedExecutor()


__all__ = ["RestrictedExecutor", "get_executor", "register_executor"]
