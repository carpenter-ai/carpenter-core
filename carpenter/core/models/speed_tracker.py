"""Speed tracker — compute measured model speeds from api_calls latency data.

Queries the api_calls table for recent latency data, computes median
seconds-per-ktok-output per model, and updates the model registry with
the measured speeds.

Called from the daily reflection hook to keep registry data fresh.
"""

import logging
from datetime import datetime, timedelta, timezone

from statistics import median as _median

from ...db import get_db, db_connection

logger = logging.getLogger(__name__)


def compute_measured_speeds(days: int = 7) -> dict[str, float]:
    """Query api_calls for latency data and compute median s/ktok output per model.

    Only considers calls with both latency_ms and output_tokens > 0.
    Uses data from the last ``days`` days.

    Args:
        days: Number of days of data to consider.

    Returns:
        Dict mapping model string (e.g., "claude-opus-4-6") to
        measured speed in seconds per ktok output.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with db_connection() as db:
        rows = db.execute(
            "SELECT model, latency_ms, output_tokens FROM api_calls "
            "WHERE latency_ms IS NOT NULL "
            "AND output_tokens > 0 "
            "AND created_at >= ? "
            "ORDER BY model, latency_ms",
            (cutoff,),
        ).fetchall()

    if not rows:
        return {}

    # Group by model and compute s/ktok
    model_speeds: dict[str, list[float]] = {}
    for row in rows:
        model = row["model"]
        latency_s = row["latency_ms"] / 1000.0
        output_ktok = row["output_tokens"] / 1000.0
        if output_ktok > 0:
            speed = latency_s / output_ktok  # seconds per ktok output
            model_speeds.setdefault(model, []).append(speed)

    # Compute medians
    result = {}
    for model, speeds in model_speeds.items():
        result[model] = round(_median(speeds), 3)

    return result


def update_registry_speeds() -> int:
    """Compute speeds from recent data and update the model registry.

    Returns:
        Number of models updated.
    """
    from .registry import get_registry, get_entry_by_model_id, update_measured_speed

    speeds = compute_measured_speeds()
    if not speeds:
        return 0

    updated = 0
    registry = get_registry()

    for model_str, speed in speeds.items():
        # Try to match by model_id (strip provider prefix if present)
        entry = get_entry_by_model_id(model_str)
        if entry:
            update_measured_speed(entry.key, speed)
            updated += 1
            logger.debug(
                "Updated speed for %s: %.3f s/ktok output",
                entry.key, speed,
            )

    if updated:
        logger.info("Updated measured speeds for %d model(s)", updated)

    return updated
