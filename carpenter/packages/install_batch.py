"""Perform an approved package install — out of the model turn.

This is the second half of the two-phase chat install (D11 in
``docs/2026-10-21_package-remote-sources-design.md``).  The first
half (:mod:`carpenter.packages.install_requests`) fetched the trees into
quarantine, previewed them without executing anything, and wrote a
``pending_package_installs`` row; a human then approved that row and
ticked the per-package grants.  This module takes it from there.

:func:`perform_install_batch` is **synchronous and blocking** by design.
It runs on a worker thread (``thread_pools.run_in_work_pool``), which is
what lets it open ``db_transaction()`` per package: that guard is
per-thread (``carpenter.db._transaction_guard``), so the async handler
that schedules this call must hold no transaction across it.  Doing the
install here rather than in the HTTP request also means a 10-60s
copytree + hash never stalls the event loop, and a browser refresh
cannot open a second install window — the work-queue item's idempotency
key and the row's state machine are the interlock.

Three properties this module exists to guarantee:

**The operator approved specific bytes.**  Each staged tree is re-hashed
and compared against the ``tree_hash`` recorded in the row's preview
before anything is installed.  A mismatch is a refusal, never a
re-fetch: re-fetching at approval time is a TOCTOU hole, where the
operator approves tree hash H and a hostile remote serves H' to the
second fetch.  A tree that has vanished is also a refusal — "ask again"
— for the same reason.

**A grant that was not ticked is not fatal.**  A package declaring
``platform_capabilities`` installs either way; an unticked grant simply
registers no verbs (``installer.confirm_platform_capabilities`` returns
``()``, and the loader registers only granted verbs).  That is what
makes a capability-declaring package installable from chat at all, which
is the whole point of the approval page.

**"Install X and Y" never ends in a silent half-state** (D5).  Packages
install in request order, each in its own transaction — there is no
cross-package DB transaction and ``_atomic_copy_into_place`` is
per-package, so cross-package atomicity does not exist and is not
faked.  Instead, the first failure stops the batch and compensates:
every package *this batch installed fresh* is uninstalled, and every
package this batch *upgraded* is left exactly where it is, because
rolling that one back would remove software the operator already had.
What could not be undone is named in the report rather than implied,
including the ``security_policies`` entries a rolled-back install
merged, which uninstall deliberately does not remove (SD5's one-way
ratchet).
"""

from __future__ import annotations

import io
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import installer, pending_install
from .pending_install import (
    STATUS_APPROVED,
    STATUS_INSTALLING,
    PendingInstall,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BatchResult",
    "PKG_STATUS_FAILED",
    "PKG_STATUS_INSTALLED",
    "PKG_STATUS_NOT_ATTEMPTED",
    "PKG_STATUS_ROLLED_BACK",
    "PKG_STATUS_UPGRADED",
    "describe_load",
    "perform_install_batch",
    "result_for_display",
    "summarise",
]


# ── per-package outcome vocabulary ──────────────────────────────────
#
# These are the ``status`` values in ``result_json["packages"][*]``.
# The chat notification is rendered from them, so they are part of this
# module's contract rather than private labels.

#: Installed for the first time by this batch.
PKG_STATUS_INSTALLED = "installed"
#: Replaced an existing install of the same name (``was_update``).
PKG_STATUS_UPGRADED = "upgraded"
#: Installed fresh by this batch, then uninstalled again because a later
#: package in the same batch failed.
PKG_STATUS_ROLLED_BACK = "rolled_back"
#: The package whose install raised.
PKG_STATUS_FAILED = "failed"
#: Never started: an earlier package in the batch failed first.
PKG_STATUS_NOT_ATTEMPTED = "not_attempted"


@dataclass(frozen=True)
class BatchResult:
    """What one approved batch did.

    Attributes:
        ok: True only when every package in the batch installed.
        error: One operator-facing sentence, set iff ``ok`` is False.
            This is what lands in ``pending_package_installs.error``.
        result: The ``result_json`` payload — per-package status plus
            the rollback report.  Always populated, including on a
            refusal, because "nothing was attempted" is itself the
            thing the operator needs told.
    """

    ok: bool
    error: str | None
    result: dict = field(default_factory=dict)


