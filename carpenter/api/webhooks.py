"""Webhook API endpoint for external trigger integration.

Routes incoming webhooks through the unified trigger/event pipeline.
Instead of directly recording events and enqueuing work items, this
module uses a WebhookTrigger to emit events into the event bus. A
built-in subscription then routes those events to the webhook dispatch
handler via the work queue.

Flow:
  HTTP POST /api/webhooks/{webhook_id}
    -> WebhookTrigger.emit() records event in event bus
    -> wake main loop
    -> process_subscriptions() matches "webhook.received" event
    -> creates work_queue item for webhook_dispatch_handler
"""

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..core.engine.main_loop import wake_signal
from ..core.engine.triggers.base import Trigger

logger = logging.getLogger(__name__)


class _ApiWebhookTrigger(Trigger):
    """Internal trigger used by the webhook API endpoint.

    Not a full EndpointTrigger (no HTTP path registration needed — the
    Starlette route is managed separately). This is a thin wrapper that
    provides emit() with proper trigger metadata for the event bus.
    """

    @classmethod
    def trigger_type(cls) -> str:
        return "api_webhook"

    def emit_webhook(self, webhook_id: str, data: dict) -> int | None:
        """Emit a webhook.received event with structured payload.

        Args:
            webhook_id: The webhook identifier from the URL path.
            data: The parsed JSON body of the webhook request.

        Returns:
            Event ID, or None if duplicate (idempotency key matched).
        """
        payload = {
            "webhook_id": webhook_id,
            "data": data,
        }
        return self.emit(
            event_type="webhook.received",
            payload=payload,
        )


# Module-level trigger instance, shared across all webhook requests
_trigger = _ApiWebhookTrigger(name="api-webhooks", config={})


async def handle_webhook(request: Request):
    """Receive a webhook and route through the trigger/event pipeline.

    The webhook_id identifies which webhook was triggered.
    The request body becomes the event payload.

    Emits a 'webhook.received' event via the trigger pipeline. A
    built-in subscription (registered at startup) routes these events
    to the webhook dispatch handler through the work queue.
    """
    webhook_id = request.path_params["webhook_id"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, KeyError, ValueError) as _exc:
        body = {}

    # Emit event through the trigger pipeline
    event_id = _trigger.emit_webhook(webhook_id, body)

    # Wake the main loop so subscriptions are processed promptly
    wake_signal.set()

    logger.info("Webhook %s received: event_id=%s", webhook_id, event_id)

    return JSONResponse(content={"event_id": event_id, "webhook_id": webhook_id})


# Built-in subscription config that routes webhook.received events to
# the webhook dispatch handler via the work queue.  Registered by the
# coordinator at startup alongside other subscription configs.
WEBHOOK_DISPATCH_SUBSCRIPTION = {
    "name": "webhook-dispatch",
    "on": "webhook.received",
    "action": {
        "type": "enqueue_work",
        "event_type": "webhook.received",
        "payload_merge": True,
    },
}


routes = [
    Route("/api/webhooks/{webhook_id}", handle_webhook, methods=["POST"]),
]
