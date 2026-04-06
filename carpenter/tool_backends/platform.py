"""Platform tool backend — handles platform management callbacks."""
import logging

from ..core.engine import work_queue

logger = logging.getLogger(__name__)


def handle_request_restart(params: dict) -> dict:
    """Request a platform restart. Params: mode (opt), reason (opt)."""
    mode = params.get("mode", "opportunistic")
    reason = params.get("reason", "")
    # Map "urgent" to "graceful" for the internal mode name
    internal_mode = "graceful" if mode == "urgent" else "opportunistic"
    work_queue.enqueue(
        "platform.restart",
        {"mode": internal_mode, "reason": reason},
        idempotency_key=f"restart-{internal_mode}",
    )
    if mode == "opportunistic":
        return {"status": "queued", "detail": "Platform will restart when idle (no active arcs)."}
    else:
        return {"status": "initiated", "detail": "Draining in-flight work, then restarting."}
