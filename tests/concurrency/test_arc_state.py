"""Concurrency tests for arc state and status updates.

Tests two race conditions:

1. set_arc_state() uses atomic INSERT ON CONFLICT UPDATE (row-level upsert)
   — should be safe from lost updates

2. update_status() uses SELECT then UPDATE with no optimistic locking
   — may expose lost-update bugs under concurrent writes

Both are synchronous, so we use ThreadPoolExecutor to interleave them.
"""

import pytest
from concurrent.futures import ThreadPoolExecutor

from carpenter.core.arcs import manager
from carpenter.core.workflows import _arc_state
from carpenter.db import get_db


@pytest.mark.concurrency
def test_set_arc_state_concurrent_different_keys():
    """10 concurrent set_arc_state() on same arc with different keys — all 10 keys present.

    This should pass because set_arc_state() uses atomic row-level upsert:
    INSERT ... ON CONFLICT(arc_id, key) DO UPDATE ...
    Each key is independent, so concurrent inserts don't conflict.
    """
    # Create an arc
    arc_id = manager.create_arc("test-arc", "test goal")

    # Race 10 workers setting different keys
    n_workers = 10

    def set_key(i):
        _arc_state.set_arc_state(arc_id, f"key_{i}", f"value_{i}")

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = [executor.submit(set_key, i) for i in range(n_workers)]
        for f in futures:
            f.result()  # Wait for all to complete

    # Verify all 10 keys exist
    # Note: arc creation may add system keys (e.g., _retry_policy), so we check
    # that our keys are present, not that there are exactly 10 total keys.
    db = get_db()
    try:
        rows = db.execute(
            "SELECT key, value_json FROM arc_state WHERE arc_id = ? ORDER BY key",
            (arc_id,),
        ).fetchall()
        keys = [r["key"] for r in rows]
        # Check that all our test keys are present
        test_keys = [f"key_{i}" for i in range(10)]
        missing = [k for k in test_keys if k not in keys]
        assert len(missing) == 0, f"Missing keys: {missing}. Found keys: {keys}"
    finally:
        db.close()


@pytest.mark.concurrency
def test_set_arc_state_concurrent_same_key():
    """10 concurrent set_arc_state() on same arc/key — last write wins, no errors.

    This tests that concurrent updates to the SAME key don't crash or corrupt.
    The upsert is atomic at the row level, so exactly one final value should
    survive. We can't predict which value (race condition), but we verify:
    - No exceptions
    - Exactly 1 row for the key
    - The value is one of the 10 attempted writes
    """
    arc_id = manager.create_arc("test-arc", "test goal")
    key = "shared_key"

    # Race 10 workers setting the same key to different values
    n_workers = 10

    def set_value(i):
        _arc_state.set_arc_state(arc_id, key, f"value_{i}")

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = [executor.submit(set_value, i) for i in range(n_workers)]
        for f in futures:
            f.result()  # Wait for all to complete

    # Verify exactly 1 row exists
    db = get_db()
    try:
        rows = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = ?",
            (arc_id, key),
        ).fetchall()
        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"

        # Value should be one of the 10 attempts (we can't predict which)
        import json
        value = json.loads(rows[0]["value_json"])
        expected = [f"value_{i}" for i in range(10)]
        assert value in expected, f"Value {value} not in expected set {expected}"
    finally:
        db.close()


@pytest.mark.concurrency
def test_update_status_concurrent_race():
    """10 concurrent update_status() on same arc — CAS prevents race condition.

    update_status() uses optimistic locking (CAS pattern):
    - SELECT status
    - Validate transition
    - UPDATE WHERE id=? AND status=old_status
    - Check rowcount — if 0, status changed (stale state error)

    Under concurrent writes:
    Thread 1: SELECT (sees pending) -> UPDATE succeeds (rowcount=1)
    Thread 2: SELECT (sees pending) -> UPDATE fails (rowcount=0, status already active)
    ...
    Thread 10: SELECT (sees pending) -> UPDATE fails (rowcount=0, status already active)

    Expected behavior: Exactly 1 success, 9 stale-state errors.
    """
    # Create an arc in pending status
    arc_id = manager.create_arc("test-arc", "test goal")

    # Verify initial state
    arc = manager.get_arc(arc_id)
    assert arc["status"] == "pending"

    # Race 10 workers trying to transition pending -> active
    n_workers = 10

    def update_once():
        try:
            manager.update_status(arc_id, "active")
            return ("ok", None)
        except Exception as e:
            return ("error", str(e))

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = [executor.submit(update_once) for _ in range(n_workers)]
        results = [f.result() for f in futures]

    # Count successes and errors
    successes = [r for r in results if r[0] == "ok"]
    errors = [r for r in results if r[0] == "error"]

    # With CAS, exactly 1 should succeed
    assert len(successes) == 1, (
        f"Expected exactly 1 success with CAS, got {len(successes)}"
    )
    assert len(errors) == 9, (
        f"Expected 9 failures with CAS, got {len(errors)}"
    )
    assert len(successes) + len(errors) == 10, (
        f"Expected 10 total results, got {len(successes)} + {len(errors)}"
    )

    # Verify the errors are either:
    # 1. CAS stale-state errors (SELECT pending, but UPDATE found active)
    # 2. Validation errors (SELECT active after another thread committed)
    # Both are correct — CAS prevents the race, validation errors are legitimate
    for status, err in errors:
        assert (
            "status changed during update" in err
            or "Invalid transition: active -> active" in err
        ), f"Expected CAS or validation error, got: {err}"

    # Verify final state
    arc = manager.get_arc(arc_id)
    assert arc["status"] == "active"

    # Verify history: should have exactly 1 status_changed entry
    history = manager.get_history(arc_id)
    status_changes = [
        h for h in history if h["entry_type"] == "status_changed"
    ]
    assert len(status_changes) == 1, (
        f"Expected 1 status_changed entry, got {len(status_changes)}"
    )


@pytest.mark.concurrency
def test_update_status_different_arcs():
    """10 concurrent update_status() on different arcs — all should succeed.

    This is a control test: updates to different arcs don't interfere.
    """
    # Create 10 arcs
    arc_ids = [
        manager.create_arc(f"arc-{i}", "test goal")
        for i in range(10)
    ]

    # Race 10 workers, each updating a different arc
    def update_arc(arc_id):
        try:
            manager.update_status(arc_id, "active")
            return ("ok", arc_id)
        except Exception as e:
            return ("error", (arc_id, e))

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(update_arc, aid) for aid in arc_ids]
        results = [f.result() for f in futures]

    # All should succeed
    successes = [r for r in results if r[0] == "ok"]
    errors = [r for r in results if r[0] == "error"]

    assert len(successes) == 10, (
        f"Expected 10 successes, got {len(successes)}. "
        f"Errors: {[str(e[1]) for e in errors]}"
    )

    # Verify all arcs are active
    for arc_id in arc_ids:
        arc = manager.get_arc(arc_id)
        assert arc["status"] == "active", f"Arc {arc_id} status is {arc['status']}, expected active"
