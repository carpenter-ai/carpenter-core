"""Forgejo API handlers — PR, webhook, and review operations.

Separated from git.py to isolate Forgejo-specific API calls from
pure-git operations. Imports shared helpers from git.py.

This is a Tier 1 (callback) backend: credentials stay on the platform side.
"""
import logging

import httpx

from .git import (
    _git_server_url,
    _git_token,
    _git_server_headers,
    _git_api_timeout,
    _git_api_long_timeout,
)

logger = logging.getLogger(__name__)


def handle_create_pr(params: dict) -> dict:
    """Create pull request via Forgejo API.

    params: {repo_owner, repo_name, branch_name, pr_title, pr_body (opt), fork_user}
    Returns: {pr_number, pr_url}
    """
    repo_owner = params["repo_owner"]
    repo_name = params["repo_name"]
    branch_name = params["branch_name"]
    pr_title = params["pr_title"]
    pr_body = params.get("pr_body", "")
    fork_user = params["fork_user"]

    url = f"{_git_server_url()}/repos/{repo_owner}/{repo_name}/pulls"
    payload = {
        "title": pr_title,
        "head": f"{fork_user}:{branch_name}",
        "base": "main",
        "body": pr_body,
    }

    response = httpx.post(url, json=payload, headers=_git_server_headers(), timeout=_git_api_timeout())
    data = response.json()

    if response.status_code in (200, 201):
        return {
            "pr_number": data["number"],
            "pr_url": data.get("html_url", ""),
        }
    else:
        return {"error": data.get("message", response.text)}


def handle_list_prs(params: dict) -> dict:
    """List pull requests via Forgejo API.

    params: {repo_owner, repo_name, state (opt, default "open")}
    Returns: {prs: [{number, title, url, state, head_branch}]}
    """
    repo_owner = params["repo_owner"]
    repo_name = params["repo_name"]
    state = params.get("state", "open")

    url = f"{_git_server_url()}/repos/{repo_owner}/{repo_name}/pulls"
    response = httpx.get(
        url,
        params={"state": state},
        headers=_git_server_headers(),
        timeout=_git_api_timeout(),
    )
    data = response.json()

    prs = []
    for pr in data:
        prs.append({
            "number": pr["number"],
            "title": pr["title"],
            "url": pr.get("html_url", ""),
            "state": pr["state"],
            "head_branch": pr.get("head", {}).get("ref", ""),
        })

    return {"prs": prs}


def handle_merge_pr(params: dict) -> dict:
    """Merge a pull request via Forgejo API.

    params: {repo_owner, repo_name, pr_number, merge_method (opt, default "merge")}
    Returns: {merged: bool}
    """
    repo_owner = params["repo_owner"]
    repo_name = params["repo_name"]
    pr_number = params["pr_number"]
    merge_method = params.get("merge_method", "merge")

    url = (
        f"{_git_server_url()}/repos/{repo_owner}/{repo_name}"
        f"/pulls/{pr_number}/merge"
    )
    payload = {"Do": merge_method}

    response = httpx.post(url, json=payload, headers=_git_server_headers(), timeout=_git_api_timeout())

    if response.status_code in (200, 204):
        return {"merged": True}
    else:
        data = response.json() if response.text else {}
        return {"merged": False, "error": data.get("message", response.text)}


def handle_close_pr(params: dict) -> dict:
    """Close a PR without merging.

    params: {repo_owner, repo_name, pr_number, comment (opt)}
    Returns: {closed: bool}
    """
    repo_owner = params["repo_owner"]
    repo_name = params["repo_name"]
    pr_number = params["pr_number"]
    comment = params.get("comment")

    headers = _git_server_headers()

    # Add comment if provided
    if comment:
        comment_url = (
            f"{_git_server_url()}/repos/{repo_owner}/{repo_name}"
            f"/issues/{pr_number}/comments"
        )
        httpx.post(
            comment_url,
            json={"body": comment},
            headers=headers,
            timeout=_git_api_timeout(),
        )

    # Close the PR via PATCH
    url = (
        f"{_git_server_url()}/repos/{repo_owner}/{repo_name}"
        f"/pulls/{pr_number}"
    )
    response = httpx.patch(
        url, json={"state": "closed"}, headers=headers, timeout=_git_api_timeout(),
    )
    data = response.json()

    if data.get("state") == "closed":
        return {"closed": True}
    else:
        return {"closed": False, "error": data.get("message", response.text)}


