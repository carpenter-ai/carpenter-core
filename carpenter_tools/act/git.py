"""Git operation tool declarations.

See ``carpenter_tools`` package docstring for the invocation model.
"""
from ..tool_meta import tool


@tool(local=False, readonly=False, side_effects=True,
      param_types={"repo_url": "URL", "workspace": "WorkspacePath", "fork_url": "URL", "branch": "Label"})
def setup_repo(repo_url, workspace, fork_url, branch=None):
    """Clone and configure repository with fork-based workflow."""
    ...


@tool(local=False, readonly=False, side_effects=True,
      param_types={"workspace": "WorkspacePath", "branch_name": "Label"})
def create_branch(workspace, branch_name):
    """Create or switch to a feature branch."""
    ...


@tool(local=False, readonly=False, side_effects=True,
      param_types={"workspace": "WorkspacePath", "branch_name": "Label", "commit_message": "UnstructuredText"})
def commit_and_push(workspace, branch_name, commit_message, files=None):
    """Commit changes, rebase on upstream/main, push to fork."""
    ...


@tool(local=False, readonly=False, side_effects=True,
      param_types={"repo_owner": "Label", "repo_name": "Label", "branch_name": "Label", "pr_title": "UnstructuredText", "pr_body": "UnstructuredText", "fork_user": "Label"})
def create_pr(repo_owner, repo_name, branch_name, pr_title, pr_body=None,
              fork_user=None):
    """Create pull request via configured forge."""
    ...


@tool(local=False, readonly=False, side_effects=True,
      param_types={"repo_owner": "Label", "repo_name": "Label", "state": "Label"})
def list_prs(repo_owner, repo_name, state="open"):
    """List pull requests via configured forge."""
    ...


@tool(local=False, readonly=False, side_effects=True,
      param_types={"repo_owner": "Label", "repo_name": "Label", "merge_method": "Label"})
def merge_pr(repo_owner, repo_name, pr_number, merge_method="merge"):
    """Merge a pull request via configured forge."""
    ...


@tool(local=False, readonly=False, side_effects=True,
      param_types={"repo_owner": "Label", "repo_name": "Label", "comment": "UnstructuredText"})
def close_pr(repo_owner, repo_name, pr_number, comment=None):
    """Close a PR without merging."""
    ...


@tool(local=False, readonly=False, side_effects=True,
      param_types={"repo_owner": "Label", "repo_name": "Label", "body": "UnstructuredText", "event": "Label"})
def post_pr_review(repo_owner, repo_name, pr_number, body, event="COMMENT",
                   comments=None):
    """Submit a review on a PR (APPROVED, REQUEST_CHANGES, or COMMENT)."""
    ...


@tool(local=False, readonly=False, side_effects=True,
      param_types={"repo_owner": "Label", "repo_name": "Label", "target_url": "URL", "secret": "Label", "content_type": "Label"})
def create_repo_webhook(repo_owner, repo_name, target_url, events=None,
                        secret=None, content_type="json"):
    """Register a webhook on a repo."""
    ...


@tool(local=False, readonly=False, side_effects=True,
      param_types={"repo_owner": "Label", "repo_name": "Label"})
def delete_repo_webhook(repo_owner, repo_name, hook_id):
    """Remove a webhook from a repo."""
    ...
