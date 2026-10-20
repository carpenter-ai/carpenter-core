"""Expiring approval links, reclaiming quarantine, surviving a restart.

An approval link is a credential with a TTL, and the fetched tree behind
it is disk the platform is holding on an untrusted remote's behalf.  The
routes expire the one row in front of them, which is what makes a stale
link unusable; these sweeps are what makes the disk come back and what
turns "the platform stopped mid-install" into something the operator can
see rather than a row stuck in ``installing`` forever.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from carpenter import config
from carpenter.db import _recover_on_startup, db_connection, db_transaction, get_db
from carpenter.packages import pending_install as pi
from carpenter.packages.installer import ensure_installer_tables


@pytest.fixture
def installer_tables():
    db = get_db()
    try:
        ensure_installer_tables(db)
    finally:
        db.close()


def _staging_root() -> Path:
    root = Path(config.CONFIG["base_dir"]) / "cache" / "package-staging"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ago(hours: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()


def _make(
    *, requested_at=None, ttl_hours=24.0, staging=True, name="req-abc",
):
    """Create a request row, optionally with a quarantine on disk."""
    staging_root = None
    if staging:
        staging_root = _staging_root() / name
        (staging_root / "pkg").mkdir(parents=True, exist_ok=True)
        (staging_root / "pkg" / "manifest.yaml").write_text("name: x\n")
    with db_transaction() as db:
        return pi.create_request(
            db,
            sources=["git+https://forge.invalid/org/x.git"],
            preview=[{"manifest": {"name": "x"}, "tree_hash": "0" * 64}],
            requested_at=requested_at or pi.utc_now_iso(),
            ttl_hours=ttl_hours,
            staging_root=str(staging_root) if staging_root else None,
        )


# ── the sweep ───────────────────────────────────────────────────────

def test_the_sweep_expires_a_stale_request_and_removes_its_quarantine(
    installer_tables,
):
    record = _make(requested_at=_ago(48), name="req-stale")
    quarantine = Path(record.staging_root)
    assert quarantine.is_dir()

    with db_transaction() as db:
        swept = pi.sweep_expired(db)

    assert [r.token for r in swept] == [record.token]
    assert swept[0].status == pi.STATUS_EXPIRED
    assert swept[0].error
    assert not quarantine.exists()


def test_the_sweep_leaves_a_request_inside_its_ttl_alone(installer_tables):
    record = _make(requested_at=_ago(1), name="req-fresh")

    with db_transaction() as db:
        assert pi.sweep_expired(db) == []

    assert pi.get_request(get_db(), record.token).status == pi.STATUS_REQUESTED
    assert Path(record.staging_root).is_dir()


def test_the_sweep_leaves_a_decided_request_alone(installer_tables):
    """A decided request has an outcome; "expired" would lose it."""
    record = _make(requested_at=_ago(48), name="req-decided")
    with db_transaction() as db:
        pi.mark_approved(db, record.token, grants={})

    with db_transaction() as db:
        assert pi.sweep_expired(db) == []

    with db_connection() as db:
        assert pi.get_request(db, record.token).status == pi.STATUS_APPROVED
    assert Path(record.staging_root).is_dir(), (
        "an approved request's tree is what the install uses"
    )


def test_the_sweep_handles_a_request_with_no_quarantine(installer_tables):
    record = _make(requested_at=_ago(48), staging=False, name="req-nodir")

    with db_transaction() as db:
        swept = pi.sweep_expired(db)

    assert [r.token for r in swept] == [record.token]


def test_the_sweep_is_a_no_op_without_the_table():
    """A database that never had the installer tables is not an error."""
    with db_transaction() as db:
        assert pi.table_exists(db) is False
        assert pi.sweep_expired(db) == []
        assert pi.fail_interrupted_installs(db) == []
        assert pi.recover_on_startup(db) == (0, 0)


def test_the_sweep_respects_its_limit(installer_tables):
    for i in range(3):
        _make(requested_at=_ago(48), name=f"req-many-{i}")

    with db_transaction() as db:
        first = pi.sweep_expired(db, limit=2)
    with db_transaction() as db:
        second = pi.sweep_expired(db)

    assert len(first) == 2
    assert len(second) == 1


# ── quarantine removal is bounded ───────────────────────────────────

def test_a_staging_path_outside_the_quarantine_root_is_not_removed(
    installer_tables, tmp_path,
):
    """A mis-recorded column must not turn a sweep into rm -rf."""
    outside = tmp_path / "not-quarantine"
    outside.mkdir()
    (outside / "important").write_text("keep me")
    record = _make(requested_at=_ago(48), staging=False, name="req-outside")
    with db_transaction() as db:
        db.execute(
            "UPDATE pending_package_installs SET staging_root = ? "
            "WHERE token = ?",
            (str(outside), record.token),
        )

    with db_transaction() as db:
        swept = pi.sweep_expired(db)

    assert len(swept) == 1, "the row is still expired"
    assert (outside / "important").exists()


def test_the_quarantine_root_itself_is_not_removed(installer_tables):
    root = _staging_root()
    other = root / "req-someone-else"
    other.mkdir(parents=True, exist_ok=True)
    record = _make(requested_at=_ago(48), staging=False, name="req-root")
    with db_transaction() as db:
        db.execute(
            "UPDATE pending_package_installs SET staging_root = ? "
            "WHERE token = ?",
            (str(root), record.token),
        )

    with db_transaction() as db:
        pi.sweep_expired(db)

    assert root.is_dir()
    assert other.is_dir(), "another request's tree must survive"


# ── restart recovery ────────────────────────────────────────────────

def test_an_interrupted_install_is_failed_not_resumed(installer_tables):
    record = _make(name="req-inflight")
    with db_transaction() as db:
        pi.mark_approved(db, record.token, grants={})
        pi.mark_installing(db, record.token)

    with db_transaction() as db:
        failed = pi.fail_interrupted_installs(db)

    assert [r.token for r in failed] == [record.token]
    assert failed[0].status == pi.STATUS_FAILED
    assert failed[0].error == pi.INTERRUPTED_ERROR
    assert failed[0].finished_at


def test_startup_recovery_flips_installing_to_failed(installer_tables):
    """``_recover_on_startup`` is what the handler's status check needs."""
    record = _make(name="req-restart")
    with db_transaction() as db:
        pi.mark_approved(db, record.token, grants={})
        pi.mark_installing(db, record.token)

    db = get_db()
    try:
        _recover_on_startup(db)
    finally:
        db.close()

    with db_connection() as db:
        after = pi.get_request(db, record.token)
    assert after.status == pi.STATUS_FAILED
    assert after.error == pi.INTERRUPTED_ERROR


