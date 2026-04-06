"""Tests for carpenter.core.main_loop."""

import asyncio
import json
from unittest.mock import patch

import pytest

from carpenter.core.engine import main_loop, work_queue


@pytest.fixture(autouse=True)
def clear_handlers():
    """Clear handler registry, in-flight tasks, and recreate wake_signal for each test."""
    main_loop._handlers.clear()
    main_loop._in_flight.clear()
    main_loop.wake_signal = asyncio.Event()
    yield
    main_loop._handlers.clear()
    main_loop._in_flight.clear()


@pytest.mark.asyncio
async def test_run_loop_processes_work_item():
    """Main loop processes a pending work item via registered handler."""
    results = []

    async def test_handler(work_id, payload):
        results.append(payload)

    main_loop.register_handler("test.event", test_handler)
    work_queue.enqueue("test.event", {"msg": "hello"})

    shutdown = asyncio.Event()

    async def stop_after_processing():
        # Give the loop one iteration to process
        await asyncio.sleep(0.1)
        shutdown.set()
        main_loop.wake_signal.set()

    asyncio.create_task(stop_after_processing())
    await main_loop.run_loop(shutdown_event=shutdown)

    assert len(results) == 1
    assert results[0]["msg"] == "hello"


@pytest.mark.asyncio
async def test_run_loop_stops_on_shutdown():
    """Main loop exits when shutdown_event is set."""
    shutdown = asyncio.Event()
    shutdown.set()  # Immediate shutdown
    main_loop.wake_signal.set()  # Don't wait for heartbeat

    await main_loop.run_loop(shutdown_event=shutdown)
    # If we reach here, the loop exited properly


@pytest.mark.asyncio
async def test_wake_signal_wakes_loop():
    """Setting wake_signal causes the loop to process immediately."""
    results = []

    async def test_handler(work_id, payload):
        results.append(True)

    main_loop.register_handler("wake.test", test_handler)

    shutdown = asyncio.Event()

    async def enqueue_and_wake():
        await asyncio.sleep(0.05)
        work_queue.enqueue("wake.test", {})
        main_loop.wake_signal.set()
        await asyncio.sleep(0.1)
        shutdown.set()
        main_loop.wake_signal.set()

    asyncio.create_task(enqueue_and_wake())
    await main_loop.run_loop(shutdown_event=shutdown)

    assert len(results) == 1


@pytest.mark.asyncio
async def test_handler_error_fails_work_item():
    """Handler errors mark the work item as failed."""
    async def failing_handler(work_id, payload):
        raise RuntimeError("something broke")

    main_loop.register_handler("fail.test", failing_handler)
    wid = work_queue.enqueue("fail.test", {}, max_retries=1)

    shutdown = asyncio.Event()

    async def stop():
        await asyncio.sleep(0.1)
        shutdown.set()
        main_loop.wake_signal.set()

    asyncio.create_task(stop())
    await main_loop.run_loop(shutdown_event=shutdown)

    item = work_queue.get_item(wid)
    assert item["error"] == "something broke"
    assert item["retry_count"] == 1


def test_register_and_get_handler():
    """register_handler and get_handler work correctly."""
    async def my_handler(work_id, payload):
        pass

    main_loop.register_handler("my.event", my_handler)
    assert main_loop.get_handler("my.event") is my_handler
    assert main_loop.get_handler("nonexistent") is None


# ── Parallel dispatch tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_handlers_run_concurrently():
    """Multiple dispatched handlers run concurrently, not sequentially."""
    barrier = asyncio.Barrier(3)
    results = []

    async def barrier_handler(work_id, payload):
        # All 3 handlers must reach the barrier before any can proceed.
        # If handlers ran sequentially, the first would block forever.
        await asyncio.wait_for(barrier.wait(), timeout=2)
        results.append(work_id)

    main_loop.register_handler("barrier.test", barrier_handler)
    for _ in range(3):
        work_queue.enqueue("barrier.test", {})

    await main_loop._dispatch_work_items(max_handlers=4)

    # 3 tasks should be in-flight
    assert len(main_loop._in_flight) == 3

    # Wait for all to finish
    await asyncio.gather(*main_loop._in_flight.values())
    assert len(results) == 3


@pytest.mark.asyncio
async def test_concurrency_bounded_by_config():
    """No more than max_concurrent_handlers run at once."""
    active = []
    peak_active = [0]
    gate = asyncio.Event()

    async def counting_handler(work_id, payload):
        active.append(work_id)
        peak_active[0] = max(peak_active[0], len(active))
        await gate.wait()
        active.remove(work_id)

    main_loop.register_handler("count.test", counting_handler)
    for _ in range(4):
        work_queue.enqueue("count.test", {})

    # Dispatch with max=2
    dispatched = await main_loop._dispatch_work_items(max_handlers=2)
    assert dispatched == 2
    assert len(main_loop._in_flight) == 2

    # Yield so handlers start and record themselves in active
    await asyncio.sleep(0)

    assert peak_active[0] == 2

    # Let first batch finish
    gate.set()
    await asyncio.gather(*list(main_loop._in_flight.values()))

    # Dispatch remaining
    dispatched2 = await main_loop._dispatch_work_items(max_handlers=2)
    assert dispatched2 == 2

    await asyncio.sleep(0)
    await asyncio.gather(*list(main_loop._in_flight.values()))


