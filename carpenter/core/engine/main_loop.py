"""Main event loop for Carpenter.

Async loop with wake signal + heartbeat. Dispatches work item handlers
as concurrent asyncio tasks, bounded by max_concurrent_handlers config.

Chat messages and other urgent events use wake_signal.set() to
bypass the heartbeat delay for near-instant processing.
"""

import asyncio
import json
import logging
import os
import sys
from typing import Any

from ... import config
from . import work_queue, event_bus, trigger_manager, subscriptions

logger = logging.getLogger(__name__)

# Global wake signal — set() to wake the loop immediately
wake_signal = asyncio.Event()

# Handler registry: event_type → async callable(work_id, payload)
_handlers: dict[str, Any] = {}

# Heartbeat hooks — called every main loop iteration (every ~5 seconds)
_heartbeat_hooks: list = []

# In-flight handler tasks: work_id → asyncio.Task
_in_flight: dict[int, asyncio.Task] = {}

# Restart state
_restart_pending: bool = False
_restart_mode: str = "opportunistic"  # "opportunistic" | "graceful"

# Reference to the shutdown_event created in http.py lifespan (set in run_loop)
_shutdown_event: asyncio.Event | None = None


def set_restart_pending(mode: str = "opportunistic", reason: str = "") -> None:
    """Request a platform restart.

    mode='opportunistic': restart when idle (no active arcs, no in-flight handlers).
    mode='graceful': drain in-flight work then restart immediately.
    """
    global _restart_pending, _restart_mode
    _restart_pending = True
    _restart_mode = mode
    wake_signal.set()  # wake the loop immediately
    logger.info("Restart requested (mode=%s): %s", mode, reason or "no reason given")


def _do_restart() -> None:
    """Replace current process with a fresh copy (works with or without systemd)."""
    from ...platform import get_platform
    get_platform().restart_process()


def _check_restart() -> None:
    """Heartbeat hook: execute pending restart when conditions are met."""
    global _restart_pending
    if not _restart_pending:
        return

    if _restart_mode == "graceful":
        # Signal the main loop to stop; http.py lifespan will call _do_restart after drain.
        if _shutdown_event is not None:
            _shutdown_event.set()
        return

    # Opportunistic: only restart when truly idle
    from ...db import get_db, db_connection
    with db_connection() as db:
        active = db.execute(
            "SELECT COUNT(*) FROM arcs WHERE status='active'"
        ).fetchone()[0]

    if active == 0 and not _in_flight:
        _restart_pending = False  # clear flag before exec (child process starts clean)
        _do_restart()  # os.execv — does not return


def register_heartbeat_hook(hook):
    """Register a function to be called every heartbeat cycle.

    Hooks are plain callables (not async). They should be fast and
    non-blocking — used for lightweight checks like mtime polling,
    health monitoring, and periodic cleanup.
    """
    _heartbeat_hooks.append(hook)


def register_handler(event_type: str, handler):
    """Register a handler function for an event type."""
    _handlers[event_type] = handler


def get_handler(event_type: str):
    """Get the registered handler for an event type, or None."""
    return _handlers.get(event_type)


async def _run_handler(work_id: int, event_type: str, payload: dict):
    """Execute a single work item handler with completion/failure tracking."""
    handler = get_handler(event_type)
    if handler is None:
        logger.warning("No handler for event type: %s", event_type)
        work_queue.fail(work_id, f"No handler for event type: {event_type}")
        return

    logger.debug("Handler start: work_id=%d event_type=%s", work_id, event_type)
    try:
        await handler(work_id, payload)
        work_queue.complete(work_id)
        logger.debug("Handler completed: work_id=%d", work_id)
    except Exception as e:  # broad catch: handler may raise anything
        logger.exception("Error processing work item %d (%s)", work_id, event_type)
        work_queue.fail(work_id, str(e))


async def _dispatch_work_items(max_handlers: int = 4):
    """Claim and dispatch pending work items as concurrent tasks.

    Returns the number of items dispatched (not completed).
    """
    dispatched = 0
    while len(_in_flight) < max_handlers:
        item = work_queue.claim()
        if item is None:
            break

        work_id = item["id"]
        event_type = item["event_type"]
        payload = json.loads(item["payload_json"])

        task = asyncio.create_task(_run_handler(work_id, event_type, payload))

        def _done_callback(t, wid=work_id):
            _in_flight.pop(wid, None)
            wake_signal.set()

        task.add_done_callback(_done_callback)
        _in_flight[work_id] = task
        dispatched += 1

    return dispatched


async def _process_events():
    """Match new events against registered matchers."""
    return event_bus.process_events()


async def _check_timeouts():
    """Check for expired event matchers."""
    return event_bus.check_timeouts()


async def _check_cron():
    """Check for due cron entries."""
    return trigger_manager.check_cron()


async def _check_triggers():
    """Check pollable triggers."""
    from .triggers import registry
    return registry.check_pollable_triggers()


async def _process_subscriptions():
    """Match events against persistent subscriptions."""
    return subscriptions.process_subscriptions()


async def run_loop(*, shutdown_event: asyncio.Event | None = None):
    """Run the main event loop.

    Args:
        shutdown_event: If provided, the loop stops when this event is set.
            If None, the loop runs until cancelled.
    """
    global _shutdown_event
    _shutdown_event = shutdown_event

    heartbeat = config.CONFIG.get("heartbeat_seconds", 5)
    max_handlers = config.CONFIG.get("max_concurrent_handlers", 4)
    logger.info("Main loop starting (heartbeat=%ds, max_handlers=%d)", heartbeat, max_handlers)

    # Register restart check as a heartbeat hook (idempotent — only once per process)
    if _check_restart not in _heartbeat_hooks:
        _heartbeat_hooks.append(_check_restart)

    while True:
        if shutdown_event and shutdown_event.is_set():
            logger.info("Shutdown signal received, stopping main loop")
            break

        wake_signal.clear()

        try:
            await _dispatch_work_items(max_handlers)
            # Subscriptions must run BEFORE one-shot matchers because
            # process_events() marks events as processed=TRUE, and
            # process_subscriptions() filters on processed=FALSE.
            # Running subscriptions first ensures timer.fired events
            # are routed to the work_queue before being marked processed.
            await _process_subscriptions()
            await _process_events()
            await _check_timeouts()
            await _check_cron()
            await _check_triggers()
            for hook in _heartbeat_hooks:
                try:
                    hook()
                except Exception:  # broad catch: hook may raise anything
                    logger.exception("Error in heartbeat hook")
        except Exception:  # broad catch: dispatch/events may raise anything
            logger.exception("Error in main loop iteration")

        try:
            await asyncio.wait_for(wake_signal.wait(), timeout=heartbeat)
        except asyncio.TimeoutError:
            pass  # Normal heartbeat cycle

    # Drain in-flight handlers on shutdown
    if _in_flight:
        logger.info("Waiting for %d in-flight handler(s) to complete", len(_in_flight))
        try:
            drain_timeout = config.CONFIG.get("shutdown_timeout", 25) - 5
            await asyncio.wait_for(
                asyncio.gather(*_in_flight.values(), return_exceptions=True),
                timeout=max(drain_timeout, 1),
            )
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for in-flight handlers, killing executor processes")
            from ...executor import process_registry
            process_registry.kill_all()
            for task in _in_flight.values():
                task.cancel()
            await asyncio.gather(*_in_flight.values(), return_exceptions=True)