def test_startup_recovery_also_expires_stale_requests(installer_tables):
    stale = _make(requested_at=_ago(48), name="req-stale-2")
    fresh = _make(requested_at=_ago(1), name="req-fresh-2")

    db = get_db()
    try:
        _recover_on_startup(db)
    finally:
        db.close()

    with db_connection() as db:
        assert pi.get_request(db, stale.token).status == pi.STATUS_EXPIRED
        assert pi.get_request(db, fresh.token).status == pi.STATUS_REQUESTED
    assert not Path(stale.staging_root).exists()
    assert Path(fresh.staging_root).is_dir()


def test_startup_recovery_survives_a_database_without_the_table():
    db = get_db()
    try:
        _recover_on_startup(db)  # must not raise
    finally:
        db.close()


def test_recover_on_startup_fails_before_it_expires(installer_tables):
    """Order matters: an interrupted install is not also "expired".

    ``installing`` is not a status :func:`due_for_expiry` selects, so
    the two sweeps cannot fight — but the ordering is asserted because
    reversing it would make an interrupted install past its TTL report
    the less informative of the two outcomes.
    """
    record = _make(requested_at=_ago(48), name="req-both")
    with db_transaction() as db:
        pi.mark_approved(db, record.token, grants={})
        pi.mark_installing(db, record.token)

    with db_transaction() as db:
        expired, interrupted = pi.recover_on_startup(db)

    assert (expired, interrupted) == (0, 1)
    with db_connection() as db:
        after = pi.get_request(db, record.token)
    assert after.status == pi.STATUS_FAILED
    assert after.error == pi.INTERRUPTED_ERROR


# ── the hourly heartbeat hook ───────────────────────────────────────

def _sweep_hook():
    """Pull the registered sweep hook out of the coordinator."""
    from carpenter.core.engine import main_loop

    hooks_before = list(main_loop._heartbeat_hooks)
    handlers_before = dict(main_loop._handlers)
    try:
        from carpenter.coordinator import Coordinator
        coordinator = Coordinator.__new__(Coordinator)
        coordinator._register_work_handlers()
        new = [h for h in main_loop._heartbeat_hooks if h not in hooks_before]
    finally:
        # Registration mutates process-global dispatch state; put both
        # back so this test cannot leak handlers into another.
        main_loop._heartbeat_hooks[:] = hooks_before
        main_loop._handlers.clear()
        main_loop._handlers.update(handlers_before)
    hooks = [
        h for h in new
        if getattr(h, "__name__", "") == "_package_install_sweep_hook"
    ]
    assert len(hooks) == 1, "exactly one sweep hook must be registered"
    return hooks[0]


def test_the_heartbeat_hook_sweeps_then_throttles_itself(installer_tables):
    record = _make(requested_at=_ago(48), name="req-hook")
    hook = _sweep_hook()

    hook()

    with db_connection() as db:
        assert pi.get_request(db, record.token).status == pi.STATUS_EXPIRED

    # A second request goes stale, but the hook has just run: the
    # throttle means it is not swept until the next hour.
    second = _make(requested_at=_ago(48), name="req-hook-2")
    hook()
    with db_connection() as db:
        assert pi.get_request(db, second.token).status == pi.STATUS_REQUESTED


