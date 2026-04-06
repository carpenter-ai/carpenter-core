"""No-op sandbox — returns commands unchanged."""


def build_command(inner_cmd: list[str], write_dirs: list[str]) -> list[str]:
    """Return command unchanged (no sandboxing)."""
    return inner_cmd


def build_shell_command(shell_cmd: str, cwd: str, write_dirs: list[str]) -> list[str]:
    """Return shell command unchanged (no sandboxing)."""
    return ["bash", "-c", shell_cmd]
