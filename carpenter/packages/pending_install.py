"""Pending package-install requests — the durable half of chat-requested installs.

A chat agent may *ask* for a package to be installed; it may never
install one.  The request writes a row here, mints a one-time token and
hands the operator a URL; the operator's decision on that URL is what
authorises the install.  This module owns that row: its DDL, its state
machine and the only writes anything should make to it.

Why SQL rather than a process-global dict (which is how
``api/review.py`` keeps arc-approval links): a request outlives the turn
that made it and must outlive a platform restart, and there is nothing
to hang it off — a package install has no arc, so the ``arc_state``
durability the review links use does not apply.  Keeping it in SQL makes
restart survival free: there is no recovery function to call, because
nothing is held in memory.  This module deliberately holds **no
module-level mutable state** for that reason.

The token is the credential.  It is a uuid4 (122 bits of entropy),
unguessable, single-use, and it expires.  Log it as
:func:`token_prefix` only — never whole, never in a URL that lands in a
log, an error message or an email subject.

State machine, enforced by :func:`set_status`::

    requested ──▶ approved ──▶ installing ──▶ installed
         │             │            │      └─▶ failed
         │             └────────────┴────────▶ failed
         ├──▶ rejected
         └──▶ expired

Terminal states (``installed``, ``failed``, ``rejected``, ``expired``)
are never re-entered or left, so a decision cannot be replayed and a
second POST to the decide route cannot install twice.  The
``approved → installing`` edge is what makes "interrupted mid-install"
detectable after a restart: a row still in ``installing`` with no live
worker was interrupted, and the honest move is to fail it rather than
guess what landed on disk.

The fetched tree itself is NOT in the database.  It stays in its fetch
quarantine (``{base_dir}/cache/package-staging/...``, off the registry
search path) and is re-hashed at approval time.  Re-fetching at approval
time would be a TOCTOU hole: the operator approves tree hash H and a
hostile remote serves H' to the second fetch.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "INTERRUPTED_ERROR",
    "PENDING_PACKAGE_INSTALLS_DDL",
    "PendingInstall",
    "PendingInstallError",
    "PendingInstallStateError",
    "STATUSES",
    "STATUS_APPROVED",
    "STATUS_EXPIRED",
    "STATUS_FAILED",
    "STATUS_INSTALLED",
    "STATUS_INSTALLING",
    "STATUS_REJECTED",
    "STATUS_REQUESTED",
    "TERMINAL_STATUSES",
    "count_active_requests",
    "count_requests_since",
    "create_request",
    "default_ttl_hours",
    "due_for_expiry",
    "ensure_pending_install_table",
    "expires_at_for",
    "fail_interrupted_installs",
    "get_request",
    "get_request_by_id",
    "list_requests",
    "mark_approved",
    "mark_expired",
    "mark_failed",
    "mark_installed",
    "mark_installing",
    "mark_rejected",
    "new_token",
    "recover_on_startup",
    "rejected_source_ids_since",
    "remove_staging",
    "set_status",
    "sweep_expired",
    "table_exists",
    "token_prefix",
    "utc_now_iso",
]


# ── status vocabulary ───────────────────────────────────────────────

STATUS_REQUESTED = "requested"
STATUS_APPROVED = "approved"
STATUS_INSTALLING = "installing"
STATUS_INSTALLED = "installed"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"

STATUSES = frozenset({
    STATUS_REQUESTED,
    STATUS_APPROVED,
    STATUS_INSTALLING,
    STATUS_INSTALLED,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_EXPIRED,
})

#: Statuses that are never left.  A row in one of these is history.
TERMINAL_STATUSES = frozenset({
    STATUS_INSTALLED,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_EXPIRED,
})

#: The only legal edges.  ``approved → failed`` exists because the
#: install can fail before it starts (the staging tree is gone, the
#: hash no longer matches), and ``installing → failed`` because startup
#: recovery fails an interrupted install rather than resuming it.
_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_REQUESTED: frozenset({
        STATUS_APPROVED, STATUS_REJECTED, STATUS_EXPIRED,
    }),
    STATUS_APPROVED: frozenset({STATUS_INSTALLING, STATUS_FAILED}),
    STATUS_INSTALLING: frozenset({STATUS_INSTALLED, STATUS_FAILED}),
    STATUS_INSTALLED: frozenset(),
    STATUS_FAILED: frozenset(),
    STATUS_REJECTED: frozenset(),
    STATUS_EXPIRED: frozenset(),
}

#: Default request lifetime when the config key is unset or unusable.
DEFAULT_TTL_HOURS = 24

#: How long a row may sit in ``approved`` before recovery stops trying
#: to start its install and fails it instead.  Generous on purpose: the
#: install is re-enqueued every sweep, so reaching this means the work
#: item is not being run at all (no main loop, a handler that is never
#: registered, a batch that cannot start), and an operator asking again
#: is a better answer than a row that waits forever.
DEFAULT_APPROVED_GRACE_HOURS = 6


# ── the table ───────────────────────────────────────────────────────

PENDING_PACKAGE_INSTALLS_DDL = """
CREATE TABLE IF NOT EXISTS pending_package_installs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- uuid4.  THE credential: whoever holds it decides this request, so
    -- it is single-use, time-limited, and logged only as its first 8
    -- characters (see ``token_prefix``).
    token           TEXT    NOT NULL UNIQUE,
    -- See the module docstring's state machine.  Enforced in
    -- ``set_status``, not by a CHECK constraint, so that an older
    -- platform reading a newer row is never stopped from reading it.
    status          TEXT    NOT NULL DEFAULT 'requested',
    -- UTC ISO-8601, same clock and format as installed_packages.installed_at.
    requested_at    TEXT    NOT NULL,
    expires_at      TEXT    NOT NULL,
    decided_at      TEXT,
    finished_at     TEXT,
    -- The conversation the request came from, so the operator can be
    -- told what happened where it was asked for.  No FK: conversations
    -- get pruned, and losing the conversation must not delete the audit
    -- record of an install.  NULL when the request had no conversation.
    conversation_id INTEGER,
    channel_type    TEXT,
    -- The verbatim user message that triggered the request.  This is
    -- the single most useful thing on the approval page: it is what
    -- lets a human notice "I never asked for this" when a third party
    -- talked the chat agent into asking.  Rendered ESCAPED.
    request_text    TEXT,
    -- JSON array of the model-supplied source spec strings, verbatim.
    sources_json    TEXT    NOT NULL,
    -- JSON array, one object per package, built WITHOUT executing any
    -- of the fetched code: source_id, kind, url_redacted, ref,
    -- resolved, subdir, tree_hash, bytes, files, manifest{}, caps[],
    -- creds[], allowlist[], chat_tools[], already_installed.
    preview_json    TEXT    NOT NULL,
    -- Quarantine directory holding the fetched trees for this batch.
    -- Kept, never re-fetched: the operator approved these bytes.
    staging_root    TEXT,
    -- JSON {pkg: {capabilities: bool, write_chat_tools: bool}} — what
    -- the operator granted, written with the decision.
    grants_json     TEXT,
    -- JSON per-package outcome + rollback report, written when the
    -- batch finishes.
    result_json     TEXT,
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ppi_status  ON pending_package_installs(status);
CREATE INDEX IF NOT EXISTS idx_ppi_expires ON pending_package_installs(expires_at);
"""


def ensure_pending_install_table(conn: sqlite3.Connection) -> None:
    """Create ``pending_package_installs`` and its indexes if missing.

    Called from :func:`carpenter.packages.installer.ensure_installer_tables`,
    which is what both the migration path and every test that needs the
    installer tables go through — the table is not in ``schema.sql``
    (nor is ``installed_packages``).  Idempotent.
    """
    conn.executescript(PENDING_PACKAGE_INSTALLS_DDL)


# ── errors ──────────────────────────────────────────────────────────

class PendingInstallError(Exception):
    """A pending-install row could not be read or written."""


class PendingInstallStateError(PendingInstallError):
    """An illegal state transition was refused.

    Raised rather than returning False so a caller cannot ignore it: a
    second decision on a spent token, or an install of a row nobody
    approved, must fail loudly.
    """


# ── the row ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PendingInstall:
    """One pending (or decided) install request.

    The ``*_json`` columns are decoded once here so no caller has to
    remember which are JSON; a column that holds unreadable JSON decodes
    to the empty default rather than raising, because a request row is
    also an audit record and must stay readable.
    """

    id: int
    token: str
    status: str
    requested_at: str
    expires_at: str
    decided_at: str | None = None
    finished_at: str | None = None
    conversation_id: int | None = None
    channel_type: str | None = None
    request_text: str | None = None
    sources: tuple[str, ...] = ()
    preview: tuple[dict, ...] = ()
    staging_root: str | None = None
    grants: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)
    error: str | None = None
    attempts: int = 0

    @property
    def token_prefix(self) -> str:
        """The loggable part of the token."""
        return token_prefix(self.token)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def is_expired(self, *, now: str | None = None) -> bool:
        """Whether this request's TTL has run out.

        Compared as ISO-8601 strings, which sort correctly because every
        timestamp here is written by :func:`utc_now_iso` (same offset,
        same precision).  A row already in a terminal state is never
        "expired" — it has an outcome, which is more informative.
        """
        if self.is_terminal:
            return False
        return (now or utc_now_iso()) > self.expires_at


# ── helpers ─────────────────────────────────────────────────────────

def utc_now_iso() -> str:
    """UTC ISO-8601 now, matching ``installed_packages.installed_at``."""
    return datetime.now(timezone.utc).isoformat()


def new_token() -> str:
    """Mint a request token: a uuid4, 122 bits of entropy.

    The same contract as a review link's id (``api/review.py``): the
    URL is the credential, so the token must be unguessable and is
    never derived from anything about the request.
    """
    return str(uuid.uuid4())


def token_prefix(token: str) -> str:
    """The first 8 characters — the only part of a token that may be logged."""
    return (token or "")[:8]


def default_ttl_hours(config: dict | None = None) -> float:
    """Request lifetime in hours, from ``package_install.request_ttl_hours``.

    Falls back to :data:`DEFAULT_TTL_HOURS` when the key is absent or
    not a usable positive number, because a malformed config value must
    not produce a request that never expires.
    """
    section: Any
    if config is not None:
        section = config.get("package_install")
    else:
        from .. import config as _config
        section = _config.CONFIG.get("package_install")
    if isinstance(section, dict):
        raw = section.get("request_ttl_hours", DEFAULT_TTL_HOURS)
    else:
        raw = DEFAULT_TTL_HOURS
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_TTL_HOURS)
    if hours <= 0:
        return float(DEFAULT_TTL_HOURS)
    return hours


def approved_grace_hours(config: dict | None = None) -> float:
    """Grace period in hours, from ``package_install.approved_grace_hours``.

    Falls back to :data:`DEFAULT_APPROVED_GRACE_HOURS` when the key is
    absent or not a usable positive number, for the same reason
    :func:`default_ttl_hours` does: a malformed config value must not
    produce a row nothing ever gives up on.
    """
    section: Any
    if config is not None:
        section = config.get("package_install")
    else:
        from .. import config as _config
        section = _config.CONFIG.get("package_install")
    if isinstance(section, dict):
        raw = section.get("approved_grace_hours", DEFAULT_APPROVED_GRACE_HOURS)
    else:
        raw = DEFAULT_APPROVED_GRACE_HOURS
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_APPROVED_GRACE_HOURS)
    if hours <= 0:
        return float(DEFAULT_APPROVED_GRACE_HOURS)
    return hours


def expires_at_for(
    requested_at: str, *, ttl_hours: float | None = None,
    config: dict | None = None,
) -> str:
    """Return the expiry timestamp for a request made at ``requested_at``."""
    hours = ttl_hours if ttl_hours is not None else default_ttl_hours(config)
    try:
        start = datetime.fromisoformat(requested_at)
    except ValueError as exc:
        raise PendingInstallError(
            f"requested_at {requested_at!r} is not an ISO-8601 timestamp",
        ) from exc
    return (start + timedelta(hours=float(hours))).isoformat()


def _loads(raw, default):
    if raw in (None, ""):
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "pending_package_installs: unreadable JSON column; "
            "treating it as empty",
        )
        return default
    return value


def _row_to_record(row) -> PendingInstall:
    """Build a :class:`PendingInstall` from a row (``sqlite3.Row`` or tuple)."""
    data = dict(row) if not isinstance(row, dict) else dict(row)
    sources = _loads(data.get("sources_json"), [])
    preview = _loads(data.get("preview_json"), [])
    grants = _loads(data.get("grants_json"), {})
    result = _loads(data.get("result_json"), {})
    return PendingInstall(
        id=int(data["id"]),
        token=data["token"],
        status=data["status"],
        requested_at=data["requested_at"],
        expires_at=data["expires_at"],
        decided_at=data.get("decided_at"),
        finished_at=data.get("finished_at"),
        conversation_id=data.get("conversation_id"),
        channel_type=data.get("channel_type"),
        request_text=data.get("request_text"),
        sources=tuple(str(s) for s in sources) if isinstance(sources, list) else (),
        preview=tuple(p for p in preview if isinstance(p, dict)) if isinstance(preview, list) else (),
        staging_root=data.get("staging_root"),
        grants=grants if isinstance(grants, dict) else {},
        result=result if isinstance(result, dict) else {},
        error=data.get("error"),
        attempts=int(data.get("attempts") or 0),
    )


def _fetch_one(conn: sqlite3.Connection, where: str, params: tuple):
    cur = conn.execute(
        f"SELECT * FROM pending_package_installs WHERE {where}", params,
    )
    cur.row_factory = sqlite3.Row
    row = cur.fetchone()
    if row is None:
        return None
    return _row_to_record(row)


# ── create / read ───────────────────────────────────────────────────

def create_request(
    conn: sqlite3.Connection,
    *,
    sources,
    preview,
    token: str | None = None,
    request_text: str | None = None,
    conversation_id: int | None = None,
    channel_type: str | None = None,
    staging_root=None,
    requested_at: str | None = None,
    ttl_hours: float | None = None,
    config: dict | None = None,
) -> PendingInstall:
    """Insert a ``requested`` row and return it.

    The caller owns the transaction (as everywhere else in
    ``carpenter.packages``), so a request and whatever else the same
    turn writes commit together or not at all.

    Args:
        conn: Open connection, inside the caller's transaction.
        sources: The model-supplied source spec strings, verbatim.
        preview: One dict per package, built without executing any
            fetched code.
        token: Override for the minted token (tests).
        request_text: The verbatim triggering user message.
        conversation_id: Conversation the request came from, or None.
        channel_type: That conversation's channel type, or None.
        staging_root: Quarantine directory holding the fetched trees.
        requested_at: Override for the request timestamp (tests).
        ttl_hours: Override for the request lifetime.
        config: Config mapping to read the TTL from (tests), instead of
            the live ``CONFIG``.

    Raises:
        PendingInstallError: ``sources`` is empty, or the token collides
            with an existing row.
    """
    source_list = [str(s) for s in (sources or ())]
    if not source_list:
        raise PendingInstallError(
            "create_request: a request must name at least one source",
        )
    preview_list = [dict(p) for p in (preview or ())]
    tok = token or new_token()
    at = requested_at or utc_now_iso()
    expires = expires_at_for(at, ttl_hours=ttl_hours, config=config)
    try:
        cur = conn.execute(
            """
            INSERT INTO pending_package_installs (
                token, status, requested_at, expires_at,
                conversation_id, channel_type, request_text,
                sources_json, preview_json, staging_root
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tok, STATUS_REQUESTED, at, expires,
                conversation_id, channel_type, request_text,
                json.dumps(source_list), json.dumps(preview_list),
                str(staging_root) if staging_root is not None else None,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise PendingInstallError(
            f"could not create install request {token_prefix(tok)}: {exc}",
        ) from exc
    record = _fetch_one(conn, "id = ?", (cur.lastrowid,))
    if record is None:  # pragma: no cover — defensive
        raise PendingInstallError("install request vanished after insert")
    return record


def get_request(conn: sqlite3.Connection, token: str) -> PendingInstall | None:
    """Return the request a token names, or None.

    An unknown token is not an error here: the route turns it into a
    404 that looks exactly like an expired-and-swept one, so probing
    cannot distinguish "never existed" from "already gone".
    """
    if not token:
        return None
    return _fetch_one(conn, "token = ?", (token,))


def get_request_by_id(
    conn: sqlite3.Connection, request_id: int,
) -> PendingInstall | None:
    """Return the request with this row id, or None.

    The work-queue payload carries the id as well as the token, so the
    handler can load the row without putting the credential in the
    queue table.
    """
    return _fetch_one(conn, "id = ?", (request_id,))


def list_requests(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[PendingInstall]:
    """Return requests, newest first, optionally filtered by status."""
    sql = "SELECT * FROM pending_package_installs"
    params: tuple = ()
    if status is not None:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY id DESC LIMIT ?"
    cur = conn.execute(sql, (*params, int(limit)))
    cur.row_factory = sqlite3.Row
    return [_row_to_record(r) for r in cur.fetchall()]


# ── rate-limit / sweep queries ──────────────────────────────────────

def count_active_requests(conn: sqlite3.Connection) -> int:
    """How many requests are waiting for a decision right now.

    Counts ``requested`` rows whose TTL has not run out, so a pile of
    stale rows the sweep has not reached yet cannot lock the chat agent
    out of asking.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM pending_package_installs "
        "WHERE status = ? AND expires_at > ?",
        (STATUS_REQUESTED, utc_now_iso()),
    ).fetchone()
    return int(row[0] if row else 0)


def count_requests_since(conn: sqlite3.Connection, since: str) -> int:
    """How many requests were created at or after ``since`` (ISO-8601).

    Counts every status: a rejected or expired request still cost the
    operator attention, so it still counts against the hourly rate.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM pending_package_installs "
        "WHERE requested_at >= ?",
        (since,),
    ).fetchone()
    return int(row[0] if row else 0)


def rejected_source_ids_since(
    conn: sqlite3.Connection, since: str,
) -> set[str]:
    """``source_id`` values the operator rejected at or after ``since``.

    Read from the previews of rejected rows, because that is where the
    identity of what was asked for lives.  Used to refuse re-asking for
    a source a human already said no to: without it, "ask again" is a
    free retry loop against a human's patience, which is the one
    defence that cannot be hardened.
    """
    cur = conn.execute(
        "SELECT preview_json FROM pending_package_installs "
        "WHERE status = ? AND COALESCE(decided_at, requested_at) >= ?",
        (STATUS_REJECTED, since),
    )
    out: set[str] = set()
    for (raw,) in cur.fetchall():
        for entry in _loads(raw, []) or []:
            if isinstance(entry, dict):
                sid = entry.get("source_id")
                if isinstance(sid, str) and sid:
                    out.add(sid)
    return out


def due_for_expiry(
    conn: sqlite3.Connection, *, now: str | None = None, limit: int = 200,
) -> list[PendingInstall]:
    """Requests past their TTL that are still waiting for a decision.

    The seam the periodic sweep and the startup recovery (WP5 T9) use:
    they mark each of these ``expired`` and remove its quarantine.  The
    GET/POST routes also expire lazily, so a sweep that never runs
    delays reclaiming disk but never lets a stale link be used.
    """
    cur = conn.execute(
        "SELECT * FROM pending_package_installs "
        "WHERE status = ? AND expires_at <= ? ORDER BY id LIMIT ?",
        (STATUS_REQUESTED, now or utc_now_iso(), int(limit)),
    )
    cur.row_factory = sqlite3.Row
    return [_row_to_record(r) for r in cur.fetchall()]


def approved_awaiting_install(
    conn: sqlite3.Connection, *, limit: int = 200,
) -> list[PendingInstall]:
    """Rows in ``approved``: decided, but the install has not claimed them.

    The install handler's first act is ``approved -> installing``, so a
    row still in ``approved`` means no handler has it.  Either the work
    item is queued and waiting (re-enqueuing is then a no-op, because
    the idempotency key already exists) or the enqueue never happened —
    it runs after the approval transaction commits, so a crash in
    between loses it.  The recovery seam re-enqueues both cases and lets
    the queue's own deduplication sort out which was which.
    """
    cur = conn.execute(
        "SELECT * FROM pending_package_installs "
        "WHERE status = ? ORDER BY id LIMIT ?",
        (STATUS_APPROVED, int(limit)),
    )
    cur.row_factory = sqlite3.Row
    return [_row_to_record(r) for r in cur.fetchall()]


#: What an operator reads on the status page of an approval the platform
#: could not start.  Says what to do, because there is nothing else they
#: can do: the token is spent and the row cannot be re-decided.
STALE_APPROVAL_ERROR = (
    "approved, but the install never started within the grace period "
    "(the platform was down, or the queued work item never ran). "
    "Nothing was installed and the quarantined copy has been removed; "
    "ask again if you still want it."
)


def fail_stale_approved(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
    grace_hours: float | None = None,
    config: dict | None = None,
    limit: int = 200,
) -> list[PendingInstall]:
    """Fail approvals whose install never started, and free their disk.

    ``approved`` is not a terminal status and not one
    :func:`due_for_expiry` selects, so without this a row whose enqueue
    was lost sits there forever: the page offers no buttons, a second
    POST is 410 and ``approved -> requested`` is not a legal edge.  The
    honest outcome after a bounded wait is a failure the operator can
    see, with "ask again" as the way forward.

    The caller owns the transaction.  Returns the rows it failed.
    """
    if not table_exists(conn):
        return []
    hours = (
        grace_hours if grace_hours is not None
        else approved_grace_hours(config)
    )
    cutoff = (
        datetime.fromisoformat(now or utc_now_iso())
        - timedelta(hours=float(hours))
    ).isoformat()
    cur = conn.execute(
        "SELECT * FROM pending_package_installs "
        "WHERE status = ? AND COALESCE(decided_at, requested_at) <= ? "
        "ORDER BY id LIMIT ?",
        (STATUS_APPROVED, cutoff, int(limit)),
    )
    cur.row_factory = sqlite3.Row
    stale = [_row_to_record(r) for r in cur.fetchall()]
    failed: list[PendingInstall] = []
    for record in stale:
        try:
            updated = set_status(
                conn, record.token, STATUS_FAILED,
                error=STALE_APPROVAL_ERROR, expect=STATUS_APPROVED,
            )
        except PendingInstallStateError:  # pragma: no cover — raced
            continue
        remove_staging(record)
        failed.append(updated)
    if failed:
        logger.warning(
            "Failed %d approved package install(s) that never started "
            "within %sh: %s",
            len(failed), hours, [r.token_prefix for r in failed],
        )
    return failed


# ── state machine ───────────────────────────────────────────────────

def set_status(
    conn: sqlite3.Connection,
    token: str,
    new_status: str,
    *,
    grants=None,
    result=None,
    error: str | None = None,
    decided_at: str | None = None,
    finished_at: str | None = None,
    bump_attempts: bool = False,
    expect: str | None = None,
) -> PendingInstall:
    """Move a request to ``new_status``, refusing an illegal transition.

    One compare-and-set, so two concurrent decisions on the same token
    cannot both win: the UPDATE carries the expected current status in
    its WHERE clause and a zero row count is the refusal.

    Args:
        conn: Open connection inside the caller's transaction.
        token: The request's token.
        new_status: Target status, one of :data:`STATUSES`.
        grants: Operator grants to record (written as JSON).
        result: Per-package outcome to record (written as JSON).
        error: Failure message to record.
        decided_at: Override for the decision timestamp; defaults to
            now for ``approved``/``rejected``.
        finished_at: Override for the finish timestamp; defaults to now
            for any terminal status.
        bump_attempts: Increment ``attempts`` (the install handler does
            this when it starts, so a re-dispatched work item is
            visible).
        expect: Require this exact current status, instead of accepting
            any status the edge is legal from.

    Returns:
        The updated row.

    Raises:
        PendingInstallStateError: unknown token, unknown status, or the
            transition is not allowed from the row's current status.
    """
    if new_status not in STATUSES:
        raise PendingInstallStateError(
            f"unknown pending-install status {new_status!r}",
        )
    record = get_request(conn, token)
    if record is None:
        raise PendingInstallStateError(
            f"no install request with token {token_prefix(token)}",
        )
    if expect is not None and record.status != expect:
        raise PendingInstallStateError(
            f"install request {token_prefix(token)} is {record.status!r}, "
            f"not {expect!r}",
        )
    allowed = _TRANSITIONS.get(record.status, frozenset())
    if new_status not in allowed:
        raise PendingInstallStateError(
            f"install request {token_prefix(token)} cannot go from "
            f"{record.status!r} to {new_status!r} "
            f"(allowed: {sorted(allowed) or 'none — terminal'})",
        )

    now = utc_now_iso()
    sets = ["status = ?"]
    params: list = [new_status]
    if new_status in (STATUS_APPROVED, STATUS_REJECTED):
        sets.append("decided_at = ?")
        params.append(decided_at or now)
    elif decided_at is not None:
        sets.append("decided_at = ?")
        params.append(decided_at)
    if new_status in TERMINAL_STATUSES or finished_at is not None:
        sets.append("finished_at = ?")
        params.append(finished_at or now)
    if grants is not None:
        sets.append("grants_json = ?")
        params.append(json.dumps(grants))
    if result is not None:
        sets.append("result_json = ?")
        params.append(json.dumps(result))
    if error is not None:
        sets.append("error = ?")
        params.append(error)
    if bump_attempts:
        sets.append("attempts = attempts + 1")

    cur = conn.execute(
        f"UPDATE pending_package_installs SET {', '.join(sets)} "
        f"WHERE token = ? AND status = ?",
        (*params, token, record.status),
    )
    if cur.rowcount != 1:
        raise PendingInstallStateError(
            f"install request {token_prefix(token)} changed status "
            f"underneath this update; refusing to {new_status}",
        )
    logger.warning(
        "Package install request %s: %s -> %s",
        token_prefix(token), record.status, new_status,
    )
    updated = get_request(conn, token)
    if updated is None:  # pragma: no cover — defensive
        raise PendingInstallStateError("install request vanished mid-update")
    return updated


def mark_approved(
    conn: sqlite3.Connection, token: str, *, grants: dict,
) -> PendingInstall:
    """Record the operator's approval and the grants they ticked."""
    return set_status(
        conn, token, STATUS_APPROVED,
        grants=grants, expect=STATUS_REQUESTED,
    )


def mark_rejected(
    conn: sqlite3.Connection, token: str, *, reason: str | None = None,
) -> PendingInstall:
    """Record the operator's rejection.  Nothing is installed, ever."""
    return set_status(
        conn, token, STATUS_REJECTED, error=reason, expect=STATUS_REQUESTED,
    )


def mark_expired(conn: sqlite3.Connection, token: str) -> PendingInstall:
    """Record that a request's TTL ran out before anyone decided it."""
    return set_status(
        conn, token, STATUS_EXPIRED, expect=STATUS_REQUESTED,
        error="the request expired before it was decided",
    )


def mark_installing(conn: sqlite3.Connection, token: str) -> PendingInstall:
    """Claim an approved request for installation.

    Also bumps ``attempts``: a work item re-dispatched after a restart
    shows up as a second attempt, and the handler refuses any row that
    is not ``approved``, so the claim is the idempotency point.
    """
    return set_status(
        conn, token, STATUS_INSTALLING,
        expect=STATUS_APPROVED, bump_attempts=True,
    )


def mark_installed(
    conn: sqlite3.Connection, token: str, *, result: dict,
) -> PendingInstall:
    """Record a finished batch and its per-package outcome."""
    return set_status(conn, token, STATUS_INSTALLED, result=result)


def mark_failed(
    conn: sqlite3.Connection, token: str, *, error: str, result=None,
) -> PendingInstall:
    """Record a failed batch, with whatever the rollback reported."""
    return set_status(conn, token, STATUS_FAILED, error=error, result=result)


# ── expiry, quarantine GC and restart recovery ──────────────────────
#
# Three callers, one implementation: the hourly heartbeat hook, the
# startup recovery pass, and (lazily, for the row in front of it) the
# approval route.  The route's lazy expiry is what makes a stale link
# unusable even if the sweep never runs; the sweep is what reclaims the
# disk the quarantine holds.

#: Marker for an install the platform was performing when it stopped.
INTERRUPTED_ERROR = "interrupted by restart"


def table_exists(conn: sqlite3.Connection) -> bool:
    """Whether ``pending_package_installs`` exists on this connection.

    The table is created by ``installer.ensure_installer_tables``, not by
    ``schema.sql``, so a database that has never had the installer tables
    — a minimal test DB, or a very old install before the first
    migration run — legitimately does not have it.  Startup recovery
    must be a no-op there rather than an error.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("pending_package_installs",),
    ).fetchone()
    return row is not None


def _staging_root_dir() -> str | None:
    """The quarantine root, or None when it cannot be determined."""
    try:
        from .. import config
        from .fetch import STAGING_DIR_NAME
    except ImportError:  # pragma: no cover — stripped build
        return None
    base_dir = config.CONFIG.get("base_dir")
    if not base_dir:
        return None
    from pathlib import Path
    return str((Path(base_dir) / "cache" / STAGING_DIR_NAME).resolve())


def remove_staging(record: PendingInstall) -> bool:
    """Delete a request's quarantine directory.

    Refuses anything that is not a proper subdirectory of the configured
    quarantine root: ``staging_root`` is a column, and a column that has
    been edited or mis-recorded must not be able to turn a routine
    expiry sweep into ``rmtree`` on an arbitrary path.  The root itself
    is also refused — removing it would take every *other* pending
    request's tree with it.

    Returns:
        True when a directory was removed.
    """
    import shutil
    from pathlib import Path

    raw = record.staging_root
    if not raw:
        return False
    root = _staging_root_dir()
    if root is None:
        logger.warning(
            "Not removing the quarantine of install request %s: the "
            "quarantine root is not configured",
            record.token_prefix,
        )
        return False
    try:
        candidate = Path(raw).resolve()
    except OSError:  # pragma: no cover — unreadable path
        return False
    if str(candidate) == root or root not in [str(p) for p in candidate.parents]:
        logger.warning(
            "Not removing the quarantine of install request %s: its "
            "recorded path is not inside the quarantine root",
            record.token_prefix,
        )
        return False
    if not candidate.is_dir():
        return False
    shutil.rmtree(candidate, ignore_errors=True)
    logger.info(
        "Removed the quarantine of install request %s", record.token_prefix,
    )
    return True


#: Prefixes a batch quarantine directory may carry: the current one and
#: the one rows written before the rename point at.  Nothing else in the
#: staging root is swept by directory — a bare ``_fetch-*`` there belongs
#: to a direct ``fetch_source`` call that has no request row at all.
_BATCH_DIR_PREFIXES = ("_req-", "req-")

#: How old an unowned batch directory must be before the sweep removes
#: it.  A request fetches into its directory *before* it writes its row,
#: so a young directory with no row may simply be a fetch in flight; a
#: fetch is capped at a minute or so per source, and this is an hour.
ORPHAN_QUARANTINE_GRACE_S = 3600.0


def sweep_orphan_quarantines(
    conn: sqlite3.Connection,
    *,
    min_age_s: float = ORPHAN_QUARANTINE_GRACE_S,
    limit: int = 500,
) -> list[str]:
    """Remove batch quarantines no live request owns.

    Every status transition reclaims its own quarantine, so this is for
    the case that has no row to hang the cleanup off: the platform died
    between the fetch and the insert, or a row was deleted by hand.
    Without it, up to one fetch cap of **untrusted fetched code** per
    crash stays on disk forever.

    What it will not do: touch anything that is not a direct child of
    the configured quarantine root, is not named like a batch directory,
    is younger than ``min_age_s``, or is the recorded ``staging_root`` of
    a request that is not in a terminal state.  Idempotent — a second
    run finds nothing left to remove.

    Returns the paths it removed.
    """
    import shutil
    from pathlib import Path

    if not table_exists(conn):
        return []
    root_str = _staging_root_dir()
    if root_str is None:
        return []
    root = Path(root_str)
    if not root.is_dir():
        return []

    placeholders = ",".join("?" * len(TERMINAL_STATUSES))
    rows = conn.execute(
        f"SELECT staging_root FROM pending_package_installs "
        f"WHERE staging_root IS NOT NULL AND staging_root != '' "
        f"AND status NOT IN ({placeholders})",
        tuple(sorted(TERMINAL_STATUSES)),
    ).fetchall()
    owned: set[str] = set()
    for row in rows:
        raw = row[0]
        try:
            owned.add(str(Path(raw).resolve()))
        except OSError:  # pragma: no cover — unreadable path
            continue

    cutoff = time.time() - max(float(min_age_s), 0.0)
    removed: list[str] = []
    for child in sorted(root.iterdir()):
        if len(removed) >= int(limit):
            break
        if not child.is_dir() or child.is_symlink():
            continue
        if not child.name.startswith(_BATCH_DIR_PREFIXES):
            continue
        try:
            if child.stat().st_mtime > cutoff:
                continue
        except OSError:  # pragma: no cover — vanished under us
            continue
        if str(child.resolve()) in owned:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed.append(str(child))
    if removed:
        logger.warning(
            "Removed %d orphaned package-install quarantine director(ies) "
            "that no live request owns: %s",
            len(removed), [Path(p).name for p in removed],
        )
    return removed


def sweep_expired(
    conn: sqlite3.Connection, *, now: str | None = None, limit: int = 200,
) -> list[PendingInstall]:
    """Expire every request past its TTL and reclaim its quarantine.

    Only ``requested`` rows are touched (:func:`due_for_expiry`): a
    decided request has an outcome, and an outcome is more informative
    than "expired".  A row whose transition loses a race with a
    concurrent decision is skipped rather than retried — the operator's
    decision wins.

    The caller owns the transaction.  Returns the rows it expired.
    """
    if not table_exists(conn):
        return []
    swept: list[PendingInstall] = []
    for record in due_for_expiry(conn, now=now, limit=limit):
        try:
            updated = mark_expired(conn, record.token)
        except PendingInstallStateError:
            # Decided between the SELECT and the UPDATE.
            continue
        remove_staging(record)
        swept.append(updated)
    if swept:
        logger.warning(
            "Expired %d undecided package-install request(s): %s",
            len(swept), [r.token_prefix for r in swept],
        )
    return swept


def fail_interrupted_installs(
    conn: sqlite3.Connection, *, limit: int = 200,
) -> list[PendingInstall]:
    """Fail any install that was in flight when the platform stopped.

    A row in ``installing`` with no live worker was interrupted.  It is
    never resumed: the batch's partial state is unknown and its
    compensating rollback may itself have been interrupted, so the
    honest outcome is a failure the operator can see, with
    ``verify_install`` and a fresh request as the way forward.  The
    re-dispatched work item is dropped by the handler's status check
    precisely because this ran first.

    The caller owns the transaction.  Returns the rows it failed.
    """
    if not table_exists(conn):
        return []
    cur = conn.execute(
        "SELECT * FROM pending_package_installs "
        "WHERE status = ? ORDER BY id LIMIT ?",
        (STATUS_INSTALLING, int(limit)),
    )
    cur.row_factory = sqlite3.Row
    interrupted = [_row_to_record(r) for r in cur.fetchall()]
    failed: list[PendingInstall] = []
    for record in interrupted:
        try:
            updated = set_status(
                conn, record.token, STATUS_FAILED,
                error=INTERRUPTED_ERROR, expect=STATUS_INSTALLING,
            )
        except PendingInstallStateError:  # pragma: no cover — raced
            continue
        # Terminal, so the quarantine is nobody's any more.  The batch
        # that was copying out of it is not coming back.
        remove_staging(record)
        failed.append(updated)
    if failed:
        logger.warning(
            "Failed %d package install(s) interrupted by a restart: %s. "
            "The partial state is unknown; run `python3 -m carpenter "
            "packages verify` before asking again.",
            len(failed), [r.token_prefix for r in failed],
        )
    return failed


def recover_on_startup(conn: sqlite3.Connection) -> tuple[int, int]:
    """Every startup sweep that is pure database work, in order.

    Returns ``(expired, interrupted)`` counts so the caller can fold
    them into its own startup-recovery log line; a stale approval
    failed here is counted as interrupted, because that is what it is.

    Re-enqueuing a live approval is deliberately NOT here: it needs
    ``work_queue.enqueue``, which opens a transaction of its own, and
    this runs inside the caller's.  That half is
    :func:`carpenter.packages.install_approved_handler.recover_approved_installs`,
    which the coordinator calls once the database is open.
    """
    if not table_exists(conn):
        return (0, 0)
    interrupted = len(fail_interrupted_installs(conn))
    interrupted += len(fail_stale_approved(conn))
    expired = len(sweep_expired(conn))
    # Last, so that the three passes above have already released the
    # directories they own: whatever is left belongs to nobody.
    sweep_orphan_quarantines(conn)
    return (expired, interrupted)
