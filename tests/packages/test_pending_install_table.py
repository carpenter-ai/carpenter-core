"""The ``pending_package_installs`` record and its state machine.

A chat-requested install is authorised by an operator's decision on a
one-time link, so the row that carries the request has to survive a
restart, refuse a replayed decision and refuse any transition that
would install something nobody approved.
"""

from __future__ import annotations

import sqlite3

import pytest

from carpenter.packages import pending_install as pi
from carpenter.packages.installer import ensure_installer_tables


EXPECTED_COLUMNS = {
    "id", "token", "status", "requested_at", "expires_at", "decided_at",
    "finished_at", "conversation_id", "channel_type", "request_text",
    "sources_json", "preview_json", "staging_root", "grants_json",
    "result_json", "error", "attempts",
}

PREVIEW = [
    {"source_id": "aaaaaaaaaaaaaaaa", "kind": "git", "name": "alpha",
     "tree_hash": "a" * 64},
    {"source_id": "bbbbbbbbbbbbbbbb", "kind": "archive", "name": "beta",
     "tree_hash": "b" * 64},
]


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    ensure_installer_tables(db)
    yield db
    db.close()


def _create(db, **kw):
    kw.setdefault("sources", ["git+https://forge.invalid/org/repo.git"])
    kw.setdefault("preview", PREVIEW)
    kw.setdefault("ttl_hours", 24)
    return pi.create_request(db, **kw)


# ── the table ───────────────────────────────────────────────────────

def test_ensure_installer_tables_creates_the_table(conn):
    cols = {
        row[1] for row in
        conn.execute("PRAGMA table_info(pending_package_installs)").fetchall()
    }
    assert cols == EXPECTED_COLUMNS


def test_ensure_installer_tables_is_idempotent(conn):
    """Called twice on the same DB, with a row already in it."""
    record = _create(conn)
    ensure_installer_tables(conn)
    ensure_installer_tables(conn)
    assert pi.get_request(conn, record.token).id == record.id


def test_indexes_exist(conn):
    names = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'pending_package_installs'",
        ).fetchall()
    }
    assert {"idx_ppi_status", "idx_ppi_expires"} <= names


