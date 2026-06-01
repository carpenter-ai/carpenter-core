"""Weekly Resource sweep — delete blobs of deprecated rows past their age window.

The Resource abstraction keeps rows forever as provenance tombstones, but
the blobs on disk are reclaimable once a Resource is deprecated (meaning
it was consumed by a trusted producer, or otherwise marked obsolete), has
no pin, and is past any ``retain_until`` window.

Crash-safety ordering: DB first, disk second.

    1. SELECT candidate rows (deprecated, unpinned, past retain, not already
       deleted, with a file_path).
    2. UPDATE resources SET deleted_at=now, file_path=NULL WHERE id=? AND
       deleted_at IS NULL  (idempotent).
    3. COMMIT.
    4. ``os.unlink`` the blob, ignoring ENOENT.
    5. ``os.rmdir`` the ``<resource_id>/`` subdir, ignoring ENOTEMPTY/ENOENT.

A crash between step 3 and step 4 leaves an orphan file on disk; the DB
row already says ``file_path=NULL`` and ``deleted_at=now``, so the next
sweep will not find it as a candidate.  Orphan blobs are discoverable
(directory exists for a deleted row) and can be cleaned up out-of-band.

The inverse ordering — unlink first — would leave a window where the row
still claims ``file_path=<path>`` but the file is already gone; consumers
would then see ``FileNotFoundError`` surprises on read.  We accept disk
orphans over DB lies.

Registration: ``register_weekly_sweep()`` below is called by the platform
coordinator during the trigger/subscription pipeline init.  It seeds a
timer cron entry and a main-loop handler that invokes :func:`run_sweep`.
The generic ``_builtin.timer_forward`` subscription (seeded by
``load_builtin_subscriptions``) handles the timer.fired → work_queue
routing for all cron-emitted events, so no feature-specific subscription
is needed.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ...db import db_connection, db_transaction

logger = logging.getLogger(__name__)

# Event type emitted by the weekly cron entry and consumed by the sweep handler.
SWEEP_EVENT_TYPE = "resources.sweep"

# Event type emitted AFTER a sweep completes, carrying the summary dict.
SWEEP_COMPLETED_EVENT_TYPE = "resources.swept"

# Cron schedule: 03:00 UTC every Sunday.
SWEEP_CRON_SCHEDULE = "0 3 * * 0"

# Cron entry name (used as a stable dedup key).
SWEEP_TRIGGER_NAME = "resources.weekly_sweep"


def _default_age_days() -> int:
    """Return the configured sweep age window in days (default 7)."""
    from ... import config as _config

    value = _config.CONFIG.get("resource_sweep_age_days", 7)
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(
            "resource_sweep_age_days=%r is not int-like; falling back to 7",
            value,
        )
        return 7


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def run_sweep(
    *,
    age_days: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Sweep deprecated Resources whose blobs are older than ``age_days``.

    Candidate rows satisfy ALL of the following:

    - ``deleted_at IS NULL``          (not already swept)
    - ``file_path IS NOT NULL``       (something on disk to reclaim)
    - ``pinned = 0``                  (not protected by an explicit pin)
    - ``retain_until IS NULL OR retain_until < now``  (past retention window)
    - ``deprecated_at IS NOT NULL AND deprecated_at < now - age_days``

    Args:
        age_days: Override the ``resource_sweep_age_days`` config value
            (default 7).  Explicit 0 is allowed (sweeps everything
            deprecated before ``now``).
        now: Override the current time (for testability).  Defaults to
            ``datetime.now(UTC)``.

    Returns:
        ``{
            "candidates": <int>,
            "files_deleted": <int>,
            "file_errors": [(resource_id, error_str), ...],
        }``

        ``candidates`` is the number of rows selected in step 1.
        ``files_deleted`` counts successful ``os.unlink`` calls *or*
        ENOENT (missing-on-disk is treated as a successful delete — the
        file is already gone, which is exactly the post-condition we
        want).  ``file_errors`` collects any other ``OSError``.
    """
    if age_days is None:
        age_days = _default_age_days()
    if age_days < 0:
        raise ValueError(f"age_days must be >= 0, got {age_days}")

    cutoff = (now or _utcnow()) - timedelta(days=age_days)
    now_iso = (now or _utcnow()).isoformat()

    # Step 1: collect candidate (id, file_path) pairs.
    # SQLite's CURRENT_TIMESTAMP is UTC in ISO-ish form (no tzinfo) so we
    # pass our own now/cutoff as ISO strings to keep comparisons consistent.
    with db_connection() as db:
        rows = db.execute(
            "SELECT id, file_path FROM resources "
            "WHERE deleted_at IS NULL "
            "  AND file_path IS NOT NULL "
            "  AND pinned = 0 "
            "  AND (retain_until IS NULL OR retain_until < ?) "
            "  AND deprecated_at IS NOT NULL "
            "  AND deprecated_at < ? ",
            (now_iso, cutoff.isoformat()),
        ).fetchall()
        candidates = [(r["id"], r["file_path"]) for r in rows]

    summary: dict = {
        "candidates": len(candidates),
        "files_deleted": 0,
        "file_errors": [],
    }

    if not candidates:
        logger.info("resources.sweep: %s", summary)
        return summary

    # Step 2+3: flip DB state first (one transaction per row keeps the
    # window small — if we crash mid-batch only the unswept tail is left
    # for the next run).
    committed_ids: list[tuple[int, str]] = []
    for rid, path in candidates:
        try:
            with db_transaction() as db:
                cursor = db.execute(
                    "UPDATE resources "
                    "SET deleted_at = ?, file_path = NULL "
                    "WHERE id = ? AND deleted_at IS NULL",
                    (now_iso, rid),
                )
                if cursor.rowcount > 0:
                    committed_ids.append((rid, path))
        except Exception as exc:  # noqa: BLE001 — keep sweep going on per-row errors
            logger.exception(
                "resources.sweep: failed to flip DB state for resource %d", rid,
            )
            summary["file_errors"].append((rid, f"db:{exc}"))

    # Step 4+5: unlink blobs.  DB is already committed; the only
    # consequence of a failure here is an orphan file.
    for rid, path in committed_ids:
        try:
            os.unlink(path)
            summary["files_deleted"] += 1
        except FileNotFoundError:
            # Already gone — counts as a successful sweep.
            summary["files_deleted"] += 1
            logger.debug(
                "resources.sweep: resource %d file already missing: %s",
                rid, path,
            )
        except OSError as exc:
            logger.warning(
                "resources.sweep: failed to unlink resource %d path %s: %s",
                rid, path, exc,
            )
            summary["file_errors"].append((rid, f"unlink:{exc}"))
            continue

        # Best-effort: remove the per-resource directory if empty.
        try:
            parent = Path(path).parent
            os.rmdir(parent)
        except FileNotFoundError:
            pass
        except OSError:
            # ENOTEMPTY or permission issue — leave the dir alone.
            pass

    logger.info("resources.sweep: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Trigger / subscription / handler wiring
# ---------------------------------------------------------------------------


async def handle_sweep_work_item(work_id: int, payload: dict) -> None:
    """Main-loop work-item handler for ``resources.sweep``.

    Invokes :func:`run_sweep` and emits a ``resources.swept`` event with
    the summary.  ``work_id`` and ``payload`` are accepted for the
    standard handler signature but are not otherwise used — the sweep
    picks up its own config.
    """
    from ..engine import event_bus

    summary = run_sweep()
    # Convert the tuple errors to JSON-friendly lists for the payload.
    emit_payload = {
        "candidates": summary["candidates"],
        "files_deleted": summary["files_deleted"],
        "file_errors": [
            {"resource_id": rid, "error": err}
            for rid, err in summary["file_errors"]
        ],
    }
    event_bus.record_event(
        SWEEP_COMPLETED_EVENT_TYPE,
        emit_payload,
        source="resources.sweep",
    )


def register_weekly_sweep(register_handler) -> None:
    """Install the cron entry and work-item handler.

    Called once per process from ``Coordinator._init_trigger_subscription_pipeline``.
    Idempotent — the cron entry is deduped by name and the work-item
    handler overwrites any prior registration.

    The generic ``_builtin.timer_forward`` subscription (seeded by
    ``subscriptions.load_builtin_subscriptions``) already routes every
    ``timer.fired`` event to the work_queue using the cron entry's
    ``event_type``.  So once we add a cron entry whose ``event_type`` is
    ``resources.sweep``, the existing pipeline delivers it to our handler
    without a feature-specific subscription.

    Args:
        register_handler: The ``main_loop.register_handler`` callable
            (passed in to avoid a hard import cycle).
    """
    from ..engine import trigger_manager

    # 1. Seed the cron entry.  add_cron() raises sqlite3.IntegrityError on
    #    the UNIQUE(name) constraint if we've already registered — that is
    #    expected on every subsequent process start.
    try:
        trigger_manager.add_cron(
            name=SWEEP_TRIGGER_NAME,
            cron_expr=SWEEP_CRON_SCHEDULE,
            event_type=SWEEP_EVENT_TYPE,
            event_payload=None,
        )
        logger.info(
            "resources.sweep: registered cron %s -> %s",
            SWEEP_CRON_SCHEDULE, SWEEP_EVENT_TYPE,
        )
    except Exception as exc:  # noqa: BLE001 — IntegrityError, ValueError, etc.
        msg = str(exc).lower()
        if "unique" in msg or "already" in msg:
            logger.debug("resources.sweep: cron already registered")
        else:
            logger.exception("resources.sweep: failed to register cron")

    # 2. Register the work-item handler.
    register_handler(SWEEP_EVENT_TYPE, handle_sweep_work_item)
    logger.info("resources.sweep: registered work-item handler")
