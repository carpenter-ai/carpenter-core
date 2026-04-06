"""Shared fixtures for concurrency tests."""

import asyncio
import pytest
from concurrent.futures import ThreadPoolExecutor


@pytest.fixture
def thread_pool():
    """Provide a ThreadPoolExecutor for running sync functions concurrently."""
    executor = ThreadPoolExecutor(max_workers=20)
    yield executor
    executor.shutdown(wait=True)


@pytest.fixture
def async_workers():
    """Helper to run multiple sync workers concurrently using threads.

    Returns a function that takes (callable, N) and runs the callable
    N times in parallel via ThreadPoolExecutor, returning results.
    """
    def _run(func, n_workers):
        """Run func() N times concurrently in threads."""
        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            futures = [executor.submit(func) for _ in range(n_workers)]
            return [f.result() for f in futures]
    return _run