@pytest.mark.asyncio
async def test_error_isolation_between_handlers():
    """One handler failing does not affect another handler's success."""
    results = []

    async def good_handler(work_id, payload):
        await asyncio.sleep(0.01)
        results.append("ok")

    async def bad_handler(work_id, payload):
        raise ValueError("boom")

    main_loop.register_handler("good.test", good_handler)
    main_loop.register_handler("bad.test", bad_handler)

    good_wid = work_queue.enqueue("good.test", {})
    bad_wid = work_queue.enqueue("bad.test", {})

    await main_loop._dispatch_work_items(max_handlers=4)
    await asyncio.gather(*main_loop._in_flight.values(), return_exceptions=True)

    assert results == ["ok"]
    good_item = work_queue.get_item(good_wid)
    bad_item = work_queue.get_item(bad_wid)
    assert good_item["status"] == "complete"
    assert bad_item["error"] == "boom"


@pytest.mark.asyncio
async def test_graceful_shutdown_waits_for_in_flight():
    """Shutdown waits for in-flight handlers to complete rather than cancelling."""
    completed = []

    async def slow_handler(work_id, payload):
        await asyncio.sleep(0.3)
        completed.append(work_id)

    main_loop.register_handler("slow.test", slow_handler)
    work_queue.enqueue("slow.test", {})

    shutdown = asyncio.Event()

    async def trigger_shutdown():
        await asyncio.sleep(0.1)  # Handler is running but not finished
        shutdown.set()
        main_loop.wake_signal.set()

    asyncio.create_task(trigger_shutdown())
    await main_loop.run_loop(shutdown_event=shutdown)

    # Handler should have completed despite shutdown
    assert len(completed) == 1


@pytest.mark.asyncio
async def test_wake_signal_dispatches_new_items():
    """New items enqueued while handlers are running get dispatched on wake."""
    results = []
    first_started = asyncio.Event()

    async def handler(work_id, payload):
        if payload.get("phase") == "first":
            first_started.set()
        results.append(payload.get("phase"))

    main_loop.register_handler("phase.test", handler)
    work_queue.enqueue("phase.test", {"phase": "first"})

    shutdown = asyncio.Event()

    async def enqueue_second():
        await first_started.wait()
        await asyncio.sleep(0.05)  # Let first handler complete
        work_queue.enqueue("phase.test", {"phase": "second"})
        main_loop.wake_signal.set()
        await asyncio.sleep(0.15)
        shutdown.set()
        main_loop.wake_signal.set()

    asyncio.create_task(enqueue_second())
    await main_loop.run_loop(shutdown_event=shutdown)

    assert "first" in results
    assert "second" in results


@pytest.mark.asyncio
async def test_in_flight_tracking():
    """_in_flight dict tracks active tasks and clears on completion."""
    gate = asyncio.Event()

    async def gated_handler(work_id, payload):
        await gate.wait()

    main_loop.register_handler("track.test", gated_handler)
    work_queue.enqueue("track.test", {})

    await main_loop._dispatch_work_items(max_handlers=4)
    assert len(main_loop._in_flight) == 1

    # Let handler finish
    gate.set()
    await asyncio.gather(*main_loop._in_flight.values())

    # Give done_callback a chance to fire
    await asyncio.sleep(0)
    assert len(main_loop._in_flight) == 0


@pytest.mark.asyncio
async def test_done_callback_sets_wake_signal():
    """Handler completion sets wake_signal so the loop refills the slot immediately."""
    async def fast_handler(work_id, payload):
        pass  # completes instantly

    main_loop.register_handler("wake.done", fast_handler)
    work_queue.enqueue("wake.done", {})

    main_loop.wake_signal.clear()
    await main_loop._dispatch_work_items(max_handlers=4)

    # Wait for the handler task to finish and its done_callback to fire
    await asyncio.gather(*main_loop._in_flight.values())
    await asyncio.sleep(0)  # let done_callback execute

    assert main_loop.wake_signal.is_set(), "wake_signal should be set after handler completes"


@pytest.mark.asyncio
async def test_shutdown_kills_remaining_after_drain_timeout(monkeypatch):
    """When drain times out, process_registry.kill_all() is called."""
    import carpenter.config
    monkeypatch.setitem(carpenter.config.CONFIG, "shutdown_timeout", 2)

    gate = asyncio.Event()

    async def blocking_handler(work_id, payload):
        await gate.wait()  # Will never unblock — forces drain timeout

    main_loop.register_handler("block.test", blocking_handler)
    work_queue.enqueue("block.test", {})

    shutdown = asyncio.Event()

    async def trigger_shutdown():
        await asyncio.sleep(0.05)
        shutdown.set()
        main_loop.wake_signal.set()

    with patch("carpenter.executor.process_registry.kill_all") as mock_kill:
        asyncio.create_task(trigger_shutdown())

        # shutdown_timeout=2 → drain_timeout = 2-5 = -3 → clamped to 1s
        await asyncio.wait_for(
            main_loop.run_loop(shutdown_event=shutdown),
            timeout=5,
        )

        mock_kill.assert_called_once()

    # Clean up blocked handler tasks
    gate.set()
    for task in list(main_loop._in_flight.values()):
        task.cancel()
    await asyncio.gather(*main_loop._in_flight.values(), return_exceptions=True)