def handle_get_pr(params: dict) -> dict:
    """Get PR metadata via Forgejo API.

    params: {repo_owner, repo_name, pr_number}
    Returns: {number, title, body, state, head_branch, base_branch, user, html_url}
    """
    repo_owner = params["repo_owner"]
    repo_name = params["repo_name"]
    pr_number = params["pr_number"]

    url = (
        f"{_git_server_url()}/repos/{repo_owner}/{repo_name}"
        f"/pulls/{pr_number}"
    )
    response = httpx.get(url, headers=_git_server_headers(), timeout=_git_api_timeout())

    if response.status_code != 200:
        data = response.json() if response.text else {}
        return {"error": data.get("message", response.text)}

    data = response.json()
    return {
        "number": data["number"],
        "title": data["title"],
        "body": data.get("body", ""),
        "state": data["state"],
        "head_branch": data.get("head", {}).get("ref", ""),
        "base_branch": data.get("base", {}).get("ref", ""),
        "user": data.get("user", {}).get("login", ""),
        "html_url": data.get("html_url", ""),
    }


def handle_get_pr_diff(params: dict) -> dict:
    """Get PR diff as unified text via Forgejo API.

    params: {repo_owner, repo_name, pr_number}
    Returns: {diff: str}
    """
    repo_owner = params["repo_owner"]
    repo_name = params["repo_name"]
    pr_number = params["pr_number"]

    url = (
        f"{_git_server_url()}/repos/{repo_owner}/{repo_name}"
        f"/pulls/{pr_number}.diff"
    )
    response = httpx.get(url, headers=_git_server_headers(), timeout=_git_api_long_timeout())

    if response.status_code != 200:
        return {"error": f"Failed to fetch diff: HTTP {response.status_code}"}

    return {"diff": response.text}


def handle_post_pr_review(params: dict) -> dict:
    """Submit a review on a PR via Forgejo API.

    params: {repo_owner, repo_name, pr_number, body, event,
             comments (opt, list of {path, body, new_position})}
    event: "APPROVED", "REQUEST_CHANGES", or "COMMENT"
    Returns: {review_id, state}
    """
    repo_owner = params["repo_owner"]
    repo_name = params["repo_name"]
    pr_number = params["pr_number"]
    body = params.get("body", "")
    event = params.get("event", "COMMENT")
    comments = params.get("comments", [])

    url = (
        f"{_git_server_url()}/repos/{repo_owner}/{repo_name}"
        f"/pulls/{pr_number}/reviews"
    )
    payload = {
        "body": body,
        "event": event,
    }
    if comments:
        payload["comments"] = comments

    response = httpx.post(
        url, json=payload, headers=_git_server_headers(), timeout=_git_api_timeout(),
    )
    data = response.json()

    if response.status_code in (200, 201):
        return {
            "review_id": data.get("id"),
            "state": data.get("state", ""),
        }
    else:
        return {"error": data.get("message", response.text)}


def handle_create_repo_webhook(params: dict) -> dict:
    """Register a webhook on a repo via Forgejo API.

    params: {repo_owner, repo_name, target_url, events (list), secret (opt),
             content_type (opt, default "json")}
    Returns: {hook_id, active}
    """
    repo_owner = params["repo_owner"]
    repo_name = params["repo_name"]
    target_url = params["target_url"]
    events = params.get("events", ["push"])
    secret = params.get("secret", "")
    content_type = params.get("content_type", "json")

    url = f"{_git_server_url()}/repos/{repo_owner}/{repo_name}/hooks"
    payload = {
        "type": "forgejo",
        "active": True,
        "events": events,
        "config": {
            "url": target_url,
            "content_type": content_type,
        },
    }
    if secret:
        payload["config"]["secret"] = secret

    response = httpx.post(
        url, json=payload, headers=_git_server_headers(), timeout=_git_api_timeout(),
    )
    data = response.json()

    if response.status_code in (200, 201):
        return {
            "hook_id": data.get("id"),
            "active": data.get("active", False),
        }
    else:
        return {"error": data.get("message", response.text)}


def handle_delete_repo_webhook(params: dict) -> dict:
    """Remove a webhook from a repo via Forgejo API.

    params: {repo_owner, repo_name, hook_id}
    Returns: {deleted: bool}
    """
    repo_owner = params["repo_owner"]
    repo_name = params["repo_name"]
    hook_id = params["hook_id"]

    url = (
        f"{_git_server_url()}/repos/{repo_owner}/{repo_name}"
        f"/hooks/{hook_id}"
    )
    response = httpx.delete(url, headers=_git_server_headers(), timeout=_git_api_timeout())

    if response.status_code in (200, 204):
        return {"deleted": True}
    else:
        data = response.json() if response.text else {}
        return {"deleted": False, "error": data.get("message", response.text)}
