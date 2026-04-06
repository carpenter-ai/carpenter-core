"""Chat tools for filesystem read operations."""

from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description="Read the contents of a file from the filesystem.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the file to read.",
            },
        },
        "required": ["path"],
    },
    capabilities=["filesystem_read"],
    always_available=True,
)
def read_file(tool_input, **kwargs):
    from carpenter.tool_backends import files as files_backend
    result = files_backend.handle_read(tool_input)
    return result.get("content", "(empty)")


@chat_tool(
    description="List files in a directory.",
    input_schema={
        "type": "object",
        "properties": {
            "dir": {
                "type": "string",
                "description": "Absolute path to the directory to list.",
            },
        },
        "required": ["dir"],
    },
    capabilities=["filesystem_read"],
    always_available=True,
)
def list_files(tool_input, **kwargs):
    from carpenter.tool_backends import files as files_backend
    result = files_backend.handle_list(tool_input)
    files = result.get("files", [])
    return "\n".join(files) if files else "(empty directory)"


@chat_tool(
    description="Count the number of files (not subdirectories) in a directory.",
    input_schema={
        "type": "object",
        "properties": {
            "directory": {
                "type": "string",
                "description": "Absolute path to the directory to count files in.",
            },
        },
        "required": ["directory"],
    },
    capabilities=["filesystem_read"],
)
def file_count(tool_input, **kwargs):
    from carpenter.tool_backends import files as files_backend
    result = files_backend.handle_file_count(tool_input)
    if "error" in result:
        return f"Error: {result['error']}"
    return str(result.get("file_count", 0))
