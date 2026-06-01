"""Read-only Git operation tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""
from ..tool_meta import tool


@tool(local=True, readonly=True, side_effects=False,
      param_types={"repo_owner": "Label", "repo_name": "Label"})
def get_pr(repo_owner, repo_name, pr_number):
    """Get PR metadata (title, body, state, branches, user)."""
    ...


@tool(local=True, readonly=True, side_effects=False,
      param_types={"repo_owner": "Label", "repo_name": "Label"})
def get_pr_diff(repo_owner, repo_name, pr_number):
    """Get PR diff as unified text."""
    ...
