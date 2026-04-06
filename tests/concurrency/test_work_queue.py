"""Concurrency tests for work queue exactly-once semantics.

Tests that work_queue.claim() and work_queue.enqueue() handle concurrent
access correctly:

1. claim() uses SELECT+UPDATE with CAS (WHERE status='pending') for atomicity
2. enqueue() uses INSERT OR IGNORE with UNIQUE constraint on idempotency_key

Both are synchronous (call db.execute() directly), so we use ThreadPoolExecutor
to actually interleave them.
"""

import pytest
from concurrent.futures import ThreadPoolExecutor

from carpenter.core.engine import work_queue
from carpenter.db import get_db


@pytest.mark.concurrency
def test_claim_exactly_once_with_10_workers():
    """10 concurrent workers claim() on 1 item — tests CAS behavior.

    claim() uses SELECT then UPDATE with WHERE status='pending' (CAS).
    The race window:

    Thread 1: SELECT id (gets work_id) -> UPDATE WHERE status='pending' (succeeds)
    Thread 2: SELECT id (gets same work_id) -> UPDATE WHERE status='pending' (0 rows)

    With the rowcount check, Thread 2 returns None when UPDATE matches 0 rows.

    Expected behavior: Exactly 1 claim succeeds, 9 return None.
    """
    # Enqueue a single work item
    work_id = work_queue.enqueue("test.race", {"data": "value"})
    assert work_id is not None

    # Race 10 workers on claim()
    n_workers = 10
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = [executor.submit(work_queue.claim) for _ in range(n_workers)]
        results = [f.result() for f in futures]

    # Count claims
    claimed = [r for r in results if r is not None]
    nones = [r for r in results if r is None]

    # With rowcount check: exactly 1 claim succeeds
    assert len(claimed) == 1, f"Expected exactly 1 claim, got {len(claimed)}"
    assert len(nones) == 9, f"Expected 9 None results, got {len(nones)}"

    # Verify the claimed item
    assert claimed[0]["id"] == work_id, f"Claimed ID {claimed[0]['id']} != enqueued {work_id}"
    assert claimed[0]["status"] == "claimed", f"Claimed item has status {claimed[0]['status']}"

    # Verify DB state: item is claimed exactly once
    item = work_queue.get_item(work_id)
    assert item["status"] == "claimed"


@pytest.mark.concurrency
def test_enqueue_idempotency_concurrent():
    """10 concurrent enqueue() with same idempotency_key — exactly 1 row in DB."""
    idempotency_key = "race-test-key"
    n_workers = 10

    # Race 10 workers on enqueue() with same key
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = [
            executor.submit(
                work_queue.enqueue,
                "test.idem",
                {"attempt": i},
                idempotency_key=idempotency_key,
            )
            for i in range(n_workers)
        ]
        results = [f.result() for f in futures]

    # Exactly one should get a work_id, others get None
    work_ids = [r for r in results if r is not None]
    nones = [r for r in results if r is None]

    assert len(work_ids) == 1, f"Expected exactly 1 enqueue success, got {len(work_ids)}"
    assert len(nones) == 9, f"Expected 9 None results, got {len(nones)}"

    # Verify DB: exactly one row with this idempotency_key
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM work_queue WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchall()
        assert len(rows) == 1, f"Expected 1 row in DB, found {len(rows)}"
        assert rows[0]["status"] == "pending"
    finally:
        db.close()


@pytest.mark.concurrency
def test_claim_multiple_items_distributed():
    """10 workers, 5 items — tests distribution without duplicates.

    With the rowcount check, each item is claimed exactly once.

    Expected behavior:
    - Exactly 5 claims succeed (one per item)
    - 5 workers return None (no items left)
    - All 5 items in DB have status='claimed'
    """
    # Enqueue 5 items
    n_items = 5
    work_ids = [
        work_queue.enqueue("test.distributed", {"order": i})
        for i in range(n_items)
    ]
    assert all(wid is not None for wid in work_ids)

    # Race 10 workers (more than items)
    n_workers = 10
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = [executor.submit(work_queue.claim) for _ in range(n_workers)]
        results = [f.result() for f in futures]

    # Count claims
    claimed = [r for r in results if r is not None]
    nones = [r for r in results if r is None]

    # With rowcount check: exactly 5 claims, no duplicates
    assert len(claimed) == 5, f"Expected exactly 5 claims, got {len(claimed)}"
    assert len(nones) == 5, f"Expected 5 None results, got {len(nones)}"

    # All claimed IDs should be unique and match enqueued IDs
    claimed_ids = [r["id"] for r in claimed]
    assert len(set(claimed_ids)) == 5, f"Expected 5 unique IDs, got {len(set(claimed_ids))}"
    assert set(claimed_ids) == set(work_ids), "Claimed IDs should match enqueued IDs"

    # Verify DB: all 5 items are claimed
    for work_id in work_ids:
        item = work_queue.get_item(work_id)
        assert item["status"] == "claimed", f"Item {work_id} status is {item['status']}, expected claimed"


@pytest.mark.concurrency
def test_enqueue_different_keys_all_succeed():
    """10 concurrent enqueue() with different idempotency_keys — all succeed."""
    n_workers = 10

    # Race 10 workers with DIFFERENT keys
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = [
            executor.submit(
                work_queue.enqueue,
                "test.unique",
                {"attempt": i},
                idempotency_key=f"unique-key-{i}",
            )
            for i in range(n_workers)
        ]
        results = [f.result() for f in futures]

    # All should succeed (no None)
    work_ids = [r for r in results if r is not None]
    nones = [r for r in results if r is None]

    assert len(work_ids) == 10, f"Expected 10 successes, got {len(work_ids)}"
    assert len(nones) == 0, f"Expected 0 None results, got {len(nones)}"

    # Verify DB: 10 distinct rows
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'test.unique'"
        ).fetchall()
        assert len(rows) == 10, f"Expected 10 rows in DB, found {len(rows)}"
    finally:
        db.close()