# ── connection handling ─────────────────────────────────────────────

@contextmanager
def _reading(conn):
    """Yield a connection for reads: the caller's, or a fresh one."""
    if conn is not None:
        yield conn
        return
    from ..db import db_connection
    with db_connection() as db:
        yield db


@contextmanager
def _writing(conn):
    """Yield a connection for one package's install or uninstall.

    With no ``conn``, each package gets its **own** ``db_transaction()``
    — the production shape, and why this function must run on a worker
    thread holding no outer transaction.  When a caller passes a
    connection it owns the transaction for the whole batch; that is a
    test convenience, and it changes the rollback's durability (one
    commit rather than one per package), not its logic.
    """
    if conn is not None:
        yield conn
        return
    from ..db import db_transaction
    with db_transaction() as db:
        yield db


# ── hash re-verification ────────────────────────────────────────────

def _verify_staged_trees(record: PendingInstall) -> list[str]:
    """Re-hash every staged tree against the approved preview.

    Returns a list of refusal sentences — empty when every tree is
    exactly the bytes the operator approved.  Checked for the whole
    batch *before* the first install, so the common failures (a swept
    quarantine, a tampered tree) install nothing at all and need no
    rollback.

    This is deliberately not re-checked immediately before each
    individual install: the quarantine is ``0700`` and the installer
    re-runs ``load_manifest`` + ``validate_manifest_security`` on the
    tree it copies, so the residual window is a local root, which owns
    the platform anyway.
    """
    problems: list[str] = []
    for entry in record.preview:
        name = _package_name(entry)
        raw_path = entry.get("tree_path") or ""
        if not raw_path:
            problems.append(
                f"{name}: the approved request does not record where its "
                f"fetched tree was staged, so the approved bytes cannot be "
                f"verified. Ask again.",
            )
            continue
        tree = Path(raw_path)
        if not tree.is_dir():
            problems.append(
                f"{name}: the fetched tree is no longer on disk; ask again. "
                f"It is never re-fetched, because the operator approved "
                f"particular bytes and a second fetch could return "
                f"different ones.",
            )
            continue
        approved = str(entry.get("tree_hash") or "")
        try:
            actual = installer.compute_package_hash(tree)
        except installer.InstallError as exc:
            problems.append(f"{name}: the staged tree could not be hashed ({exc}).")
            continue
        if actual != approved:
            problems.append(
                f"{name}: the staged tree no longer matches the bytes that "
                f"were approved (approved {approved[:12] or '<none>'}, now "
                f"{actual[:12]}). Nothing was installed and nothing was "
                f"re-fetched; ask again.",
            )
    return problems


# ── preview accessors ───────────────────────────────────────────────

def _package_name(entry: dict) -> str:
    manifest = entry.get("manifest") or {}
    name = manifest.get("name")
    return str(name) if name else str(entry.get("spec") or "<unknown>")


def _grant_for(record: PendingInstall, name: str) -> tuple[bool, bool]:
    """Return ``(capabilities, write_chat_tools)`` as the operator left them.

    Absent means **not granted**: a grant is only ever an explicit tick
    on the approval page, and a row whose ``grants_json`` is missing or
    malformed grants nothing rather than everything.
    """
    grants = record.grants if isinstance(record.grants, dict) else {}
    block = grants.get(name)
    if not isinstance(block, dict):
        return False, False
    return bool(block.get("capabilities")), bool(block.get("write_chat_tools"))


def _provenance_for(entry: dict):
    """Rebuild the :class:`SourceSpec` for an entry's provenance columns.

    Built from the preview rather than re-parsed from the spec string so
    it cannot depend on a config source registry that may have changed
    since the request was made.  ``url_redacted`` is the right value to
    pass: ``parse_source`` already produces a credential-free canonical
    URL, so the redaction is a no-op on it and ``source_id`` comes out
    identical to the one in the preview.
    """
    from .sources import SourceSpec

    try:
        return SourceSpec(
            kind=str(entry.get("kind") or ""),
            raw=str(entry.get("spec") or ""),
            url=(entry.get("url_redacted") or None),
            ref=(entry.get("ref") or None),
            subdir=(entry.get("subdir") or None),
        )
    except Exception:  # noqa: BLE001 — provenance is a record, not a gate
        logger.warning(
            "Could not rebuild provenance for %r; recording it as unknown",
            entry.get("spec"), exc_info=True,
        )
        return None


