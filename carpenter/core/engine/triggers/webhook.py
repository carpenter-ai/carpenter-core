"""Webhook trigger — HTTP endpoint for external events.

Wraps the existing webhook endpoint logic.  Forge-specific payload parsing
lives on each :class:`carpenter.forges.protocol.ForgeProvider`
implementation (``parse_webhook_trigger``).  Users configure which parser
to use via the per-trigger ``parser:`` config key.

On request: parse payload → emit structured event with idempotency key
extracted from webhook headers.
"""

import json
import logging

from ....forges import get_forge_provider
from .base import EndpointTrigger

logger = logging.getLogger(__name__)


# Built-in generic parser — providers register themselves under named
# keys (forgejo / github / ...).  Generic stays inline since it has no
# forge to abstract over.


def _parse_generic(headers: dict, body: dict) -> tuple[str, dict, str | None]:
    """Generic parser — passes through the raw body."""
    return "generic", {"data": body}, None


def _parse(parser_name: str, headers: dict, body: dict) -> tuple[str, dict, str | None]:
    """Resolve a parser by name and dispatch.

    ``"generic"`` uses the built-in passthrough parser.  Other names are
    looked up in the forge-provider registry and dispatched to
    ``parse_webhook_trigger``.  Unknown names fall back to generic.
    """
    if parser_name == "generic":
        return _parse_generic(headers, body)
    provider = get_forge_provider(parser_name)
    if provider is None:
        return _parse_generic(headers, body)
    return provider.parse_webhook_trigger(headers, body)


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

        try:
            event_subtype, parsed_payload, delivery_id = _parse(parser_name, headers, body)
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
