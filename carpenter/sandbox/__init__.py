"""Filesystem sandboxing for code execution.

Provides kernel-enforced filesystem isolation using Linux user+mount
namespaces (preferred), bubblewrap, or a no-op fallback. Sandbox wraps
subprocess execution — orthogonal to executor_type.

Public API:
    SandboxConfig — configuration dataclass
    SandboxError  — raised in fail-closed mode
    get_sandbox_config() — build config from CONFIG, auto-detect if needed
    sandbox_command()    — wrap a command list for sandboxed execution
    sandbox_shell_command() — wrap a shell command string for sandboxed execution
"""

import logging
import os
import tempfile
from dataclasses import dataclass, field

from .. import config

logger = logging.getLogger(__name__)

# Cached detection result (populated on first get_sandbox_config() call)
_cached_config: "SandboxConfig | None" = None

# Injected sandbox provider (overrides built-in auto-detection)
_sandbox_provider = None

# Registry of sandbox methods added by platform packages
_sandbox_methods: dict[str, tuple] = {}


def set_sandbox_provider(detect_fn) -> None:
    """Override the auto-detection step in get_sandbox_config().

    Args:
        detect_fn: A callable returning a dict with a 'recommended' key.
    """
    global _sandbox_provider
    _sandbox_provider = detect_fn


def register_sandbox_method(name: str, build_cmd_fn, build_shell_cmd_fn) -> None:
    """Register a sandbox method for use by sandbox_command()/sandbox_shell_command().

    Args:
        name: Method name (e.g. 'landlock', 'namespace').
        build_cmd_fn: Callable(command, write_dirs) -> wrapped command list.
        build_shell_cmd_fn: Callable(shell_cmd, cwd, write_dirs) -> command list.
    """
    _sandbox_methods[name] = (build_cmd_fn, build_shell_cmd_fn)


class SandboxError(Exception):
    """Raised when sandbox creation fails in fail-closed mode."""


@dataclass
class SandboxConfig:
    """Sandbox configuration."""
    method: str = "none"  # none, namespace, bubblewrap, landlock, apparmor, auto
    allowed_write_dirs: list[str] = field(default_factory=list)
    on_failure: str = "closed"  # closed = refuse execution, open = fallback unsandboxed


def get_sandbox_config() -> SandboxConfig:
    """Build sandbox config from CONFIG, running auto-detection if needed.

    Results are cached after the first call. The cache can be cleared
    by setting the module-level _cached_config to None.
    """
    global _cached_config
    if _cached_config is not None:
        return _cached_config

    sandbox_cfg = config.CONFIG.get("sandbox", {})
    method = sandbox_cfg.get("method", "auto")
    on_failure = sandbox_cfg.get("on_failure", "closed")

    # Compute default write dirs from config paths
    configured_write_dirs = sandbox_cfg.get("allowed_write_dirs", [])
    if configured_write_dirs:
        write_dirs = list(configured_write_dirs)
    else:
        write_dirs = _default_write_dirs()

    # Auto-detect best available method
    if method == "auto":
        if _sandbox_provider is not None:
            caps = _sandbox_provider()
            method = caps["recommended"]
            logger.info("Sandbox auto-detection: %s (caps: %s)", method, caps)
        else:
            method = "none"
            logger.info("No sandbox provider registered, defaulting to none")
        if method == "none":
            logger.warning(
                "No sandbox method available. Code execution will be unsandboxed."
            )

    cfg = SandboxConfig(
        method=method,
        allowed_write_dirs=write_dirs,
        on_failure=on_failure,
    )
    _cached_config = cfg
    return cfg


def _default_write_dirs() -> list[str]:
    """Compute default writable directories from config paths."""
    dirs = []
    for key in ("workspaces_dir", "code_dir", "log_dir"):
        val = config.CONFIG.get(key)
        if val:
            dirs.append(os.path.expanduser(val))
    dirs.append(tempfile.gettempdir())
    return dirs


def sandbox_command(command: list[str], cfg: SandboxConfig) -> list[str]:
    """Wrap a command with sandbox prefix based on config.

    Args:
        command: The command to run (e.g. ["python3", "script.py"]).
        cfg: Sandbox configuration.

    Returns:
        Wrapped command list, or original command on failure/noop.

    Raises:
        SandboxError: If sandbox creation fails and on_failure is "closed".
    """
    if cfg.method == "none":
        return command

    try:
        if cfg.method in _sandbox_methods:
            build_cmd_fn = _sandbox_methods[cfg.method][0]
            return build_cmd_fn(command, cfg.allowed_write_dirs)
        else:
            logger.error("Unknown sandbox method: %s", cfg.method)
            return _handle_failure(command, cfg, f"Unknown method: {cfg.method}")
    except (OSError, ValueError, RuntimeError) as e:
        return _handle_failure(command, cfg, str(e))


def sandbox_shell_command(shell_cmd: str, cwd: str, cfg: SandboxConfig) -> list[str]:
    """Wrap a shell command for sandboxed execution.

    Args:
        shell_cmd: Shell command string.
        cwd: Working directory for the command.
        cfg: Sandbox configuration.

    Returns:
        Command list for sandboxed execution, or simple bash -c on failure/noop.

    Raises:
        SandboxError: If sandbox creation fails and on_failure is "closed".
    """
    if cfg.method == "none":
        return ["bash", "-c", shell_cmd]

    try:
        if cfg.method in _sandbox_methods:
            build_shell_cmd_fn = _sandbox_methods[cfg.method][1]
            return build_shell_cmd_fn(shell_cmd, cwd, cfg.allowed_write_dirs)
        else:
            logger.error("Unknown sandbox method: %s", cfg.method)
            return _handle_failure(
                ["bash", "-c", shell_cmd], cfg, f"Unknown method: {cfg.method}"
            )
    except (OSError, ValueError, RuntimeError) as e:
        return _handle_failure(["bash", "-c", shell_cmd], cfg, str(e))


def _handle_failure(
    fallback_cmd: list[str], cfg: SandboxConfig, error_msg: str
) -> list[str]:
    """Handle sandbox creation failure based on on_failure policy.

    Returns:
        The fallback command (fail-open).

    Raises:
        SandboxError: If on_failure is "closed".
    """
    if cfg.on_failure == "closed":
        raise SandboxError(f"Sandbox creation failed: {error_msg}")
    else:
        logger.error(
            "Sandbox creation failed (falling back to unsandboxed): %s", error_msg
        )
        return fallback_cmd
