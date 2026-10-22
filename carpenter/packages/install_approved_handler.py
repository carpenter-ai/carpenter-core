"""The ``package.install.approved`` work handler.

The approval route does not install anything.  It flips the row to
``approved``, enqueues one work item and returns — because the route
body runs on the event loop, where a fetch-verify + copytree + hash
would stall every other HTTP request including the chat UI's 500ms
poll, and because a browser refresh of a synchronous install opens a
second install window.  This module is what picks that work item up.

The handler owns the row's status and nothing else owns it:

* ``approved`` is the only status it will act on.  Anything else is
  logged and dropped, which is what makes a **re-dispatched** item
  harmless.  That matters because startup recovery resets ``claimed``
  work items to ``pending`` regardless of ``max_retries``, so an
  interrupted install's work item WILL come back; startup recovery also
  flips the row ``installing → failed``, so when the item comes back
  the status check drops it.  Resuming is not an option: after an
  interruption the batch's partial state is unknown and its rollback
  may itself have been interrupted, so the honest move is to fail,
  report it in chat, and leave ``verify_install`` and the operator to
  decide.
* ``installing`` is claimed with a compare-and-set, so two dispatches of
  the same item cannot both get past it.
* ``installed`` / ``failed`` is written from the batch's own report, and
  the conversation is notified from that same row.

**No transaction is held across the thread hop.**  ``db_transaction()``
guards against re-entry *per thread*, and the actual install runs on a
``tc-work`` thread via ``thread_pools.run_in_work_pool`` so that it can
open one transaction per package.  Every status write here therefore
opens and closes its own transaction before or after the ``await``,
never around it.
"""

from __future__ import annotations

import logging

from . import install_batch, install_notify, pending_install
from .pending_install import STATUS_APPROVED

logger = logging.getLogger(__name__)

__all__ = [
    "EVENT_TYPE",
    "enqueue_approved_install",
    "handle_package_install_approved",
    "idempotency_key_for",
    "recover_approved_installs",
    "register_handlers",
]

#: The work-queue event type.  The decide route enqueues this; the main
#: loop dispatches it to :func:`handle_package_install_approved`.
EVENT_TYPE = "package.install.approved"

#: Dispatch ahead of the default (100): an operator is watching a page
#: for this, and the request has already waited for a human.
PRIORITY = 50


def idempotency_key_for(request_id: int) -> str:
    """The work-queue idempotency key for one request.

    Keyed on the row id, not the token.  It still makes a second POST to
    the decide route unable to enqueue a second install, because there is
    one row per request — but the token is the credential that approves
    an install, and ``work_queue`` rows are durable, readable by anything
    with database access, and outlive the request.  Writing the token
    into one would hand out that credential to every later reader.
    """
    return f"pkg_install:{request_id}"


def enqueue_approved_install(record) -> int | None:
    """Queue the install of an already-approved request.

    Call this **after** the transaction that flipped the row to
    ``approved`` has committed, not inside it: ``work_queue.enqueue``
    opens a ``db_transaction()`` of its own, and that guard is
    per-thread, so calling it inside another one raises
    ``RuntimeError`` rather than enqueuing anything.  Follow it with
    ``main_loop.wake_signal.set()`` so the loop picks the item up
    without waiting for the next heartbeat.

    The row is already ``approved`` and durable at that point, so a
    crash between the commit and the enqueue loses only the dispatch:
    the operator sees an approved request that never started, and the
    honest recovery is to look, not to guess.

    The payload carries the row **id**, not the token: the work_queue
    table is not a place to put a credential, and the handler can read
    the token off the row.

    Returns:
        The work item id, or ``None`` when an item for this request is
        already pending or claimed.
    """
    from ..core.engine import work_queue

    work_id = work_queue.enqueue(
        EVENT_TYPE,
        {"request_id": record.id},
        idempotency_key=idempotency_key_for(record.id),
        max_retries=work_queue.SINGLE_ATTEMPT,
        priority=PRIORITY,
    )
    if work_id is None:
        logger.warning(
            "Package install request %s: an install work item is already "
            "queued; not enqueuing a second one",
            record.token_prefix,
        )
    return work_id


