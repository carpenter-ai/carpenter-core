"""Webhook trigger — HTTP endpoint for external events.

Wraps the existing webhook endpoint logic. Ships with built-in payload
parsers for Forgejo and GitHub. Users can configure which parser to use.

On request: parse payload → emit structured event with idempotency key
extracted from webhook headers.
"""

import json
import logging

from .base import EndpointTrigger

logger = logging.getLogger(__name__)


# Built-in payload parsers


def _parse_webhook_common(
    headers: dict,
    body: dict,
    *,
    event_header: str,
    delivery_header: str,
    event_key: str,
) -> tuple[str, dict, str | None]:
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


def _parse_forgejo(headers: dict, body: dict) -> tuple[str, dict, str | None]:
    """Parse a Forgejo webhook payload.

    Returns:
        (event_subtype, parsed_payload, delivery_id)
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


def _parse_github(headers: dict, body: dict) -> tuple[str, dict, str | None]:
    """Parse a GitHub webhook payload.

    Returns:
        (event_subtype, parsed_payload, delivery_id)
    """
    return _parse_webhook_common(
        headers,
        body,
        event_header="x-github-event",
        delivery_header="x-github-delivery",
        event_key="github_event",
    )


def _parse_generic(headers: dict, body: dict) -> tuple[str, dict, str | None]:
    """Generic parser — passes through the raw body."""
    return "generic", {"data": body}, None


_PARSERS = {
    "forgejo": _parse_forgejo,
    "github": _parse_github,
    "generic": _parse_generic,
}


class WebhookTrigger(EndpointTrigger):
    """HTTP webhook endpoint trigger.

    Config:
        parser: parser name ('forgejo', 'github', 'generic')
        emits: base event type (e.g., 'webhook.forgejo')
        path_suffix: optional path suffix (default: trigger name)
    """

    @classmethod
    def trigger_type(cls) -> str:
        return "webhook"

    @property
    def path(self) -> str:
        suffix = self.config.get("path_suffix", self.name)
        return f"/triggers/{suffix}"

    async def handle_request(self, request) -> dict:
        """Parse webhook request and emit event."""
        parser_name = self.config.get("parser", "generic")
        emits = self.config.get("emits", f"webhook.{self.name}")

        # Get headers as lowercase dict
        headers = {k.lower(): v for k, v in request.headers.items()}

        # Parse body
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}

        # Get parser
        parser = _PARSERS.get(parser_name, _parse_generic)

        try:
            event_subtype, parsed_payload, delivery_id = parser(headers, body)
        except Exception:
            logger.exception("Webhook parser %s failed for trigger %s", parser_name, self.name)
            event_subtype = "error"
            parsed_payload = {"raw": body}
            delivery_id = None

        # Build idempotency key from delivery ID
        if delivery_id:
            idempotency_key = f"webhook-{self.name}-{delivery_id}"
        else:
            idempotency_key = None

        event_id = self.emit(
            event_type=emits,
            payload=parsed_payload,
            idempotency_key=idempotency_key,
        )

        return {
            "event_id": event_id,
            "trigger": self.name,
            "event_subtype": event_subtype,
        }
