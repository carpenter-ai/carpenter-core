"""Platform control work item handler: restart requests."""

import logging

logger = logging.getLogger(__name__)


async def handle_platform_restart(work_id: int, payload: dict) -> None:
    """Handle a platform.restart work item by requesting a restart via main_loop."""
    from ..engine import main_loop

    mode = payload.get("mode", "opportunistic")
    reason = payload.get("reason", "")
    main_loop.set_restart_pending(mode=mode, reason=reason)
    # work_queue.complete() is called automatically by main_loop._run_handler


def register_handlers(register_fn) -> None:
    """Register platform control handlers with the main loop."""
    register_fn("platform.restart", handle_platform_restart)
