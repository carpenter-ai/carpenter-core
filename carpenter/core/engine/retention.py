"""Daily platform-DB retention sweep.

Ages out log-like rows in ``platform.db`` past a configurable per-table
retention window.  The predicates mirror the terminal-state semantics
that the rest of the code already uses, so we only ever delete rows
that are safe to drop:

- ``events``: only ``processed=1`` rows (unprocessed events are still
  work-in-flight for ``subscriptions.process_subscriptions()``).
- ``work_queue``: only terminal statuses (``completed``, ``complete``,
  ``failed``, ``cancelled``, ``dead_letter``) — never ``pending`` or
  ``claimed``.
- ``arc_history`` / ``arc_state``: only for arcs whose ``status`` is
  in ``FROZEN_STATUSES`` and whose ``updated_at`` is past the cutoff.
  We keep the ``arcs`` row itself as a lineage tombstone.
- ``tool_calls`` / ``messages``: only rows tied to conversations that
  are ``archived=1`` and haven't received a message since the cutoff.
  ``tool_calls`` are deleted first to preserve the FK on ``messages``.

  NOTE: ``messages`` retention is a *no-op* until conversations get
  archived upstream.  If you want message pruning to actually do
  work, tune conversation archival separately — this sweep will not
  touch messages that live under an unarchived conversation, no
  matter how old the messages are.

Registration mirrors ``carpenter/core/resources/sweep.py``: a daily
cron entry emits ``db.retention.sweep`` via the generic
``_builtin.timer_forward`` subscription, and a work-item handler
runs :func:`run_retention_sweep`.

Safety features:

- **dry_run**: log projected per-table counts and return without
  executing any DELETE.  Wired via config (``db_retention.dry_run``).
- **Per-table enabled**: each table can be individually disabled
  without disabling the whole sweep.
- **retention_days = 0**: treated as *disabled* (safer than
  interpreting as "delete everything").
- **Batched deletes**: ``DELETE ... WHERE id IN (SELECT id ... LIMIT ?)``
  in short transactions until no rows remain.  SQLite doesn't ship
  ``DELETE ... LIMIT`` by default, so the subquery form is required.
- **Global enabled toggle**: ``db_retention.enabled = false`` disables
  everything (returns an empty summary).
- **VACUUM opt-in**: ``vacuum_after=True`` runs ``VACUUM`` after a
  successful sweep.  Off by default — SQLite reuses freed pages, so
  file size only shrinks after VACUUM or after freed pages get
  overwritten by later inserts.  The snapshot rsync deduplicator
  benefits from the page-content changes even without VACUUM.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ...db import db_connection, db_transaction

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Event type emitted by the daily cron entry and consumed by the sweep handler.
RETENTION_EVENT_TYPE = "db.retention.sweep"

# Event type emitted AFTER a sweep completes, carrying the summary dict.
RETENTION_COMPLETED_EVENT_TYPE = "db.retention.swept"

# Cron schedule: 04:00 UTC daily (an hour after the 03:00 snapshot).
RETENTION_CRON_SCHEDULE = "0 4 * * *"

# Cron entry name (stable dedup key).
RETENTION_TRIGGER_NAME = "db.retention.daily_sweep"

# Terminal work_queue statuses that are safe to prune.  Mirrors the values
# actually written by ``main_loop`` and ``work_queue`` on completion / failure.
_TERMINAL_WORK_STATUSES = ("complete", "completed", "failed", "cancelled", "dead_letter")

# Arc terminal statuses.  Kept literal here (rather than imported from
# ``arcs.manager``) to avoid a hard import cycle during startup.  Must be
# kept in sync with ``FROZEN_STATUSES`` in ``carpenter/core/arcs/manager.py``.
_ARC_FROZEN_STATUSES = ("completed", "failed", "cancelled", "escalated")

# The full set of tables this sweeper knows how to prune.  Config layers
# a per-table ``{enabled, retention_days}`` on top; unknown tables in
# config are ignored.
KNOWN_TABLES: tuple[str, ...] = (
    "events",
    "work_queue",
    "arc_history",
    "arc_state",
    "tool_calls",
    "messages",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _cfg() -> dict:
    """Return the ``db_retention`` config block (with defaults applied)."""
    from ... import config as _config

    cfg = _config.CONFIG.get("db_retention") or {}
    if not isinstance(cfg, dict):
        logger.warning(
            "db_retention config is not a dict (%r); ignoring", type(cfg).__name__,
        )
        return {}
    return cfg


def _table_cfg(table: str, cfg: dict) -> tuple[bool, int]:
    """Extract ``(enabled, retention_days)`` for one table.

    Returns ``(False, 0)`` if the table isn't configured, or if
    ``retention_days`` is missing / not int-like / <= 0.
    """
    tables = cfg.get("tables") or {}
    entry = tables.get(table) or {}
    if not isinstance(entry, dict):
        return False, 0
    enabled = bool(entry.get("enabled", False))
    raw_days = entry.get("retention_days", 0)
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        logger.warning(
            "db_retention.tables.%s.retention_days=%r is not int-like; treating as disabled",
            table, raw_days,
        )
        return False, 0
    if days <= 0:
        # retention_days == 0 (or negative) is treated as disabled — safer
        # than "delete everything at cutoff=now".
        return False, 0
    return enabled, days


# ---------------------------------------------------------------------------
# Per-table predicates
# ---------------------------------------------------------------------------
#
# Each entry defines the WHERE clause used to select prunable rows.  The
# ``id_select`` string must SELECT the primary key column (``id``) and
# must reference the ``:cutoff`` bind parameter.  The batch DELETE
# statement is then::
#
#     DELETE FROM {table} WHERE id IN ({id_select} LIMIT :limit)
#
# We keep the SQL literal here (rather than assembling it dynamically) so
# each predicate is auditable end-to-end.


_PREDICATES: dict[str, str] = {
    "events": (
        "SELECT id FROM events "
        "WHERE processed = 1 "
        "  AND created_at < :cutoff"
    ),
    "work_queue": (
        "SELECT id FROM work_queue "
        "WHERE status IN "
        + "(" + ", ".join(f"'{s}'" for s in _TERMINAL_WORK_STATUSES) + ") "
        "  AND COALESCE(completed_at, created_at) < :cutoff"
    ),
    "arc_history": (
        "SELECT ah.id FROM arc_history ah "
        "JOIN arcs a ON a.id = ah.arc_id "
        "WHERE a.status IN "
        + "(" + ", ".join(f"'{s}'" for s in _ARC_FROZEN_STATUSES) + ") "
        "  AND COALESCE(a.updated_at, a.created_at) < :cutoff"
    ),
    "arc_state": (
        "SELECT s.id FROM arc_state s "
        "JOIN arcs a ON a.id = s.arc_id "
        "WHERE a.status IN "
        + "(" + ", ".join(f"'{s}'" for s in _ARC_FROZEN_STATUSES) + ") "
        "  AND COALESCE(a.updated_at, a.created_at) < :cutoff"
    ),
    "tool_calls": (
        "SELECT tc.id FROM tool_calls tc "
        "JOIN conversations c ON c.id = tc.conversation_id "
        "WHERE c.archived = 1 "
        "  AND COALESCE(c.last_message_at, c.started_at) < :cutoff "
        "  AND tc.created_at < :cutoff"
    ),
    "messages": (
        "SELECT m.id FROM messages m "
        "JOIN conversations c ON c.id = m.conversation_id "
        "WHERE c.archived = 1 "
        "  AND COALESCE(c.last_message_at, c.started_at) < :cutoff "
        "  AND m.created_at < :cutoff"
    ),
}

# Explicit deletion order.  ``tool_calls`` MUST run before ``messages`` so
# that we don't leave orphan FKs pointing at a since-deleted message row.
# ``arc_state`` before ``arc_history`` is arbitrary — neither depends on
# the other — but we fix an order for test reproducibility.
_TABLE_ORDER: tuple[str, ...] = (
    "events",
    "work_queue",
    "arc_history",
    "arc_state",
    "tool_calls",
    "messages",
)


# ---------------------------------------------------------------------------
# Core sweep
# ---------------------------------------------------------------------------


def run_retention_sweep(
    *,
    now: datetime | None = None,
    dry_run_override: bool | None = None,
) -> dict[str, Any]:
    """Run the retention sweep once.

    Args:
        now: Override the current time (for tests).  Defaults to
            ``datetime.now(UTC)``.
        dry_run_override: If not None, overrides the ``dry_run`` config
            flag.  Used by the operator CLI to force a dry pass.

    Returns:
        A summary dict of the form::

            {
                "enabled": bool,
                "dry_run": bool,
                "vacuum_ran": bool,
                "tables": {
                    "events": {"enabled": bool, "retention_days": int,
                               "projected": int, "deleted": int},
                    ...
                },
                "total_deleted": int,
                "total_projected": int,
            }

        Tables that are disabled (globally or individually, or configured
        with ``retention_days <= 0``) report ``projected = deleted = 0``
        and are still included in the ``tables`` map so operators can
        see the config in effect.
    """
    now_dt = now or _utcnow()
    cfg = _cfg()
    global_enabled = bool(cfg.get("enabled", False))
    dry_run = bool(cfg.get("dry_run", False)) if dry_run_override is None else bool(dry_run_override)
    batch_size = int(cfg.get("batch_size", 1000) or 1000)
    if batch_size <= 0:
        batch_size = 1000
    vacuum_after = bool(cfg.get("vacuum_after", False))

    summary: dict[str, Any] = {
        "enabled": global_enabled,
        "dry_run": dry_run,
        "vacuum_ran": False,
        "tables": {},
        "total_deleted": 0,
        "total_projected": 0,
    }

    if not global_enabled:
        logger.info("db.retention.sweep: globally disabled; skipping")
        return summary

    any_deleted = False

    for table in _TABLE_ORDER:
        enabled, days = _table_cfg(table, cfg)
        entry: dict[str, Any] = {
            "enabled": enabled,
            "retention_days": days,
            "projected": 0,
            "deleted": 0,
        }
        summary["tables"][table] = entry

        if not enabled:
            continue

        cutoff_iso = (now_dt - timedelta(days=days)).isoformat()
        id_select = _PREDICATES[table]

        # Projected count (for dry_run and logging).  This is a plain
        # count of matching rows at the time of the query — the actual
        # DELETE loop below re-evaluates the predicate per batch, so
        # concurrent inserts during the sweep may cause deleted != projected.
        try:
            with db_connection() as db:
                projected_row = db.execute(
                    f"SELECT COUNT(*) AS n FROM ({id_select}) sub",
                    {"cutoff": cutoff_iso},
                ).fetchone()
            projected = int(projected_row["n"]) if projected_row else 0
        except Exception:  # noqa: BLE001 — never let one bad table kill the sweep
            logger.exception(
                "db.retention.sweep: failed to count %s; skipping table", table,
            )
            continue

        entry["projected"] = projected
        summary["total_projected"] += projected

        if dry_run:
            logger.info(
                "db.retention.sweep[dry_run]: %s cutoff=%s projected=%d",
                table, cutoff_iso, projected,
            )
            continue

        if projected == 0:
            continue

        # Batched delete loop.  We DELETE up to ``batch_size`` rows per
        # transaction so a runaway sweep can't lock the DB for minutes.
        deleted_total = 0
        while True:
            try:
                with db_transaction() as db:
                    cursor = db.execute(
                        f"DELETE FROM {table} "
                        f"WHERE id IN ({id_select} LIMIT :limit)",
                        {"cutoff": cutoff_iso, "limit": batch_size},
                    )
                    affected = cursor.rowcount
            except Exception:  # noqa: BLE001
                logger.exception(
                    "db.retention.sweep: DELETE batch failed for %s "
                    "(deleted so far this table: %d); stopping this table",
                    table, deleted_total,
                )
                break
            if affected <= 0:
                break
            deleted_total += affected
            if affected < batch_size:
                # Last (partial) batch — no point spinning another round.
                break

        entry["deleted"] = deleted_total
        summary["total_deleted"] += deleted_total
        if deleted_total > 0:
            any_deleted = True
        logger.info(
            "db.retention.sweep: %s cutoff=%s projected=%d deleted=%d",
            table, cutoff_iso, projected, deleted_total,
        )

    # Optional VACUUM.  VACUUM cannot run inside a transaction and does
    # not accept parameters, hence the bare connection here.
    if vacuum_after and any_deleted and not dry_run:
        try:
            with db_connection() as db:
                db.execute("VACUUM")
            summary["vacuum_ran"] = True
            logger.info("db.retention.sweep: VACUUM completed")
        except Exception:  # noqa: BLE001
            logger.exception("db.retention.sweep: VACUUM failed")

    logger.info("db.retention.sweep summary: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Trigger / subscription / handler wiring
# ---------------------------------------------------------------------------


async def handle_retention_work_item(work_id: int, payload: dict) -> None:
    """Main-loop work-item handler for ``db.retention.sweep``.

    Runs :func:`run_retention_sweep` and emits a
    ``db.retention.swept`` event carrying the summary.  ``work_id`` and
    ``payload`` are accepted for the standard handler signature but not
    otherwise used — the sweep pulls its own config each run.
    """
    from ..engine import event_bus

    summary = run_retention_sweep()
    event_bus.record_event(
        RETENTION_COMPLETED_EVENT_TYPE,
        summary,
        source="db.retention.sweep",
    )


def register_daily_retention(register_handler) -> None:
    """Install the daily cron entry and work-item handler.

    Called once per process from
    ``Coordinator._init_trigger_subscription_pipeline``.  Idempotent —
    the cron entry is deduped by name and the work-item handler
    overwrites any prior registration.

    The generic ``_builtin.timer_forward`` subscription already routes
    every ``timer.fired`` event to the work_queue using the cron
    entry's ``event_type``, so we don't need a feature-specific
    subscription here.

    Args:
        register_handler: The ``main_loop.register_handler`` callable
            (passed in to avoid a hard import cycle).
    """
    from ..engine import trigger_manager

    try:
        trigger_manager.add_cron(
            name=RETENTION_TRIGGER_NAME,
            cron_expr=RETENTION_CRON_SCHEDULE,
            event_type=RETENTION_EVENT_TYPE,
            event_payload=None,
        )
        logger.info(
            "db.retention.sweep: registered cron %s -> %s",
            RETENTION_CRON_SCHEDULE, RETENTION_EVENT_TYPE,
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "unique" in msg or "already" in msg:
            logger.debug("db.retention.sweep: cron already registered")
        else:
            logger.exception("db.retention.sweep: failed to register cron")

    register_handler(RETENTION_EVENT_TYPE, handle_retention_work_item)
    logger.info("db.retention.sweep: registered work-item handler")
