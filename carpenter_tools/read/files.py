"""Read-only file tools. Tier 2: runs directly, no callback."""
import os

from ..tool_meta import tool


@tool(local=True, readonly=True, side_effects=False,
      param_policies={"path": "filepath"},
      param_types={"path": "WorkspacePath"}, return_types="UnstructuredText")
def read(path: str) -> str:
    """Read file contents."""
    with open(path, "r") as f:
        return f.read()


@tool(local=True, readonly=True, side_effects=False,
      param_policies={"directory": "filepath"},
      param_types={"directory": "WorkspacePath"})
def list_dir(directory: str) -> list[str]:
    """List directory contents."""
    return os.listdir(directory)