def test_the_heartbeat_hook_does_not_raise_without_the_table():
    _sweep_hook()()  # no installer tables in this DB


def test_the_heartbeat_hook_swallows_a_database_error(monkeypatch):
    hook = _sweep_hook()

    def boom(_conn):
        raise sqlite3.OperationalError("the disk is on fire")

    monkeypatch.setattr(pi, "table_exists", boom)
    hook()  # must not raise


# ── an approved row the enqueue never happened for (F4) ─────────────
#
# ``_enqueue_install`` runs after the approval transaction commits, by
# necessity: ``work_queue.enqueue`` opens a transaction of its own.  A
# crash in that window used to leave a row no path could reach --
# ``approved`` is not terminal, not swept, not re-decidable (the route
# answers 410 and the state machine refuses ``approved -> requested``)
# -- and a quarantine nothing reclaimed.


def _approve_without_enqueuing(record):
    """The crash window: the approval commits, the enqueue never runs."""
    with db_transaction() as db:
        pi.mark_approved(db, record.token, grants={})


def _install_work_items():
    from carpenter.packages.install_approved_handler import EVENT_TYPE

    with db_connection() as db:
        return [
            dict(row) for row in db.execute(
                "SELECT id, status, idempotency_key, payload_json "
                "FROM work_queue WHERE event_type = ?",
                (EVENT_TYPE,),
            ).fetchall()
        ]


def test_an_approved_row_is_re_enqueued_by_recovery(installer_tables):
    from carpenter.packages import install_approved_handler as handler

    record = _make(name="req-approved-lost")
    _approve_without_enqueuing(record)
    assert _install_work_items() == []

    requeued, failed = handler.recover_approved_installs()

    assert (requeued, failed) == (1, 0)
    items = _install_work_items()
    assert len(items) == 1
    assert items[0]["idempotency_key"] == handler.idempotency_key_for(
        record.id,
    )
    with db_connection() as db:
        after = pi.get_request(db, record.token)
    assert after.status == pi.STATUS_APPROVED  # now reachable, not stuck
    assert Path(record.staging_root).is_dir()  # the install still needs it


def test_recovery_does_not_enqueue_a_second_item(installer_tables):
    from carpenter.packages import install_approved_handler as handler

    record = _make(name="req-approved-twice")
    _approve_without_enqueuing(record)

    handler.recover_approved_installs()
    second = handler.recover_approved_installs()

    assert second == (0, 0)
    assert len(_install_work_items()) == 1


def test_startup_recovery_re_enqueues_an_approved_row(installer_tables):
    """The coordinator owns this half: enqueuing needs its own transaction.

    ``pending_install.recover_on_startup`` runs inside the startup
    connection's transaction, so it cannot enqueue; the coordinator
    calls the handler's recovery right after the review-link recovery.
    """
    import inspect

    from carpenter.coordinator import Coordinator

    assert "_recover_approved_installs" in inspect.getsource(Coordinator.start)

    record = _make(name="req-approved-startup")
    _approve_without_enqueuing(record)

    Coordinator.__new__(Coordinator)._recover_approved_installs()

    assert len(_install_work_items()) == 1
    with db_connection() as db:
        assert pi.get_request(db, record.token).status == pi.STATUS_APPROVED


def test_an_approval_beyond_the_grace_period_is_failed(installer_tables):
    from carpenter.packages import install_approved_handler as handler

    record = _make(name="req-approved-stale")
    with db_transaction() as db:
        pi.mark_approved(db, record.token, grants={})
        # Backdate the decision past the grace period.
        db.execute(
            "UPDATE pending_package_installs SET decided_at = ? WHERE id = ?",
            (_ago(72), record.id),
        )

    requeued, failed = handler.recover_approved_installs()

    assert (requeued, failed) == (0, 1)
    with db_connection() as db:
        after = pi.get_request(db, record.token)
    assert after.status == pi.STATUS_FAILED
    assert "approved" in (after.error or "")
    assert "ask again" in (after.error or "")
    assert after.finished_at
    # The quarantine it was holding is reclaimed with it.
    assert not Path(record.staging_root).exists()
    assert _install_work_items() == []


def test_the_heartbeat_hook_recovers_approved_rows_too(installer_tables):
    record = _make(name="req-approved-hook")
    _approve_without_enqueuing(record)

    _sweep_hook()()

    assert len(_install_work_items()) == 1
    with db_connection() as db:
        assert pi.get_request(db, record.token).status == pi.STATUS_APPROVED


def test_an_interrupted_install_reclaims_its_quarantine(installer_tables):
    record = _make(name="req-interrupted-disk")
    with db_transaction() as db:
        pi.mark_approved(db, record.token, grants={})
        pi.mark_installing(db, record.token)

    with db_transaction() as db:
        pi.fail_interrupted_installs(db)

    assert not Path(record.staging_root).exists()
