"""Registry of running executor subprocess.Popen objects.

Tracks all active executor processes so they can be signalled during
platform shutdown. register/unregister are called from thread pool
workers (one Popen per call); signal_all/kill_all are called from the
asyncio thread during shutdown after no new registrations will happen.
Python's GIL protects set.add/discard for these access patterns.
"""

import logging

logger = logging.getLogger(__name__)

_running: set = set()


def register(proc):
    """Add a Popen object to the running set."""
    _running.add(proc)


def unregister(proc):
    """Remove a Popen object from the running set."""
    _running.discard(proc)


def signal_all():
    """Send SIGTERM to all registered processes (non-blocking)."""
    for proc in list(_running):
        try:
            proc.terminate()
        except OSError:
            pass  # Process already exited


def kill_all():
    """Send SIGKILL to all registered processes that are still alive."""
    for proc in list(_running):
        try:
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass  # Process already exited


def count():
    """Return the number of registered processes."""
    return len(_running)
