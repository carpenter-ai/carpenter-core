"""Read-only Git operations. Tier 1: callback to platform (requires credentials)."""
from .._callback import callback
from ..tool_meta import tool


@tool(local=True, readonly=True, side_effects=False,
      param_types={"repo_owner": "Label", "repo_name": "Label"})
def get_pr(repo_owner, repo_name, pr_number):
    """Get PR metadata (title, body, state, branches, user)."""
    return callback("git.get_pr", {
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "pr_number": pr_number,
    })


@tool(local=True, readonly=True, side_effects=False,
      param_types={"repo_owner": "Label", "repo_name": "Label"})
def get_pr_diff(repo_owner, repo_name, pr_number):
    """Get PR diff as unified text."""
    return callback("git.get_pr_diff", {
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "pr_number": pr_number,
    })