def _package_block(entry: dict, record: PendingInstall) -> dict:
    """The skeleton of one package's ``result_json`` entry."""
    manifest = entry.get("manifest") or {}
    already = entry.get("already_installed") or None
    granted, write_ok = _grant_for(record, _package_name(entry))
    return {
        "name": _package_name(entry),
        "spec": entry.get("spec") or "",
        "source_id": entry.get("source_id") or "",
        "kind": entry.get("kind") or "",
        "source_url": entry.get("url_redacted") or "",
        "requested_ref": entry.get("ref") or "",
        "resolved": entry.get("resolved") or "",
        "status": PKG_STATUS_NOT_ATTEMPTED,
        "version": manifest.get("version") or "",
        "hash": entry.get("tree_hash") or "",
        "hash_short": (entry.get("tree_hash") or "")[:12],
        "previous_version": (already or {}).get("version"),
        "capabilities_declared": len(entry.get("capabilities") or ()),
        "capabilities_granted": granted,
        "granted_verbs": [],
        "write_chat_tools_requested": bool(
            entry.get("write_consent_required"),
        ),
        "write_chat_tools_allowed": write_ok,
        "allowlist_added": [],
        "load": None,
        "rollback": None,
        "error": None,
    }


# ── the batch ───────────────────────────────────────────────────────

