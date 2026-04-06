"""Dedicated thread pools for the platform.

Separates long-running work-handler threads (API calls that block for
minutes) from the default pool used by asyncio.to_thread() for short
interactive operations.  This prevents work handlers from starving HTTP
request handling and chat response threads.

Usage::

    from . import thread_pools

    # In coordinator.start():
    thread_pools.init_pools()
    loop.set_default_executor(thread_pools.get_default_pool())

    # In work handlers:
    result = await thread_pools.run_in_work_pool(blocking_fn, arg1, kw=val)

    # In coordinator.stop():
    thread_pools.shutdown_pools()
"""

import asyncio
import functools
import logging
from concurrent.futures import ThreadPoolExecutor

from . import config

logger = logging.getLogger(__name__)

_default_pool: ThreadPoolExecutor | None = None
_work_handler_pool: ThreadPoolExecutor | None = None


def init_pools() -> None:
    """Create the default and work-handler thread pools.

    Reads pool sizes from config (``default_thread_pool_size`` and
    ``work_handler_thread_pool_size``).  Safe to call multiple times;
    existing pools are shut down first.
    """
    global _default_pool, _work_handler_pool

    # Tear down any previous pools (e.g. during test resets)
    shutdown_pools()

    default_size = config.CONFIG.get("default_thread_pool_size", 16)
    work_size = config.CONFIG.get("work_handler_thread_pool_size", 3)

    _default_pool = ThreadPoolExecutor(
        max_workers=default_size, thread_name_prefix="tc-default",
    )
    _work_handler_pool = ThreadPoolExecutor(
        max_workers=work_size, thread_name_prefix="tc-work",
    )
    logger.info(
        "Thread pools initialised: default=%d, work-handler=%d",
        default_size, work_size,
    )


def get_default_pool() -> ThreadPoolExecutor:
    """Return the default thread pool (general purpose)."""
    if _default_pool is None:
        raise RuntimeError("Thread pools not initialised — call init_pools() first")
    return _default_pool


def get_work_handler_pool() -> ThreadPoolExecutor:
    """Return the work-handler thread pool (long-running API calls)."""
    if _work_handler_pool is None:
        raise RuntimeError("Thread pools not initialised — call init_pools() first")
    return _work_handler_pool


async def run_in_work_pool(fn, *args, **kwargs):
    """Run *fn* in the work-handler pool and return the result.

    Equivalent to ``asyncio.to_thread(fn, *args, **kwargs)`` but routed
    through the dedicated work-handler pool instead of the default executor.
    """
    loop = asyncio.get_running_loop()
    call = functools.partial(fn, *args, **kwargs)
    return await loop.run_in_executor(get_work_handler_pool(), call)


def shutdown_pools() -> None:
    """Shut down both pools (no-op if not initialised)."""
    global _default_pool, _work_handler_pool

    for name, pool in [("default", _default_pool), ("work-handler", _work_handler_pool)]:
        if pool is not None:
            try:
                pool.shutdown(wait=False)
            except RuntimeError as _exc:
                logger.exception("Error shutting down %s thread pool", name)

    _default_pool = None
    _work_handler_pool = None
