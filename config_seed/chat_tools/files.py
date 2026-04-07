"""Chat tools for filesystem read operations.

All paths are restricted to the Carpenter base directory (~/carpenter by
default, or the configured ``base_dir``).  Any attempt to read outside that
tree is rejected before the backend is called.
"""

import os

from carpenter.chat_tool_loader import chat_tool


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
    error = _check_path(tool_input["path"])
    if error:
        return error
    from carpenter.tool_backends import files as files_backend
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
    return "\n".join(files) if files else "(empty directory)"


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
