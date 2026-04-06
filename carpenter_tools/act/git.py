"""Git operations. Tier 1: callback to platform (external + credentials)."""
from .._callback import callback
from ..tool_meta import tool


@tool(local=False, readonly=False, side_effects=True,
      param_types={"repo_url": "URL", "workspace": "WorkspacePath", "fork_url": "URL", "branch": "Label"})
def setup_repo(repo_url, workspace, fork_url, branch=None):
    """Clone and configure repository with fork-based workflow."""
    params = {"repo_url": repo_url, "workspace": workspace, "fork_url": fork_url}
    if branch:
        params["branch"] = branch
    return callback("git.setup_repo", params)


@tool(local=False, readonly=False, side_effects=True,
      param_types={"workspace": "WorkspacePath", "branch_name": "Label"})
def create_branch(workspace, branch_name):
    """Create or switch to a feature branch."""
    return callback("git.create_branch", {
        "workspace": workspace,
        "branch_name": branch_name,
    })


@tool(local=False, readonly=False, side_effects=True,
      param_types={"workspace": "WorkspacePath", "branch_name": "Label", "commit_message": "UnstructuredText"})
def commit_and_push(workspace, branch_name, commit_message, files=None):
    """Commit changes, rebase on upstream/main, push to fork."""
    params = {
        "workspace": workspace,
        "branch_name": branch_name,
        "commit_message": commit_message,
    }
    if files:
        params["files"] = files
    return callback("git.commit_and_push", params)


@tool(local=False, readonly=False, side_effects=True,
      param_types={"repo_owner": "Label", "repo_name": "Label", "branch_name": "Label", "pr_title": "UnstructuredText", "pr_body": "UnstructuredText", "fork_user": "Label"})
def create_pr(repo_owner, repo_name, branch_name, pr_title, pr_body=None,
              fork_user=None):
    """Create pull request via Forgejo API."""
    params = {
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "branch_name": branch_name,
        "pr_title": pr_title,
        "fork_user": fork_user,
    }
    if pr_body:
        params["pr_body"] = pr_body
    return callback("git.create_pr", params)


@tool(local=False, readonly=False, side_effects=True,
      param_types={"repo_owner": "Label", "repo_name": "Label", "state": "Label"})
def list_prs(repo_owner, repo_name, state="open"):
    """List pull requests via Forgejo API."""
    return callback("git.list_prs", {
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "state": state,
    })


@tool(local=False, readonly=False, side_effects=True,
      param_types={"repo_owner": "Label", "repo_name": "Label", "merge_method": "Label"})
def merge_pr(repo_owner, repo_name, pr_number, merge_method="merge"):
    """Merge a pull request via Forgejo API."""
    return callback("git.merge_pr", {
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "pr_number": pr_number,
        "merge_method": merge_method,
    })


@tool(local=False, readonly=False, side_effects=True,
      param_types={"repo_owner": "Label", "repo_name": "Label", "comment": "UnstructuredText"})
def close_pr(repo_owner, repo_name, pr_number, comment=None):
    """Close a PR without merging."""
    params = {
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "pr_number": pr_number,
    }
    if comment:
        params["comment"] = comment
    return callback("git.close_pr", params)


@tool(local=False, readonly=False, side_effects=True,
      param_types={"repo_owner": "Label", "repo_name": "Label", "body": "UnstructuredText", "event": "Label"})
def post_pr_review(repo_owner, repo_name, pr_number, body, event="COMMENT",
                   comments=None):
    """Submit a review on a PR (APPROVED, REQUEST_CHANGES, or COMMENT)."""
    params = {
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "pr_number": pr_number,
        "body": body,
        "event": event,
    }
    if comments:
        params["comments"] = comments
    return callback("git.post_pr_review", params)


@tool(local=False, readonly=False, side_effects=True,
      param_types={"repo_owner": "Label", "repo_name": "Label", "target_url": "URL", "secret": "Label", "content_type": "Label"})
def create_repo_webhook(repo_owner, repo_name, target_url, events=None,
                        secret=None, content_type="json"):
    """Register a webhook on a repo."""
    params = {
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "target_url": target_url,
        "content_type": content_type,
    }
    if events:
        params["events"] = events
    if secret:
        params["secret"] = secret
    return callback("git.create_repo_webhook", params)


@tool(local=False, readonly=False, side_effects=True,
      param_types={"repo_owner": "Label", "repo_name": "Label"})
def delete_repo_webhook(repo_owner, repo_name, hook_id):
    """Remove a webhook from a repo."""
    return callback("git.delete_repo_webhook", {
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "hook_id": hook_id,
    })