def perform_install_batch(
    request_id: int, *, conn=None,
) -> BatchResult:
    """Install every package of an approved request, or none of them.

    Synchronous: call it from a worker thread
    (``thread_pools.run_in_work_pool``), never from the event loop, and
    never while holding a ``db_transaction()`` on the calling thread.

    Does not move the request's status.  The caller
    (:mod:`carpenter.packages.install_approved_handler`) owns
    ``installing → installed|failed``, so the status write and the
    notification happen in one place and a restart mid-install is
    detectable as a row left in ``installing``.

    Args:
        request_id: ``pending_package_installs.id``.  The row carries
            the token, so the credential never has to ride in the
            work-queue payload.
        conn: Optional connection to use for every read and write.
            With ``None`` — production — each package's install gets its
            own ``db_transaction()``.

    Returns:
        :class:`BatchResult`.  Never raises for an install failure: a
        failure is data the operator has to be shown, so it comes back
        in ``error`` and ``result``.
    """
    started_at = utc_now_iso()
    with _reading(conn) as db:
        record = pending_install.get_request_by_id(db, request_id)

    if record is None:
        return BatchResult(
            ok=False,
            error=f"install request {request_id} no longer exists",
            result={
                "status": "failed", "packages": [],
                "started_at": started_at, "finished_at": utc_now_iso(),
            },
        )

    if record.status not in (STATUS_APPROVED, STATUS_INSTALLING):
        # Not an error worth raising: a re-dispatched work item, or a
        # row an operator-side sweep already failed.
        logger.warning(
            "Package install batch %s: refusing to install a request in "
            "status %r (expected %r or %r)",
            record.token_prefix, record.status,
            STATUS_APPROVED, STATUS_INSTALLING,
        )
        return BatchResult(
            ok=False,
            error=(
                f"the request is {record.status!r}, not approved; nothing "
                f"was installed"
            ),
            result=_skeleton(record, started_at, "failed"),
        )

    packages = [_package_block(entry, record) for entry in record.preview]
    result = _skeleton(record, started_at, "failed", packages=packages)

    if not packages:
        return BatchResult(
            ok=False,
            error="the request lists no packages; nothing was installed",
            result=result,
        )

    logger.warning(
        "Package install batch %s starting: %s (grants: %s)",
        record.token_prefix,
        [p["name"] for p in packages],
        {
            p["name"]: {
                "capabilities": p["capabilities_granted"],
                "write_chat_tools": p["write_chat_tools_allowed"],
            }
            for p in packages
        },
    )

    # 1. The bytes the operator approved, or nothing.
    problems = _verify_staged_trees(record)
    if problems:
        for block in packages:
            block["error"] = next(
                (p for p in problems if p.startswith(f"{block['name']}:")),
                None,
            )
        error = " ".join(problems)
        logger.warning(
            "Package install batch %s refused before installing anything: %s",
            record.token_prefix, error,
        )
        result["finished_at"] = utc_now_iso()
        result["error"] = error
        return BatchResult(ok=False, error=error, result=result)

    # 2. Install in request order, each in its own transaction.
    by_name = {block["name"]: block for block in packages}
    installed_fresh: list[str] = []
    failure: str | None = None

    for entry, block in zip(record.preview, packages):
        name = block["name"]
        granted = block["capabilities_granted"]
        write_ok = block["write_chat_tools_allowed"]
        tree = Path(entry["tree_path"])
        dest = installer.default_install_dest(name)
        # The StringIO keeps the PLATFORM-LEVEL TRUST banner out of the
        # server's stderr while preserving it verbatim in the log as the
        # consent record for this grant.
        cap_log = io.StringIO()
        try:
            with _writing(conn) as db:
                outcome = installer.install_package(
                    tree, dest, conn=db,
                    # ``_g`` binds the grant at definition time: a
                    # closure over the loop variable would read the next
                    # package's grant if the call were ever deferred.
                    capability_input_fn=(
                        lambda *_a, _g=granted, **_k: "yes" if _g else "no"
                    ),
                    capability_prompt_stream=cap_log,
                    allow_write_chat_tools=write_ok,
                    provenance=_provenance_for(entry),
                    resolved=str(entry.get("resolved") or ""),
                )
        except Exception as exc:  # noqa: BLE001 — any install failure
            block["status"] = PKG_STATUS_FAILED
            block["error"] = str(exc)
            failure = f"{name} failed to install: {exc}"
            logger.warning(
                "Package install batch %s: %s failed",
                record.token_prefix, name, exc_info=True,
            )
            break

        _record_success(block, outcome, cap_log, record)
        if outcome.was_update:
            block["status"] = PKG_STATUS_UPGRADED
        else:
            block["status"] = PKG_STATUS_INSTALLED
            installed_fresh.append(name)

    # 3. Compensate, or hot-load.
    if failure is not None:
        _compensate(record, by_name, installed_fresh, conn=conn, result=result)
        result["finished_at"] = utc_now_iso()
        result["error"] = failure
        logger.warning(
            "Package install batch %s failed: %s; outcome %s",
            record.token_prefix, failure,
            {p["name"]: p["status"] for p in packages},
        )
        return BatchResult(ok=False, error=failure, result=result)

    _hot_load(record, packages, result)
    result["status"] = "installed"
    result["finished_at"] = utc_now_iso()
    logger.warning(
        "Package install batch %s installed: %s",
        record.token_prefix,
        {p["name"]: p["status"] for p in packages},
    )
    return BatchResult(ok=True, error=None, result=result)


def _skeleton(
    record: PendingInstall, started_at: str, status: str,
    *, packages: list[dict] | None = None,
) -> dict:
    """The invariant shape of ``result_json``."""
    return {
        "status": status,
        "token_prefix": record.token_prefix,
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "packages": packages if packages is not None else [],
        "rolled_back": [],
        "not_rolled_back": [],
        "security_policies_kept": [],
        "hot_load": None,
        "error": None,
    }


def _record_success(
    block: dict, outcome, cap_log: io.StringIO, record: PendingInstall,
) -> None:
    """Fold one :class:`installer.InstallResult` into its report block."""
    block["version"] = outcome.version
    block["hash"] = outcome.hash
    block["hash_short"] = outcome.hash[:12]
    block["granted_verbs"] = [
        str(cap.get("verb")) for cap in outcome.platform_capabilities_granted
    ]
    block["write_chat_tools_allowed"] = bool(outcome.write_chat_tools_allowed)
    block["allowlist_added"] = [
        [str(policy_type), str(value)]
        for (policy_type, value) in outcome.allowlist_added
    ]
    transcript = cap_log.getvalue()
    if transcript:
        # The consent record: what the operator was shown, and that the
        # grant decision came from the approval page rather than a tty.
        logger.warning(
            "Capability consent transcript for %r (request %s, grant=%s):\n%s",
            block["name"], record.token_prefix,
            "GRANTED" if block["capabilities_granted"] else "DECLINED",
            transcript,
        )


