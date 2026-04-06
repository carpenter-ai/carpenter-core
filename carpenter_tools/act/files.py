"""Write file tools. Tier 2: runs directly, no callback."""
import os

from ..tool_meta import tool


@tool(local=True, readonly=False, side_effects=True,
      param_policies={"path": "filepath"},
      param_types={"path": "WorkspacePath", "content": "UnstructuredText"})
def write(path: str, content: str):
    """Write content to a file. Creates parent directories if needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
