"""Sweep wiring tests.

Verify the three-part hookup installed by ``register_weekly_sweep``:

1. The cron entry (``resources.weekly_sweep``) is seeded with the right
   schedule and target event type.
2. The main-loop handler is registered against ``resources.sweep``.
3. End-to-end: a ``timer.fired`` event carrying our cron event type is
   routed through the generic ``_builtin.timer_forward`` subscription
   into the work_queue as a ``resources.sweep`` item.

These tests exercise the subscription processing pipeline synchronously;
they do not spin up the async main loop.
"""

from datetime import datetime, timezone

import pytest

from carpenter.core.engine import (
    main_loop,
    subscriptions,
    trigger_manager,
    work_queue,
)
from carpenter.core.engine import event_bus
from carpenter.core.resources import sweep as res_sweep
from carpenter.core.resources.sweep import (
    SWEEP_CRON_SCHEDULE,
    SWEEP_EVENT_TYPE,
    SWEEP_TRIGGER_NAME,
    register_weekly_sweep,
)


@pytest.fixture
def fresh_pipeline():
    """Reset subscriptions / handlers so each test starts clean."""
    subscriptions.reset()
    main_loop._handlers.clear()
    yield
    subscriptions.reset()
    main_loop._handlers.clear()


def test_cron_entry_registered(fresh_pipeline):
    register_weekly_sweep(main_loop.register_handler)

    entry = trigger_manager.get_cron(SWEEP_TRIGGER_NAME)
    assert entry is not None, "cron entry must be seeded"
    assert entry["cron_expr"] == SWEEP_CRON_SCHEDULE
    assert entry["event_type"] == SWEEP_EVENT_TYPE
    assert entry["enabled"]  # truthy — stored as 1 in SQLite


def test_handler_is_registered(fresh_pipeline):
    register_weekly_sweep(main_loop.register_handler)

    handler = main_loop.get_handler(SWEEP_EVENT_TYPE)
    assert handler is not None
    assert handler is res_sweep.handle_sweep_work_item


def test_register_is_idempotent(fresh_pipeline):
    # Two calls must not raise and must not duplicate the cron entry.
    register_weekly_sweep(main_loop.register_handler)
    register_weekly_sweep(main_loop.register_handler)

    entries = [e for e in trigger_manager.list_cron() if e["name"] == SWEEP_TRIGGER_NAME]
    assert len(entries) == 1

    handler = main_loop.get_handler(SWEEP_EVENT_TYPE)
    assert handler is res_sweep.handle_sweep_work_item


def test_timer_fired_event_routes_to_work_queue(fresh_pipeline):
    """End-to-end slice: cron-style timer.fired → work_queue[resources.sweep].

    We rely on the generic ``_builtin.timer_forward`` subscription (seeded
    by ``load_builtin_subscriptions``) to route any ``timer.fired`` event
    whose payload carries ``cron_event_type=resources.sweep`` into the
    work queue.  No feature-specific subscription is needed.
    """
    subscriptions.load_builtin_subscriptions()
    register_weekly_sweep(main_loop.register_handler)

    # Simulate what check_cron() would emit when our sweep entry fires.
    cron_entry = trigger_manager.get_cron(SWEEP_TRIGGER_NAME)
    assert cron_entry is not None

    fire_time = datetime.now(timezone.utc).isoformat()
    payload = {
        "cron_id": cron_entry["id"],
        "cron_name": cron_entry["name"],
        "cron_event_type": SWEEP_EVENT_TYPE,
        "fire_time": fire_time,
    }
    event_bus.record_event(
        trigger_manager.TIMER_FIRED_EVENT,
        payload,
        source=f"cron:{cron_entry['name']}",
        idempotency_key=f"cron-{cron_entry['id']}-{fire_time}",
    )

    actions = subscriptions.process_subscriptions()
    assert actions >= 1

    # A work item with event_type=resources.sweep should now exist.
    from carpenter.db import db_connection
    with db_connection() as db:
        row = db.execute(
            "SELECT id, event_type, status FROM work_queue "
            "WHERE event_type = ? ORDER BY id DESC LIMIT 1",
            (SWEEP_EVENT_TYPE,),
        ).fetchone()
    assert row is not None, "timer.fired should have been forwarded to work_queue"
    assert row["event_type"] == SWEEP_EVENT_TYPE
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_handler_runs_sweep_and_emits_completed_event(
    fresh_pipeline, tmp_path,
):
    """Invoking the work-item handler runs run_sweep() and emits resources.swept."""
    from carpenter.core.resources import manager as res_manager
    from carpenter.db import db_connection, db_transaction
    from datetime import timedelta

    # Prepare a sweepable resource so the handler has something to do.
    rid = res_manager.create_resource(
        content_type="text/plain", file_path=None, produced_by_arc_id=None,
    )
    d = tmp_path / "resources" / str(rid)
    d.mkdir(parents=True, exist_ok=True)
    blob = d / "blob"
    blob.write_bytes(b"data")
    old = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).isoformat()
    with db_transaction() as db:
        db.execute(
            "UPDATE resources SET file_path = ?, deprecated_at = ? "
            "WHERE id = ?",
            (str(blob), old, rid),
        )

    register_weekly_sweep(main_loop.register_handler)
    handler = main_loop.get_handler(SWEEP_EVENT_TYPE)

    await handler(work_id=999, payload={})

    # Resource was swept.
    row = res_manager.get_resource(rid)
    assert row["deleted_at"] is not None
    assert row["file_path"] is None

    # resources.swept event recorded.
    with db_connection() as db:
        event = db.execute(
            "SELECT event_type, payload_json FROM events "
            "WHERE event_type = 'resources.swept' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert event is not None
    import json as _json
    emitted = _json.loads(event["payload_json"])
    assert emitted["candidates"] == 1
    assert emitted["files_deleted"] == 1
    assert emitted["file_errors"] == []
