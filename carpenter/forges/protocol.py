"""Forge provider protocol — abstraction over a git-forge SaaS/self-hosted instance.

Defines the narrow surface every forge provider (Forgejo, GitHub, ...) must
implement.  Pure-git operations (clone, branch, commit, push, fetch, rebase)
are NOT in this protocol — they live in ``tool_backends/git.py`` and are
forge-agnostic.  The provider only owns the *forge API* layer (PRs, reviews,
webhooks) and webhook-payload parsing.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol


class ForgeEvent:
    """Normalized webhook event shape.

    Lightweight container: providers may return a plain ``dict`` instead.
    Callers should treat the parsed payload as a dict.
    """

    source_type: str
    event_type: str
    action: str
    delivery_id: Optional[str]
    repo_owner: str
    repo_name: str
    pr_number: Optional[int]
    pr_title: str
    pr_body: str
    pr_state: str
    head_branch: str
    base_branch: str
    html_url: str
    raw: dict


class ForgeProvider(Protocol):
    """Abstraction over a git-forge SaaS/self-hosted instance."""

    name: str
    default_base_branch: str

    # ---- PR lifecycle (return shape: dict, error key on failure) ----
    def create_pr(self, params: dict) -> dict: ...
    def list_prs(self, params: dict) -> dict: ...
    def get_pr(self, params: dict) -> dict: ...
    def get_pr_diff(self, params: dict) -> dict: ...
    def merge_pr(self, params: dict) -> dict: ...
    def close_pr(self, params: dict) -> dict: ...
    def post_pr_review(self, params: dict) -> dict: ...

    # ---- Repo webhook lifecycle ----
    def create_repo_webhook(self, params: dict) -> dict: ...
    def delete_repo_webhook(self, params: dict) -> dict: ...

    # ---- Webhook ingress ----
    def parse_webhook_legacy(self, data: dict, event_filter: list) -> Optional[dict]:
        """Parse a webhook payload for the legacy dispatch path.

        Used by ``carpenter/core/workflows/webhook_dispatch_handler.py``.
        Returns a normalized dict keyed by ``source_type``/``event_type`` etc.
        Returns ``None`` if the event is filtered out.
        """
        ...

    def parse_webhook_trigger(self, headers: dict, body: dict) -> tuple[str, dict, Optional[str]]:
        """Parse a webhook payload for the trigger pipeline.

        Used by ``carpenter/core/engine/triggers/webhook.py``.  Returns
        ``(event_subtype, parsed_payload, delivery_id)``.
        """
        ...

    def verify_webhook_signature(
        self, headers: dict, raw_body: bytes, secret: str
    ) -> bool:
        """Verify the HMAC signature on an incoming webhook.

        RESERVED SLOT — today this matches existing behavior (effectively
        ``return True``).  Real enforcement is a separate, security-gated
        follow-up PR.
        """
        ...

    # ---- Identity helpers ----
    def verify_token(self, *, server_url: str, token: str) -> dict:
        """Verify a token by hitting the forge's user/identity endpoint."""
        ...