def test_upgrade_from_a_db_without_the_table():
    """An existing install gains the table on the next migration."""
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE installed_packages (
            name TEXT PRIMARY KEY, version TEXT NOT NULL, hash TEXT NOT NULL,
            source_path TEXT NOT NULL, install_path TEXT NOT NULL,
            installed_at TEXT NOT NULL
        );
        """,
    )
    with pytest.raises(sqlite3.OperationalError):
        db.execute("SELECT 1 FROM pending_package_installs")
    ensure_installer_tables(db)
    assert _create(db).status == pi.STATUS_REQUESTED
    db.close()


# ── create / read ───────────────────────────────────────────────────

def test_create_request_writes_a_requested_row(conn):
    record = _create(
        conn, request_text="install alpha and beta please",
        conversation_id=7, channel_type="web", staging_root="/nowhere/stage",
    )
    assert record.status == pi.STATUS_REQUESTED
    assert record.sources == ("git+https://forge.invalid/org/repo.git",)
    assert record.preview[1]["name"] == "beta"
    assert record.request_text == "install alpha and beta please"
    assert record.conversation_id == 7
    assert record.channel_type == "web"
    assert record.staging_root == "/nowhere/stage"
    assert record.grants == {} and record.result == {}
    assert record.attempts == 0
    assert record.decided_at is None and record.finished_at is None
    assert len(record.token) == 36 and record.token_prefix == record.token[:8]


def test_create_request_refuses_an_empty_source_list(conn):
    with pytest.raises(pi.PendingInstallError):
        pi.create_request(conn, sources=[], preview=PREVIEW)


def test_token_is_unique(conn):
    record = _create(conn)
    with pytest.raises(pi.PendingInstallError):
        _create(conn, token=record.token)


def test_expiry_is_requested_at_plus_ttl(conn):
    record = _create(
        conn, requested_at="2000-01-01T00:00:00+00:00", ttl_hours=24,
    )
    assert record.expires_at == "2000-01-02T00:00:00+00:00"
    assert record.is_expired() is True


def test_ttl_comes_from_config(conn):
    record = _create(
        conn, requested_at="2000-01-01T00:00:00+00:00", ttl_hours=None,
        config={"package_install": {"request_ttl_hours": 2}},
    )
    assert record.expires_at == "2000-01-01T02:00:00+00:00"


@pytest.mark.parametrize("section", [
    None, {}, {"request_ttl_hours": 0}, {"request_ttl_hours": -1},
    {"request_ttl_hours": "soon"}, "not-a-dict",
])
def test_unusable_ttl_config_falls_back_to_the_default(section):
    """A malformed value must never mean "this link never expires"."""
    assert pi.default_ttl_hours({"package_install": section}) == 24.0


def test_ttl_default_is_in_the_shipped_config():
    from carpenter import config
    assert config.DEFAULTS["package_install"]["request_ttl_hours"] == 24


def test_get_request_unknown_token_is_none(conn):
    assert pi.get_request(conn, "nope") is None
    assert pi.get_request(conn, "") is None


def test_get_request_by_id(conn):
    record = _create(conn)
    assert pi.get_request_by_id(conn, record.id).token == record.token
    assert pi.get_request_by_id(conn, record.id + 500) is None


def test_list_requests_newest_first_and_filtered(conn):
    first = _create(conn)
    second = _create(conn)
    pi.mark_rejected(conn, first.token)
    assert [r.id for r in pi.list_requests(conn)] == [second.id, first.id]
    assert [r.id for r in pi.list_requests(conn, status=pi.STATUS_REJECTED)] \
        == [first.id]


# ── the state machine ───────────────────────────────────────────────

def test_the_happy_path(conn):
    record = _create(conn)
    approved = pi.mark_approved(
        conn, record.token,
        grants={"alpha": {"capabilities": True, "write_chat_tools": False}},
    )
    assert approved.status == pi.STATUS_APPROVED
    assert approved.decided_at and approved.finished_at is None
    assert approved.grants["alpha"]["capabilities"] is True

    installing = pi.mark_installing(conn, record.token)
    assert installing.status == pi.STATUS_INSTALLING
    assert installing.attempts == 1

    done = pi.mark_installed(
        conn, record.token, result={"alpha": {"status": "installed"}},
    )
    assert done.status == pi.STATUS_INSTALLED
    assert done.finished_at and done.is_terminal
    assert done.result["alpha"]["status"] == "installed"


def test_reject_and_expire(conn):
    rejected = pi.mark_rejected(conn, _create(conn).token, reason="no thanks")
    assert rejected.status == pi.STATUS_REJECTED
    assert rejected.error == "no thanks" and rejected.finished_at

    expired = pi.mark_expired(conn, _create(conn).token)
    assert expired.status == pi.STATUS_EXPIRED and expired.is_terminal


def test_failure_from_approved_and_from_installing(conn):
    a = _create(conn)
    pi.mark_approved(conn, a.token, grants={})
    assert pi.mark_failed(
        conn, a.token, error="the fetched tree is no longer on disk",
    ).status == pi.STATUS_FAILED

    b = _create(conn)
    pi.mark_approved(conn, b.token, grants={})
    pi.mark_installing(conn, b.token)
    failed = pi.mark_failed(
        conn, b.token, error="interrupted by restart",
        result={"alpha": {"status": "rolled_back"}},
    )
    assert failed.status == pi.STATUS_FAILED
    assert failed.result["alpha"]["status"] == "rolled_back"


@pytest.mark.parametrize("target", [
    pi.STATUS_INSTALLING, pi.STATUS_INSTALLED, pi.STATUS_FAILED,
])
def test_a_requested_row_cannot_skip_the_decision(conn, target):
    """Nothing installs without an approval: the edge does not exist."""
    record = _create(conn)
    with pytest.raises(pi.PendingInstallStateError):
        pi.set_status(conn, record.token, target)
    assert pi.get_request(conn, record.token).status == pi.STATUS_REQUESTED


def test_an_approved_row_cannot_be_rejected_or_re_approved(conn):
    record = _create(conn)
    pi.mark_approved(conn, record.token, grants={})
    with pytest.raises(pi.PendingInstallStateError):
        pi.mark_rejected(conn, record.token)
    with pytest.raises(pi.PendingInstallStateError):
        pi.mark_approved(conn, record.token, grants={})


@pytest.mark.parametrize("terminal", [
    pi.STATUS_INSTALLED, pi.STATUS_FAILED, pi.STATUS_REJECTED,
    pi.STATUS_EXPIRED,
])
def test_terminal_states_are_never_left(conn, terminal):
    record = _create(conn)
    conn.execute(
        "UPDATE pending_package_installs SET status = ? WHERE token = ?",
        (terminal, record.token),
    )
    for target in sorted(pi.STATUSES):
        with pytest.raises(pi.PendingInstallStateError):
            pi.set_status(conn, record.token, target)


def test_the_token_is_spent_on_the_first_decision(conn):
    """Two decisions on one link: the second is refused, not applied."""
    record = _create(conn)
    pi.mark_approved(conn, record.token, grants={"alpha": {}})
    with pytest.raises(pi.PendingInstallStateError):
        pi.mark_rejected(conn, record.token)
    again = pi.get_request(conn, record.token)
    assert again.status == pi.STATUS_APPROVED and again.grants == {"alpha": {}}


def test_set_status_refuses_an_unknown_token_or_status(conn):
    record = _create(conn)
    with pytest.raises(pi.PendingInstallStateError):
        pi.set_status(conn, "no-such-token", pi.STATUS_REJECTED)
    with pytest.raises(pi.PendingInstallStateError):
        pi.set_status(conn, record.token, "halfway")


def test_expect_pins_the_current_status(conn):
    record = _create(conn)
    pi.mark_approved(conn, record.token, grants={})
    with pytest.raises(pi.PendingInstallStateError):
        pi.set_status(
            conn, record.token, pi.STATUS_INSTALLING,
            expect=pi.STATUS_REQUESTED,
        )


def test_a_terminal_row_is_not_also_expired(conn):
    record = _create(conn, requested_at="2000-01-01T00:00:00+00:00")
    assert record.is_expired() is True
    rejected = pi.mark_rejected(conn, record.token)
    assert rejected.is_expired() is False


# ── rate-limit and sweep queries ────────────────────────────────────

def test_count_active_requests_ignores_decided_and_stale_rows(conn):
    live = _create(conn)
    decided = _create(conn)
    pi.mark_rejected(conn, decided.token)
    _create(conn, requested_at="2000-01-01T00:00:00+00:00")  # past its TTL
    assert pi.count_active_requests(conn) == 1
    assert pi.get_request(conn, live.token).status == pi.STATUS_REQUESTED


def test_count_requests_since_counts_every_status(conn):
    _create(conn, requested_at="2000-01-01T00:00:00+00:00")
    recent = _create(conn, requested_at="2030-01-01T00:00:00+00:00")
    pi.mark_rejected(conn, recent.token)
    assert pi.count_requests_since(conn, "2029-01-01T00:00:00+00:00") == 1
    assert pi.count_requests_since(conn, "1999-01-01T00:00:00+00:00") == 2


def test_rejected_source_ids_since(conn):
    """Keyed on the DECISION time: "rejected in the last 24h"."""
    pi.mark_rejected(conn, _create(conn).token)
    pi.mark_approved(conn, _create(conn).token, grants={})
    ids = pi.rejected_source_ids_since(conn, "2000-01-01T00:00:00+00:00")
    assert ids == {"aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"}
    assert pi.rejected_source_ids_since(conn, "2099-01-01T00:00:00+00:00") \
        == set()


def test_due_for_expiry_only_returns_undecided_stale_rows(conn):
    stale = _create(conn, requested_at="2000-01-01T00:00:00+00:00")
    fresh = _create(conn)
    decided = _create(conn, requested_at="2000-01-01T00:00:00+00:00")
    pi.mark_approved(conn, decided.token, grants={})
    due = pi.due_for_expiry(conn)
    assert [r.id for r in due] == [stale.id]
    assert pi.mark_expired(conn, stale.token).status == pi.STATUS_EXPIRED
    assert pi.due_for_expiry(conn) == []
    assert pi.get_request(conn, fresh.token).status == pi.STATUS_REQUESTED


# ── durability ──────────────────────────────────────────────────────

def test_a_request_survives_losing_every_in_process_reference(tmp_path):
    """No recovery function: the row IS the state.

    Written on one connection, read on another after the first is gone —
    which is what a platform restart looks like to this module.
    """
    db_path = tmp_path / "platform.db"
    first = sqlite3.connect(db_path)
    ensure_installer_tables(first)
    record = _create(first, request_text="install alpha")
    first.commit()
    first.close()
    del first

    second = sqlite3.connect(db_path)
    try:
        reloaded = pi.get_request(second, record.token)
        assert reloaded.id == record.id
        assert reloaded.request_text == "install alpha"
        assert reloaded.preview == tuple(PREVIEW)
        assert pi.mark_approved(
            second, record.token, grants={"alpha": {"capabilities": False}},
        ).status == pi.STATUS_APPROVED
    finally:
        second.close()


def test_the_module_holds_no_pending_state():
    """Nothing to recover means nothing to forget on restart."""
    leaky = {
        name: value for name, value in vars(pi).items()
        if not name.startswith("_")
        and isinstance(value, (list, set, dict))
    }
    assert leaky == {}


def test_unreadable_json_still_reads_as_a_record(conn):
    """A request row is an audit record: it must stay readable."""
    record = _create(conn)
    conn.execute(
        "UPDATE pending_package_installs "
        "SET preview_json = '{{{', grants_json = 'nope' WHERE token = ?",
        (record.token,),
    )
    reloaded = pi.get_request(conn, record.token)
    assert reloaded.preview == () and reloaded.grants == {}
