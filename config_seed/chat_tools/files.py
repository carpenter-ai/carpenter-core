"""Chat tools for filesystem read operations.

All paths are restricted to the Carpenter base directory (~/carpenter by
default, or the configured ``base_dir``).  Any attempt to read outside that
tree is rejected before the backend is called.
"""

import os

from carpenter.chat_tool_loader import chat_tool
from carpenter.security.platform_paths import (
    audit_path_decision,
    is_invisible,
)


def _allowed_base() -> str:
    """Return the resolved absolute path of the Carpenter base directory."""
    from carpenter.config import CONFIG
    return os.path.realpath(os.path.expanduser(
        CONFIG.get("base_dir", "~/carpenter")
    ))


def _check_path(path: str) -> str | None:
    """Return an error string if *path* is outside the allowed base, else None."""
    base = _allowed_base()
    resolved = os.path.realpath(os.path.expanduser(path))
    if resolved != base and not resolved.startswith(base + os.sep):
        return (
            f"Access denied: path is outside the Carpenter directory "
            f"({base}).  The read_file / list_files tools can only access "
            f"files within that directory."
        )
    return None


@chat_tool(
    description=(
        "Read the contents of a file from the Carpenter config directory.  "
        "The path must be inside the Carpenter base directory (~/carpenter)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Absolute path to the file to read.  Must be within the "
                    "Carpenter base directory."
                ),
            },
        },
        "required": ["path"],
    },
    capabilities=["filesystem_read"],
    always_available=True,
)
def read_file(tool_input, **kwargs):
    # Platform-integrity tier check (I12).  Belt-and-suspenders alongside
    # the backend check in ``files.chat_read_provenance_check`` — running
    # the check here first means a misconfigured ``base_dir`` cannot
    # accidentally let a T0 path through under a different base_dir
    # resolution, and chat callers get a tool-friendly denial rather
    # than a raw DispatchError.
    path = tool_input["path"]
    try:
        if is_invisible(path):
            audit_path_decision(
                None,
                "t0_read_refused",
                os.path.realpath(os.path.expanduser(path)),
                {"tool": "chat.read_file"},
            )
            return (
                "Access denied: path is platform-invisible "
                "(credentials, platform database, or other restricted "
                "platform state)."
            )
    except Exception:  # noqa: BLE001 — fail open; backend will recheck
        pass
    error = _check_path(tool_input["path"])
    if error:
        return error
    from carpenter.tool_backends import files as files_backend
    # I2 enforcement on the chat path: chat agents are TRUSTED-only
    # (docs/design.md §"Agent Types and Capabilities") and cannot
    # read bytes produced by a non-trusted writer.  handle_read's
    # cross-trust check requires a _caller_arc_id which the chat tool
    # cannot supply, so we consult provenance directly here.
    refusal = files_backend.chat_read_provenance_check(tool_input["path"])
    if refusal:
        return refusal
    result = files_backend.handle_read(tool_input)
    return result.get("content", "(empty)")


@chat_tool(
    description=(
        "List files in a directory within the Carpenter config directory.  "
        "The directory must be inside the Carpenter base directory (~/carpenter)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "dir": {
                "type": "string",
                "description": (
                    "Absolute path to the directory to list.  Must be within "
                    "the Carpenter base directory."
                ),
            },
        },
        "required": ["dir"],
    },
    capabilities=["filesystem_read"],
    always_available=True,
)
def list_files(tool_input, **kwargs):
    error = _check_path(tool_input["dir"])
    if error:
        return error
    from carpenter.tool_backends import files as files_backend
    result = files_backend.handle_list(tool_input)
    files = result.get("files", [])
    # Belt-and-suspenders: the backend already filters T0 entries.
    # Re-filter here so a stale handler import (or a backend bypass)
    # still doesn't leak invisible filenames into the chat surface.
    directory = tool_input["dir"]
    safe: list[str] = []
    for name in files:
        try:
            if is_invisible(os.path.join(directory, name)):
                continue
        except Exception:  # noqa: BLE001
            pass
        safe.append(name)
    return "\n".join(safe) if safe else "(empty directory)"


@chat_tool(
    description=(
        "Count the number of files (not subdirectories) in a directory "
        "within the Carpenter config directory.  The directory must be inside "
        "the Carpenter base directory (~/carpenter)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "directory": {
                "type": "string",
                "description": (
                    "Absolute path to the directory to count files in.  Must "
                    "be within the Carpenter base directory."
                ),
            },
        },
        "required": ["directory"],
    },
    capabilities=["filesystem_read"],
)
def file_count(tool_input, **kwargs):
    error = _check_path(tool_input["directory"])
    if error:
        return error
    from carpenter.tool_backends import files as files_backend
    result = files_backend.handle_file_count(tool_input)
    if "error" in result:
        return f"Error: {result['error']}"
    return str(result.get("file_count", 0))