# ── compensating rollback (D5 / plan §7) ────────────────────────────

def _compensate(
    record: PendingInstall,
    by_name: dict[str, dict],
    installed_fresh: list[str],
    *,
    conn,
    result: dict,
) -> None:
    """Undo what this batch installed fresh, and report what it cannot.

    The rollback set is exactly "the packages this batch installed for
    the first time", in reverse install order.  An **upgrade** is never
    rolled back: the operator already had that package, and uninstalling
    it would take away working software to compensate for a different
    package's failure.  ``force=True`` on the uninstall skips the
    blocking-arc guard, which can only have become non-empty through a
    trigger the package itself just started.
    """
    for name in reversed(installed_fresh):
        block = by_name.get(name, {})
        try:
            with _writing(conn) as db:
                installer.uninstall_package(name, conn=db, force=True)
        except Exception as exc:  # noqa: BLE001 — report, never mask
            block["rollback"] = (
                f"rollback FAILED: {name} was installed by this batch but "
                f"could not be uninstalled ({exc}). It is still on disk — "
                f"run `python3 -m carpenter packages verify` and remove it "
                f"by hand."
            )
            logger.warning(
                "Package install batch %s: rollback of %r FAILED",
                record.token_prefix, name, exc_info=True,
            )
            continue
        block["status"] = PKG_STATUS_ROLLED_BACK
        block["rollback"] = (
            f"{name} was installed by this batch and has been uninstalled "
            f"again, because a later package in the same request failed."
        )
        result["rolled_back"].append(name)
        logger.warning(
            "Package install batch %s: rolled back %r",
            record.token_prefix, name,
        )
        # SD5: uninstall does not remove merged allowlist entries, so
        # name the ones a rolled-back install left behind rather than
        # implying the rollback was total.
        for (policy_type, value) in block.get("allowlist_added") or ():
            result["security_policies_kept"].append({
                "type": policy_type, "value": value, "package": name,
            })

    for name, block in by_name.items():
        if block.get("status") != PKG_STATUS_UPGRADED:
            continue
        previous = block.get("previous_version") or "an earlier version"
        block["rollback"] = (
            f"{name} was upgraded from v{previous} to v{block['version']} "
            f"and cannot be rolled back: it was already installed, so "
            f"uninstalling it would remove software you already had. The "
            f"previous version's pristine tree is in the archive cache "
            f"(`python3 -m carpenter packages install --path <cached tree>` "
            f"to restore it), but that cache is keyed on name and version "
            f"with no source discriminator and a same-version re-install "
            f"overwrites it — so this is a report, not a promise."
        )
        result["not_rolled_back"].append(
            {"name": name, "reason": "it was an upgrade, not a fresh install"},
        )

    if result["security_policies_kept"]:
        logger.warning(
            "Package install batch %s: %d security_policies entry(ies) "
            "merged by rolled-back install(s) remain (SD5 one-way "
            "ratchet): %s",
            record.token_prefix, len(result["security_policies_kept"]),
            result["security_policies_kept"],
        )


# ── hot-load reporting (T10 / D7) ───────────────────────────────────

def describe_load(block: dict) -> str:
    """One clause telling the operator whether this package is usable now.

    Exactly one of three things is true of a package that installed, and
    the operator is told which: it is live, it needs a restart because
    this process already imported that package's modules, or the live
    registration did not happen and why.
    """
    load = block.get("load")
    if not isinstance(load, dict):
        return "not loaded: the live registration was not attempted"
    if load.get("loaded"):
        return "loaded live"
    if load.get("needs_restart"):
        return (
            "a platform restart is required (already imported in this "
            "process)"
        )
    reason = load.get("reason") or "no reason reported"
    return f"not loaded: {reason}"