def recover_approved_installs() -> tuple[int, int]:
    """Make every ``approved`` row reachable again.  Returns (requeued, failed).

    The gap this closes: :func:`enqueue_approved_install` must run after
    the approval transaction commits, so a crash in between leaves a row
    in ``approved`` with no work item — and ``approved`` is not
    terminal, not swept and not re-decidable, so nothing ever looked at
    it again and its quarantine was never reclaimed.

    Two passes, both safe to repeat:

    * Approvals past the grace period are **failed** with an error the
      operator can act on, and their quarantine removed.
    * Everything still in ``approved`` is **re-enqueued**.  The
      idempotency key makes that free when an item is already pending or
      claimed (``enqueue`` returns ``None``), and the handler drops any
      item whose row is no longer ``approved``, so this cannot cause a
      double install.

    Called at startup (after the database is open — this opens
    transactions of its own, so it cannot run inside
    :func:`carpenter.packages.pending_install.recover_on_startup`) and
    from the hourly sweep.
    """
    from ..db import db_connection, db_transaction

    with db_connection() as db:
        if not pending_install.table_exists(db):
            return (0, 0)

    with db_transaction() as db:
        failed = pending_install.fail_stale_approved(db)

    with db_connection() as db:
        waiting = pending_install.approved_awaiting_install(db)

    requeued = 0
    for record in waiting:
        if enqueue_approved_install(record) is not None:
            requeued += 1
            logger.warning(
                "Re-enqueued the install of approved request %s, which had "
                "no work item; the approval itself is unchanged",
                record.token_prefix,
            )
    if requeued:
        from ..core.engine.main_loop import wake_signal
        wake_signal.set()
    return (requeued, len(failed))


def _load(request_id, token):
    """Read the request row, by id when we have one, else by token."""
    from ..db import db_connection

    with db_connection() as db:
        if request_id is not None:
            record = pending_install.get_request_by_id(db, int(request_id))
            if record is not None:
                return record
        if token:
            return pending_install.get_request(db, str(token))
    return None


async def handle_package_install_approved(work_id: int, payload: dict) -> None:
    """Install the batch an operator approved, then report the outcome."""
    from ..db import db_transaction

    request_id = payload.get("request_id")
    token = payload.get("token")
    record = _load(request_id, token)

    if record is None:
        logger.warning(
            "package.install.approved: no install request %r; dropping work "
            "item %s",
            request_id if request_id is not None else "<by token>", work_id,
        )
        return

    if record.status != STATUS_APPROVED:
        # The idempotency point.  A re-dispatched item after a restart,
        # or a second enqueue, lands here and stops.
        logger.warning(
            "package.install.approved: request %s is %r, not %r; dropping "
            "work item %s without installing anything",
            record.token_prefix, record.status, STATUS_APPROVED, work_id,
        )
        return

    with db_transaction() as db:
        record = pending_install.mark_installing(db, record.token)

    try:
        batch = await _run_batch(record.id)
    except Exception as exc:  # noqa: BLE001 — must still report
        logger.exception(
            "package.install.approved: the install of request %s crashed",
            record.token_prefix,
        )
        batch = install_batch.BatchResult(
            ok=False,
            error=f"the install crashed: {exc}",
            result={"status": "failed", "packages": [], "error": str(exc)},
        )

    with db_transaction() as db:
        if batch.ok:
            record = pending_install.mark_installed(
                db, record.token, result=batch.result,
            )
        else:
            record = pending_install.mark_failed(
                db, record.token,
                error=batch.error or "the install failed",
                result=batch.result,
            )

    # Terminal either way, so the quarantine has no owner left: the
    # batch has copied what it needed out of it, and a failed batch has
    # already rolled back. Every other terminal status reclaims it too
    # (rejection on the route, expiry and interruption in the sweeps).
    pending_install.remove_staging(record)

    await install_notify.notify_install_outcome(record)


async def _run_batch(request_id: int):
    """Run the blocking install on the work-handler pool.

    Split out so a test can drive the handler without a thread pool,
    and so it is obvious that nothing holds a transaction here: the
    per-thread guard in ``carpenter.db`` would raise inside
    :func:`perform_install_batch` if anything did.
    """
    from .. import thread_pools

    return await thread_pools.run_in_work_pool(
        install_batch.perform_install_batch, request_id,
    )


def register_handlers(register_fn) -> None:
    """Register the approved-install handler with the main loop."""
    register_fn(EVENT_TYPE, handle_package_install_approved)
