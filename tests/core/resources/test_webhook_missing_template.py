"""Subscriptions with an unknown resource_content_type fall back to legacy.

When a webhook subscription sets ``resource_content_type`` to a value
that has no matching ``consumes_content_type`` in the resource template
registry, the Resource-wrap path must fail open:
  - No Resource row is created.
  - A warning is logged.
  - The webhook is still processed by the legacy action path (e.g.
    enqueue_work).
"""

from __future__ import annotations

import logging

import pytest

from carpenter.core.engine import work_queue
from carpenter.core.workflows import webhook_dispatch_handler as handler


@pytest.mark.asyncio
async def test_unknown_content_type_falls_back_to_legacy(caplog):
    """Subscription with bogus resource_content_type runs legacy action."""
    handler.create_subscription(
        webhook_id="bogus-type-hook",
        source_type="generic",
        action_type="enqueue_work",
        action_config={
            "event_type": "fallback.action",
            "payload": {"marker": "yes"},
        },
        resource_content_type="this-content-type-has-no-template",
    )

    from carpenter.db import db_connection
    with db_connection() as db:
        before = db.execute("SELECT COUNT(*) FROM resources").fetchone()[0]

    with caplog.at_level(logging.WARNING, logger=(
        "carpenter.core.workflows.webhook_resource_wrap"
    )):
        await handler.handle_webhook_received(1, {
            "webhook_id": "bogus-type-hook",
            "data": {"body": "anything"},
        })

    with db_connection() as db:
        after = db.execute("SELECT COUNT(*) FROM resources").fetchone()[0]

    # No Resource created when template missing.
    assert before == after

    # Legacy action still ran: enqueue_work dropped a work item on the queue.
    item = work_queue.claim()
    assert item is not None
    assert item["event_type"] == "fallback.action"


@pytest.mark.asyncio
async def test_known_content_type_does_not_fall_back():
    """Sanity: a registered resource_content_type does NOT fall back."""
    handler.create_subscription(
        webhook_id="known-type-hook",
        source_type="generic",
        action_type="enqueue_work",
        action_config={
            "event_type": "should.not.fire",
        },
        resource_content_type="json-webhook",
    )

    await handler.handle_webhook_received(1, {
        "webhook_id": "known-type-hook",
        "data": {"k": "v"},
    })

    # Legacy work item must NOT have been enqueued — the wrap path took
    # over.  enqueue_work's event_type was 'should.not.fire'.  We check
    # the queue holds no such item; the reviewer dispatch
    # ('arc.dispatch') may be present instead.
    found_should_not_fire = False
    while True:
        item = work_queue.claim()
        if item is None:
            break
        if item["event_type"] == "should.not.fire":
            found_should_not_fire = True
    assert not found_should_not_fire
