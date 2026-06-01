"""Forgejo forge provider — PR, webhook, and review operations.

Implements ``ForgeProvider`` for Forgejo / Gitea.  Body lifted from the
former ``carpenter/tool_backends/forgejo_api.py`` module.

This provider is registered eagerly on import in
``carpenter/forges/__init__.py``.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from .. import config
from ..tool_backends.git import (
    _git_api_long_timeout,
    _git_api_timeout,
    _git_server_headers,
    _git_server_url,
)

logger = logging.getLogger(__name__)


class ForgejoProvider:
    """Forge provider implementation for Forgejo / Gitea."""

    name = "forgejo"

    def __init__(self) -> None:
        # default_base_branch is read on-demand from config so that runtime
        # config edits are picked up immediately.
        pass

    @property
    def default_base_branch(self) -> str:
        return config.CONFIG.get("forge_default_base_branch", "main")

    # ------------------------------------------------------------------
    # PR lifecycle
    # ------------------------------------------------------------------

    def create_pr(self, params: dict) -> dict:
        """Create pull request via Forgejo API.

        params: {repo_owner, repo_name, branch_name, pr_title, pr_body (opt),
                 fork_user, base_branch (opt)}
        Returns: {pr_number, pr_url}
        """
        repo_owner = params["repo_owner"]
        repo_name = params["repo_name"]
        branch_name = params["branch_name"]
        pr_title = params["pr_title"]
        pr_body = params.get("pr_body", "")
        fork_user = params["fork_user"]
        base_branch = params.get("base_branch") or self.default_base_branch

        url = f"{_git_server_url()}/repos/{repo_owner}/{repo_name}/pulls"
        payload = {
            "title": pr_title,
            "head": f"{fork_user}:{branch_name}",
            "base": base_branch,
            "body": pr_body,
        }

        response = httpx.post(
            url, json=payload, headers=_git_server_headers(), timeout=_git_api_timeout()
        )
        data = response.json()

        if response.status_code in (200, 201):
            return {
                "pr_number": data["number"],
                "pr_url": data.get("html_url", ""),
            }
        else:
            return {"error": data.get("message", response.text)}

    def list_prs(self, params: dict) -> dict:
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

    def merge_pr(self, params: dict) -> dict:
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

        response = httpx.post(
            url, json=payload, headers=_git_server_headers(), timeout=_git_api_timeout()
        )

        if response.status_code in (200, 204):
            return {"merged": True}
        else:
            data = response.json() if response.text else {}
            return {"merged": False, "error": data.get("message", response.text)}

    def close_pr(self, params: dict) -> dict:
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

    def get_pr(self, params: dict) -> dict:
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

    def get_pr_diff(self, params: dict) -> dict:
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

    def post_pr_review(self, params: dict) -> dict:
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

    # ------------------------------------------------------------------
    # Repo webhook lifecycle
    # ------------------------------------------------------------------

    def create_repo_webhook(self, params: dict) -> dict:
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

    def delete_repo_webhook(self, params: dict) -> dict:
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

    # ------------------------------------------------------------------
    # Webhook ingress — two parser flavors
    # ------------------------------------------------------------------

    def parse_webhook_legacy(self, data: dict, event_filter: list) -> Optional[dict]:
        """Parse a Forgejo webhook payload (legacy dispatch path).

        Returns a normalized event dict or None if the event should be ignored.
        Lifted from the former
        ``webhook_dispatch_handler._parse_forgejo_payload``.
        """
        action = data.get("action", "")
        event_type = None

        # Determine event type from payload structure
        if "pull_request" in data:
            event_type = "pull_request"
        elif "ref" in data and "commits" in data:
            event_type = "push"
        elif "issue" in data:
            event_type = "issues"
        elif "release" in data:
            event_type = "release"
        else:
            event_type = "unknown"

        # Filter check
        if event_filter and event_type not in event_filter:
            return None

        result = {
            "source_type": "forgejo",
            "event_type": event_type,
            "action": action,
        }

        # Extract PR details
        if event_type == "pull_request":
            pr = data.get("pull_request", {})
            # Only process opened/synchronize/reopened actions
            if action not in ("opened", "synchronize", "reopened", "edited"):
                if event_filter and "pull_request" in event_filter:
                    # User wants PR events but this action isn't interesting
                    return None
            result["pr_number"] = pr.get("number")
            result["pr_title"] = pr.get("title", "")
            result["pr_body"] = pr.get("body", "")
            result["pr_state"] = pr.get("state", "")
            result["pr_user"] = pr.get("user", {}).get("login", "")
            result["head_branch"] = pr.get("head", {}).get("ref", "")
            result["base_branch"] = pr.get("base", {}).get("ref", "")
            result["html_url"] = pr.get("html_url", "")
            # Repo info
            repo = data.get("repository", {})
            result["repo_owner"] = repo.get("owner", {}).get("login", "")
            result["repo_name"] = repo.get("name", "")

        elif event_type == "push":
            result["ref"] = data.get("ref", "")
            result["commits"] = len(data.get("commits", []))

        return result

    def parse_webhook_trigger(
        self, headers: dict, body: dict
    ) -> tuple[str, dict, Optional[str]]:
        """Parse a Forgejo webhook payload (trigger pipeline path).

        Returns ``(event_subtype, parsed_payload, delivery_id)``.  Lifted
        from the former ``core/engine/triggers/webhook._parse_forgejo``.
        """
        event_type, parsed, delivery_id = _parse_webhook_common(
            headers,
            body,
            event_header="x-forgejo-event",
            delivery_header="x-forgejo-delivery",
            event_key="forgejo_event",
        )

        # Forgejo-specific fields
        if "pull_request" in body:
            parsed["pr_state"] = body["pull_request"].get("state", "")
        if "commits" in body:
            parsed["commit_count"] = len(body["commits"])

        return event_type, parsed, delivery_id

    def verify_webhook_signature(
        self, headers: dict, raw_body: bytes, secret: str
    ) -> bool:
        """Verify webhook signature.

        RESERVED SLOT — matches existing behavior (no enforcement).  Real
        HMAC-SHA256 verification against ``X-Forgejo-Signature`` /
        ``X-Gitea-Signature`` is a separate security-gated follow-up PR.

        TODO(D5-followup): implement HMAC-SHA256 verification.
        """
        return True

    # ------------------------------------------------------------------
    # Identity helpers
    # ------------------------------------------------------------------

    def verify_token(self, *, server_url: str, token: str) -> dict:
        """Verify a Forgejo token by hitting ``GET /api/v1/user``.

        Returns ``{"valid": True, "username": str}`` on success or
        ``{"valid": False, "reason": str}`` on failure.
        """
        if not server_url:
            return {"valid": False, "reason": "git_server_url not configured"}

        url = server_url.rstrip("/") + "/api/v1/user"
        try:
            response = httpx.get(
                url,
                headers={"Authorization": f"token {token}"},
                timeout=15.0,
            )
            if response.status_code == 200:
                data = response.json()
                return {
                    "valid": True,
                    "username": data.get("login", ""),
                }
            else:
                return {"valid": False, "reason": f"HTTP {response.status_code}"}
        except (OSError, ValueError, KeyError) as e:
            return {"valid": False, "reason": str(e)}


# ---------------------------------------------------------------------------
# Shared helper for trigger parsing — also usable by future GitHub provider
# ---------------------------------------------------------------------------


def _parse_webhook_common(
    headers: dict,
    body: dict,
    *,
    event_header: str,
    delivery_header: str,
    event_key: str,
) -> tuple[str, dict, Optional[str]]:
    """Shared extraction logic for platform webhook parsers.

    Args:
        headers: Lowercased HTTP headers.
        body: Parsed JSON body.
        event_header: Header name for the event type (e.g. ``x-forgejo-event``).
        delivery_header: Header name for the delivery ID.
        event_key: Key used in the parsed dict to store the event type
                   (e.g. ``forgejo_event``).

    Returns:
        (event_type, parsed_payload, delivery_id)
    """
    event_type = headers.get(event_header, "unknown")
    delivery_id = headers.get(delivery_header)

    parsed: dict = {
        event_key: event_type,
        "delivery_id": delivery_id,
    }

    # Common body fields
    if "action" in body:
        parsed["action"] = body["action"]
    if "repository" in body:
        repo = body["repository"]
        parsed["repo_full_name"] = repo.get("full_name", "")
        parsed["repo_name"] = repo.get("name", "")
    if "sender" in body:
        parsed["sender"] = body["sender"].get("login", "")
    if "pull_request" in body:
        pr = body["pull_request"]
        parsed["pr_number"] = pr.get("number")
        parsed["pr_title"] = pr.get("title", "")
    if "ref" in body:
        parsed["ref"] = body["ref"]

    return event_type, parsed, delivery_id