def _hot_load(
    record: PendingInstall, packages: list[dict], result: dict,
) -> None:
    """Register the freshly installed packages in the live process.

    Best-effort by construction: the packages are on disk and recorded,
    so a failure here is a "restart required" report, never an install
    failure.  Deliberately does NOT restart or exit the process, and
    sets no restart-pending flag — telling the operator is this
    module's job; deciding what to do about it is theirs.
    """
    from . import reload as pkg_reload

    names = [block["name"] for block in packages]
    by_name = {block["name"]: block for block in packages}
    try:
        outcomes = pkg_reload.reload_packages(only=names)
    except Exception as exc:  # noqa: BLE001 — the install already stands
        logger.warning(
            "Package install batch %s: live load failed; the packages are "
            "installed and need a restart to become active",
            record.token_prefix, exc_info=True,
        )
        result["hot_load"] = {"attempted": True, "error": str(exc)}
        for block in packages:
            block["load"] = {
                "loaded": False,
                "needs_restart": True,
                "reason": (
                    f"the live load could not run ({exc}); the package is "
                    f"installed and a platform restart will activate it"
                ),
                "components": {},
            }
        return

    result["hot_load"] = {"attempted": True, "error": None}
    for outcome in outcomes:
        block = by_name.get(outcome.name)
        if block is None:
            continue
        block["load"] = {
            "loaded": bool(outcome.loaded),
            "needs_restart": bool(outcome.needs_restart),
            "reason": outcome.reason,
            "components": {k: int(v) for k, v in dict(outcome.components).items()},
        }
    for block in packages:
        if block.get("load") is None:
            block["load"] = {
                "loaded": False,
                "needs_restart": True,
                "reason": (
                    "the live load pass did not report on this package; a "
                    "platform restart will register it"
                ),
                "components": {},
            }
    logger.warning(
        "Package install batch %s live load: %s",
        record.token_prefix,
        {b["name"]: describe_load(b) for b in packages},
    )


def summarise(result: dict) -> list[str]:
    """One human line per package, for the chat notification.

    Lives here rather than in the notifier because the vocabulary of
    ``result_json`` is this module's; the notifier owns delivery.
    """
    lines: list[str] = []
    for block in result.get("packages") or ():
        if not isinstance(block, dict):
            continue
        lines.append(_summarise_one(block))
    return lines


def _summarise_one(block: dict) -> str:
    name = block.get("name") or "<unknown>"
    version = block.get("version") or "?"
    status = block.get("status") or PKG_STATUS_NOT_ATTEMPTED
    short = block.get("hash_short") or ""
    source = block.get("source_url") or block.get("spec") or "an unnamed source"

    headline = {
        PKG_STATUS_INSTALLED: f"installed {name} v{version}",
        PKG_STATUS_UPGRADED: (
            f"upgraded {name} to v{version} "
            f"(was v{block.get('previous_version') or '?'})"
        ),
        PKG_STATUS_ROLLED_BACK: f"rolled back {name} v{version}",
        PKG_STATUS_FAILED: f"FAILED to install {name}",
        PKG_STATUS_NOT_ATTEMPTED: f"did not attempt {name}",
    }.get(status, f"{status} {name}")

    parts = [headline]
    if short:
        parts.append(f"hash {short}")
    parts.append(f"from {source}")

    if block.get("capabilities_declared"):
        verbs = block.get("granted_verbs") or []
        parts.append(
            "granted platform verbs: " + ", ".join(verbs) if verbs
            else "no platform verbs granted (the package installed without them)"
        )
    if block.get("write_chat_tools_requested"):
        parts.append(
            "write-capable chat tools ENABLED"
            if block.get("write_chat_tools_allowed")
            else "write-capable chat tools left disabled"
        )
    if status in (PKG_STATUS_INSTALLED, PKG_STATUS_UPGRADED):
        parts.append(describe_load(block))
    if block.get("error"):
        parts.append(f"error: {block['error']}")
    if block.get("rollback"):
        parts.append(str(block["rollback"]))
    return "- " + "; ".join(parts)


def result_for_display(result: dict) -> dict[str, Any]:
    """Return the parts of ``result_json`` a surface should show.

    Kept small and explicit so a page or a chat message cannot acquire a
    dependency on an internal key by accident.
    """
    return {
        "status": result.get("status"),
        "lines": summarise(result),
        "rolled_back": list(result.get("rolled_back") or ()),
        "not_rolled_back": list(result.get("not_rolled_back") or ()),
        "security_policies_kept": list(
            result.get("security_policies_kept") or (),
        ),
        "error": result.get("error"),
    }
