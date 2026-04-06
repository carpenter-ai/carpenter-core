"""Tests for carpenter.thread_pools — dedicated thread pool management."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from carpenter import config, thread_pools


@pytest.fixture(autouse=True)
def _clean_pools():
    """Ensure pools are shut down after each test."""
    yield
    thread_pools.shutdown_pools()


class TestInitPools:
    def test_creates_both_pools(self):
        thread_pools.init_pools()
        assert isinstance(thread_pools.get_default_pool(), ThreadPoolExecutor)
        assert isinstance(thread_pools.get_work_handler_pool(), ThreadPoolExecutor)

    def test_uses_config_sizes(self, monkeypatch):
        monkeypatch.setitem(config.CONFIG, "default_thread_pool_size", 4)
        monkeypatch.setitem(config.CONFIG, "work_handler_thread_pool_size", 2)
        thread_pools.init_pools()
        assert thread_pools.get_default_pool()._max_workers == 4
        assert thread_pools.get_work_handler_pool()._max_workers == 2

    def test_defaults_without_config(self, monkeypatch):
        # Remove keys if present
        monkeypatch.delitem(config.CONFIG, "default_thread_pool_size", raising=False)
        monkeypatch.delitem(config.CONFIG, "work_handler_thread_pool_size", raising=False)
        thread_pools.init_pools()
        assert thread_pools.get_default_pool()._max_workers == 16
        assert thread_pools.get_work_handler_pool()._max_workers == 3

    def test_reinit_shuts_down_previous(self):
        thread_pools.init_pools()
        pool1 = thread_pools.get_default_pool()
        thread_pools.init_pools()
        pool2 = thread_pools.get_default_pool()
        assert pool1 is not pool2
        # Old pool should be shut down
        assert pool1._shutdown


class TestGetPoolsBeforeInit:
    def test_get_default_pool_raises(self):
        # The conftest autouse fixture init_pools() runs before this test,
        # so we must explicitly shut down pools first.
        thread_pools.shutdown_pools()
        with pytest.raises(RuntimeError, match="not initialised"):
            thread_pools.get_default_pool()

    def test_get_work_handler_pool_raises(self):
        thread_pools.shutdown_pools()
        with pytest.raises(RuntimeError, match="not initialised"):
            thread_pools.get_work_handler_pool()


class TestRunInWorkPool:
    def test_runs_function_in_work_pool(self):
        thread_pools.init_pools()

        import threading
        results = {}

        def capture_thread():
            results["thread"] = threading.current_thread().name
            return 42

        result = asyncio.get_event_loop().run_until_complete(
            thread_pools.run_in_work_pool(capture_thread)
        )
        assert result == 42
        assert "tc-work" in results["thread"]

    def test_passes_args(self):
        thread_pools.init_pools()

        def add(a, b):
            return a + b

        result = asyncio.get_event_loop().run_until_complete(
            thread_pools.run_in_work_pool(add, 3, 7)
        )
        assert result == 10

    def test_passes_kwargs(self):
        thread_pools.init_pools()

        def greet(name, greeting="hello"):
            return f"{greeting} {name}"

        result = asyncio.get_event_loop().run_until_complete(
            thread_pools.run_in_work_pool(greet, "world", greeting="hi")
        )
        assert result == "hi world"

    def test_propagates_exceptions(self):
        thread_pools.init_pools()

        def fail():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            asyncio.get_event_loop().run_until_complete(
                thread_pools.run_in_work_pool(fail)
            )


class TestShutdownPools:
    def test_noop_when_not_initialised(self):
        # Should not raise
        thread_pools.shutdown_pools()

    def test_sets_pools_to_none(self):
        thread_pools.init_pools()
        thread_pools.shutdown_pools()
        with pytest.raises(RuntimeError):
            thread_pools.get_default_pool()

    def test_idempotent(self):
        thread_pools.init_pools()
        thread_pools.shutdown_pools()
        thread_pools.shutdown_pools()  # Should not raise
