"""Tests for carpenter.core.work_queue."""

import json
import pytest

from carpenter.core.engine import work_queue


def test_enqueue_returns_id():
    """enqueue returns an integer work item ID."""
    wid = work_queue.enqueue("test.event", {"key": "value"})
    assert isinstance(wid, int)
    assert wid > 0


def test_enqueue_idempotency_key_prevents_duplicate():
    """Second enqueue with same idempotency_key returns None."""
    wid1 = work_queue.enqueue("test.event", {"a": 1}, idempotency_key="unique-1")
    wid2 = work_queue.enqueue("test.event", {"b": 2}, idempotency_key="unique-1")
    assert wid1 is not None
    assert wid2 is None


def test_claim_returns_oldest_pending():
    """claim returns the oldest pending item."""
    wid1 = work_queue.enqueue("first", {"order": 1})
    wid2 = work_queue.enqueue("second", {"order": 2})
    item = work_queue.claim()
    assert item["id"] == wid1
    assert item["status"] == "claimed"
    assert item["event_type"] == "first"


def test_claim_empty_queue_returns_none():
    """claim returns None when queue is empty."""
    assert work_queue.claim() is None


def test_complete_marks_item_done():
    """complete sets status to 'complete' with timestamp."""
    wid = work_queue.enqueue("test.event", {})
    work_queue.claim()
    work_queue.complete(wid)
    item = work_queue.get_item(wid)
    assert item["status"] == "complete"
    assert item["completed_at"] is not None


def test_fail_retries_when_under_limit():
    """fail requeues item as pending when retries remain."""
    wid = work_queue.enqueue("test.event", {}, max_retries=3)
    work_queue.claim()
    work_queue.fail(wid, "transient error")
    item = work_queue.get_item(wid)
    assert item["status"] == "pending"
    assert item["retry_count"] == 1
    assert item["error"] == "transient error"


def test_fail_dead_letters_at_max_retries():
    """fail moves item to dead_letter when retries exhausted."""
    wid = work_queue.enqueue("test.event", {}, max_retries=1)
    work_queue.claim()
    work_queue.fail(wid, "permanent error")
    item = work_queue.get_item(wid)
    assert item["status"] == "dead_letter"
    assert item["retry_count"] == 1


def test_get_dead_letter_items():
    """get_dead_letter_items returns all dead-lettered items."""
    wid = work_queue.enqueue("test.event", {}, max_retries=1)
    work_queue.claim()
    work_queue.fail(wid, "fatal")
    items = work_queue.get_dead_letter_items()
    assert len(items) == 1
    assert items[0]["id"] == wid


def test_claimed_item_not_reclaimed():
    """A claimed item is not returned by a second claim call."""
    work_queue.enqueue("test.event", {"a": 1})
    item1 = work_queue.claim()
    item2 = work_queue.claim()
    assert item1 is not None
    assert item2 is None
