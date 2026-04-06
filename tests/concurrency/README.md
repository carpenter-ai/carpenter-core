# Concurrency Tests

This directory contains tests for race conditions and concurrent access patterns in Carpenter core.

## Purpose

These tests document and verify the behavior of Carpenter's core concurrency-sensitive code paths. They expose actual race conditions in the current implementation and will fail (or change behavior) when those races are fixed.

## Test Files

### `test_work_queue.py`

Tests work queue exactly-once semantics under concurrent access.

**Race condition**: `claim()` uses SELECT+UPDATE with WHERE status='pending' (CAS), but doesn't check rowcount. This allows multiple threads to return the same item even though only one actually updates the status.

- `test_claim_exactly_once_with_10_workers`: 10 workers race on 1 item. Due to the race, 1-3 workers may return the item (all the same ID).
- `test_claim_multiple_items_distributed`: 10 workers race on 5 items. Due to race, 5-10 claimed results (some duplicates).
- `test_enqueue_idempotency_concurrent`: 10 workers enqueue with same idempotency_key. INSERT OR IGNORE correctly prevents duplicates (exactly 1 row).
- `test_enqueue_different_keys_all_succeed`: Control test — different keys don't interfere.

**Expected fixes**:
- Check `rowcount` after UPDATE in `claim()` and return None if 0 rows affected
- Or use `RETURNING *` clause (SQLite 3.35+) to atomic UPDATE+SELECT

### `test_arc_state.py`

Tests arc state updates and status transitions under concurrent access.

**Race condition**: `update_status()` does SELECT then UPDATE with no optimistic locking. Under concurrent writes, the SELECT+UPDATE window causes validation errors.

- `test_set_arc_state_concurrent_different_keys`: 10 workers set different keys. INSERT ON CONFLICT UPDATE is atomic — all 10 keys present (PASS).
- `test_set_arc_state_concurrent_same_key`: 10 workers set same key to different values. Last write wins, no corruption (PASS).
- `test_update_status_concurrent_race`: 10 workers transition pending->active. First wins, rest fail with "Invalid transition: active -> active". Documents the race condition.
- `test_update_status_different_arcs`: Control test — updates to different arcs don't interfere.

**Expected fixes**:
- Add optimistic locking (version column)
- Or use serializable transaction isolation
- Or restructure to use atomic UPDATE with subquery

## Implementation Details

All tests use `ThreadPoolExecutor` to run synchronous functions concurrently (work_queue and arc state APIs are sync).

Concurrency level: N=10 workers (enough to expose races without overloading the Pi).

## Stability

These tests are timing-dependent by nature. They run reliably on the Pi 4 but may behave differently on faster hardware or under different load. The tolerance ranges (e.g., "1-3 claimed results") account for timing variance while still catching regressions.

## Running

```bash
~/bin/run-tests tests/concurrency/ -v
```

**Important**: Always use `~/bin/run-tests`, never `pytest` directly. The wrapper sets `TMPDIR=/dev/shm` to avoid SD card I/O storms.

## When to Update

These tests should be updated when:
1. The underlying race conditions are fixed (tests will fail)
2. Implementation changes affect concurrency patterns
3. Tolerance ranges prove too narrow (flaky failures) or too wide (not catching bugs)
