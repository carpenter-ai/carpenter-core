"""Upgrade an installed capability package from its recorded source.

An *install* takes a source the operator names.  An **update** takes no
source at all: it re-fetches the one the install recorded, following
that source's *ref* rather than the commit it was pinned to, because
"follow the branch and tell me what changed" is the whole point of an
update.  A package with no recorded remote source therefore cannot be
updated, and is refused with the command that fixes it.

What makes this more than a re-install is the three-way reconcile.  The
installed tree is a working directory an operator may legitimately have
edited; re-installing over it clobbers those edits silently, which is
what the platform did before this module existed.  So an update
classifies every path across three trees — the pristine tree the
installed version shipped (``shipped_old``, from the local archive
cache), the tree on disk now (``live``) and the pristine tree the new
version ships (``shipped_new``) — using
:func:`carpenter.packages.reconcile.classify`, and materialises the
result through
:func:`carpenter.packages.reconcile_apply.apply_reconciled_install`.
There is exactly one merge engine and this module is not it.

Two decisions are the operator's, never ours:

* **Conflicts.**  A path that both the operator and upstream changed
  cannot be merged automatically.  :func:`plan_update` lists every such
  path and :func:`apply_update` refuses unless ``accept_conflicts`` is
  explicitly set — in which case the new version wins those paths and
  the result says which edits were overridden.
* **Versions.**  ``version`` is a free-form string with no ordering, so
  nothing here compares two of them for precedence.  "Same version,
  different bytes" and "the new version string sorts before the
  installed one" are both *reported* in the plan's notes and left to
  the operator to judge.

When the pristine tree of the installed version is unavailable (never
cached, or it no longer verifies against its recorded hash), there is no
anchor and no reconcile is possible.  The update then falls back to the
old clobbering swap — but only through a plan that says so in plain
words and lists the files it will overwrite.  Nothing here ever
clobbers silently.

Deliberately NOT done here: no chat tool, no HTTP route, no restart, no
process exit.  Whether the platform needs restarting to run the new code
is *reported* (:attr:`UpdateResult.needs_restart`, decided by the same
``sys.modules`` test :mod:`carpenter.packages.reload` applies) and acted
on by nobody.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import archive_cache, reconcile
from .archive_cache import ArchiveCacheError
from .fetch import FetchError, FetchLimits, FetchResult, cleanup, fetch_source
from .installer import (
    InstallError,
    _iter_files,
    compute_package_hash,
    compute_proposal_diff,
    get_install_record,
    granted_verbs_for_package,
)
from .manifest import ManifestError, PackageManifest, load_manifest
from .reconcile import FileStatus
from .reconcile_apply import apply_reconciled_install
from .security import PackageSecurityError, validate_manifest_security
from .sources import SourceSpec, describe
from .sources import source_id as source_id_of

logger = logging.getLogger(__name__)

__all__ = [
    "STRATEGY_CLOBBER",
    "STRATEGY_NOOP",
    "STRATEGY_RECONCILE",
    "UpdateError",
    "UpdatePlan",
    "UpdateResult",
    "apply_update",
    "cleanup_plan",
    "plan_update",
]

#: Three-way merge against the pristine tree of the installed version.
STRATEGY_RECONCILE = "reconcile"
#: No pristine anchor: overwrite the installed tree, losing local edits.
STRATEGY_CLOBBER = "clobber"
#: The source ships the same bytes that are already installed.
STRATEGY_NOOP = "noop"

#: How many paths a note lists before it says "and N more".
_NOTE_LIST_CAP = 25


class UpdateError(Exception):
    """Raised when a package cannot be planned for, or updated.

    Every failure mode of this module raises this one type with a
    message that names what the operator can do instead, so a CLI or a
    chat flow has one thing to catch and one thing to print.
    """


# ── the plan ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UpdatePlan:
    """What an operator is asked to approve before an update happens.

    Attributes:
        name: Installed package name.
        installed_version: ``installed_packages.version``.
        installed_hash: ``installed_packages.hash`` — the tree that was
            installed, which is not necessarily what is on disk now (an
            operator may have edited it).
        fetched_version: The ``version`` the newly fetched manifest
            declares.  Free-form: see the module docstring.
        fetched_hash: Root hash of the freshly fetched tree.
        resolved: What pins the fetched bytes — a git commit sha40 or an
            archive sha256.
        source: The :class:`~carpenter.packages.sources.SourceSpec`
            rebuilt from the install row's provenance columns, i.e. the
            source that was re-fetched.
        strategy: :data:`STRATEGY_RECONCILE`, :data:`STRATEGY_CLOBBER`
            or :data:`STRATEGY_NOOP`.
        local_edits: Paths whose on-disk content differs from what the
            installed version shipped — the operator's own changes.
            Empty for a clobber plan, where there is no anchor to tell
            an edit from a shipped file (the notes say so).
        conflicts: Paths that cannot be merged automatically because
            both the operator and upstream changed them.
        notes: Everything the operator should read before deciding, in
            plain sentences.
        install_path: The installed tree this plan would rewrite.
        fetched_tree: The fetched package root, inside the quarantine.
            :func:`apply_update` installs THESE bytes — it never
            re-fetches, because re-fetching at apply time would let a
            source swap the bytes the operator approved.
        staging: The quarantine directory to remove when the plan has
            been applied or abandoned (:func:`cleanup_plan`).
    """

    name: str
    installed_version: str
    installed_hash: str
    fetched_version: str
    fetched_hash: str
    resolved: str
    source: SourceSpec
    strategy: str
    local_edits: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    install_path: Path | None = None
    fetched_tree: Path | None = None
    staging: Path | None = None

    @property
    def is_noop(self) -> bool:
        """True when the source ships what is already installed."""
        return self.strategy == STRATEGY_NOOP

    @property
    def has_conflicts(self) -> bool:
        """True when applying needs ``accept_conflicts``."""
        return bool(self.conflicts)


@dataclass(frozen=True)
class UpdateResult:
    """What :func:`apply_update` did.

    Attributes:
        name: Package name.
        strategy: The plan's strategy, as carried out.
        changed: False only for a no-op (no DB write, no disk write).
        from_version / to_version: Recorded version before and after.
        from_hash: The previously recorded tree hash.
        to_hash: Hash of the tree now on disk.  For a reconcile this is
            the MERGED tree and therefore matches neither the old nor
            the fetched hash.
        shipped_hash: Root hash of the pristine tree the new version
            shipped — the anchor the next update reconciles against.
        resolved: The new commit sha / archive sha256.
        dest_path: The installed tree.
        files_written: Files materialised by the apply.
        local_edits_preserved: Operator edits the merge kept.
        conflicts_overridden: Operator edits the new version replaced
            because ``accept_conflicts`` was set.
        overwritten: Clobber only — the files the swap replaced.
        archive_cached: Whether the new version's pristine tree made it
            into the archive cache.  False means the NEXT update has no
            anchor and will have to clobber.
        subscriptions_registered: Trigger subscriptions re-recorded from
            the new manifest (the atomic swap removes the record, which
            lives inside the install directory).
        needs_restart: Whether the new code can be live in this process.
        restart_reason: Why, in words for the operator.
        notes: Carried over from the plan, plus anything the apply
            learned.
    """

    name: str
    strategy: str
    changed: bool
    from_version: str
    to_version: str
    from_hash: str
    to_hash: str
    shipped_hash: str
    resolved: str
    dest_path: Path | None
    files_written: int = 0
    local_edits_preserved: tuple[str, ...] = ()
    conflicts_overridden: tuple[str, ...] = ()
    overwritten: tuple[str, ...] = ()
    archive_cached: bool = False
    subscriptions_registered: int = 0
    needs_restart: bool = False
    restart_reason: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


# ── small helpers ───────────────────────────────────────────────────


def _read_tree(root: Path) -> dict[str, bytes]:
    """Read a package directory as ``{posix_rel_path: bytes}``.

    Uses :func:`carpenter.packages.installer._iter_files`, so the files
    the reconcile sees are exactly the files
    ``compute_package_hash`` measures — same ignore set (``__pycache__``,
    ``.pyc``, ``.git``, editor cruft, ``_subscriptions.json``).  A path
    the hash does not cover must not show up as an operator edit.
    """
    root = Path(root)
    return {
        p.relative_to(root.resolve()).as_posix(): p.read_bytes()
        for p in _iter_files(root)
    }


def _pristine_hash(record: dict) -> str:
    """Return the hash of the tree the installed version SHIPPED.

    ``shipped_hash`` when the row has it; otherwise ``hash``, which is
    correct for every install that materialised its source verbatim —
    i.e. everything written before reconciled upgrades existed.
    """
    shipped = record.get("shipped_hash")
    if isinstance(shipped, str) and shipped:
        return shipped
    return str(record["hash"])


def _listing(paths) -> str:
    """Render a path list for a note, capped so it stays readable."""
    paths = list(paths)
    head = ", ".join(paths[:_NOTE_LIST_CAP])
    if len(paths) > _NOTE_LIST_CAP:
        return f"{head}, and {len(paths) - _NOTE_LIST_CAP} more"
    return head


def cleanup_plan(plan: UpdatePlan) -> None:
    """Remove the quarantine a plan is holding.

    Safe to call more than once, and on a plan that never had one.
    :func:`apply_update` calls it for you; a caller that *abandons* a
    plan (the operator said no) must call it itself or the fetched tree
    stays on disk.
    """
    if plan.staging is None:
        return
    cleanup(FetchResult(
        tree=plan.fetched_tree or plan.staging,
        staging=plan.staging,
        resolved=plan.resolved,
        bytes_fetched=0,
        files_fetched=0,
    ))


# ── rebuilding the source from the install row ──────────────────────


def _spec_from_record(name: str, record: dict) -> SourceSpec:
    """Rebuild the recorded source as a :class:`SourceSpec`.

    The install row carries the source in seven columns; an update needs
    them back as a spec.  ``source_ref`` is what we put back as the ref
    — deliberately NOT ``source_resolved``: the resolved commit is the
    pin of the bytes we already have, and re-fetching it would always
    report "no change".  Following the ref is what an update IS.

    A spec is refused, rather than guessed at, in three cases, each with
    the command that fixes it:

    * no provenance at all (installed before provenance was recorded),
    * a local source (an operator's own directory has no ref to follow),
    * a URL stored redacted because it carried a credential — the
      credential is deliberately not persisted, so the URL cannot be
      reconstructed here.

    Raises:
        UpdateError: in any of those cases.
    """
    kind = (record.get("source_kind") or "").strip()
    url = (record.get("source_url") or "").strip()
    install_hint = (
        "Install it again naming the source — "
        "`python3 -m carpenter packages install <source>` — and the "
        "install records the provenance an update needs."
    )

    if not kind:
        raise UpdateError(
            f"package {name!r} has no recorded source, so there is "
            f"nothing to update from: it was installed before the "
            f"install recorded provenance, or by a caller that supplied "
            f"none. {install_hint}",
        )
    if kind == "local":
        where = record.get("source_path") or "a local directory"
        raise UpdateError(
            f"package {name!r} was installed from {where}, a local "
            f"directory. An update re-fetches a remote source at its "
            f"ref, and a local tree has no ref to follow. Re-install it "
            f"with `python3 -m carpenter packages install --path "
            f"{where}` after changing that tree, or install it from a "
            f"remote source to make it updatable.",
        )
    if not url:
        raise UpdateError(
            f"package {name!r} records source kind {kind!r} but no URL, "
            f"so the source cannot be re-fetched. {install_hint}",
        )
    if "***" in url:
        raise UpdateError(
            f"the recorded source URL for {name!r} is stored redacted "
            f"({url}) because it carried a credential, and a credential "
            f"is never persisted. The URL cannot be reconstructed here. "
            f"{install_hint}",
        )

    return SourceSpec(
        kind=kind,
        raw=url,
        url=url,
        ref=(record.get("source_ref") or None),
        subdir=(record.get("source_subdir") or None),
        # No digest: an archive source's recorded sha256 pins the bytes
        # we already have.  Pinning it would refuse every new release.
        digest=None,
    )


# ── planning ────────────────────────────────────────────────────────


def plan_update(
    name: str,
    *,
    conn: sqlite3.Connection | None = None,
    limits: FetchLimits | None = None,
) -> UpdatePlan:
    """Fetch the recorded source and plan the update it implies.

    Reads the DB, fetches into quarantine, and classifies — it writes
    nothing to the installed tree and nothing to the DB.  The returned
    plan holds the quarantine, which the caller must either apply
    (:func:`apply_update`, which cleans up) or abandon
    (:func:`cleanup_plan`).

    Args:
        name: Installed package name.
        conn: Open SQLite connection.  Omitted, a read-only connection
            is opened for the call.
        limits: Resource ceilings for the fetch.

    Returns:
        An :class:`UpdatePlan`.

    Raises:
        UpdateError: the package is not installed, its install
            directory is gone, it has no updatable recorded source, the
            fetch failed, or the fetched tree is not an installable
            package of the same name.
    """
    if conn is None:
        from ..db import db_connection
        with db_connection() as db:
            return plan_update(name, conn=db, limits=limits)

    record = get_install_record(conn, name)
    if record is None:
        raise UpdateError(
            f"package {name!r} is not installed, so there is nothing to "
            f"update. `python3 -m carpenter packages list` shows what is.",
        )
    install_path = Path(record["install_path"])
    if not install_path.is_dir():
        raise UpdateError(
            f"package {name!r} is recorded as installed at "
            f"{install_path}, but that directory does not exist. An "
            f"update reconciles against the installed tree, so fix the "
            f"install first: re-install the package, or uninstall it "
            f"and install it again.",
        )

    spec = _spec_from_record(name, record)
    try:
        fetched = fetch_source(spec, limits=limits)
    except FetchError as exc:
        # Nothing has been touched: no quarantine survives a failed
        # fetch (the dispatcher removes it), the installed tree is
        # untouched and the DB row is unread since.
        raise UpdateError(
            f"could not fetch the recorded source for {name!r} "
            f"({describe(spec)}): {exc}",
        ) from exc

    try:
        return _plan_from_fetched(
            name, record, spec, fetched, conn=conn,
        )
    except Exception:
        cleanup(fetched)
        raise


def _fetched_manifest(name: str, tree: Path) -> PackageManifest:
    """Load + security-validate the fetched tree's manifest.

    The same two gates ``install_package`` applies before it touches
    the destination, applied here before the operator is asked to
    approve anything.  Neither imports the package's python: the
    manifest is YAML and the guards read the tree, so a hostile source
    gets no execution out of being planned.
    """
    manifest_file = Path(tree) / "manifest.yaml"
    try:
        manifest = load_manifest(manifest_file)
        import yaml
        with open(manifest_file, encoding="utf-8") as fp:
            raw = yaml.safe_load(fp)
        if not isinstance(raw, dict):
            raise ManifestError(
                f"manifest at {manifest_file} must be a mapping",
            )
        validate_manifest_security(
            manifest, raw_manifest=raw, manifest_path=manifest_file,
        )
    except (ManifestError, PackageSecurityError) as exc:
        raise UpdateError(
            f"the newly fetched version of {name!r} is not installable: "
            f"{exc}. The installed copy is untouched.",
        ) from exc
    if manifest.name != name:
        raise UpdateError(
            f"the recorded source for {name!r} now publishes a package "
            f"called {manifest.name!r}. Refusing to install one package "
            f"over another; install {manifest.name!r} separately if that "
            f"is what you want.",
        )
    return manifest


def _version_notes(
    installed_version: str, fetched_version: str,
) -> list[str]:
    """Report what the two version strings do and do not tell us.

    ``version`` is free-form and unordered, so this never decides
    anything — it surfaces the two cases an operator would want to know
    about and leaves the judgement to them.
    """
    notes: list[str] = []
    if fetched_version == installed_version:
        notes.append(
            f"SAME VERSION, DIFFERENT BYTES: the source ships different "
            f"content under the same version string "
            f"{installed_version!r}. That is either a re-publish of that "
            f"version or a mistake at the source; version strings carry "
            f"no ordering, so nothing here can tell which. The installed "
            f"version's cached pristine tree is preserved under a "
            f"'superseded' name rather than overwritten, so this "
            f"update stays reconcilable.",
        )
    elif fetched_version != installed_version and (
        fetched_version < installed_version
    ):
        notes.append(
            f"POSSIBLE DOWNGRADE: the fetched version "
            f"{fetched_version!r} sorts BEFORE the installed "
            f"{installed_version!r} by plain string comparison. Version "
            f"strings have no defined ordering in this platform, so "
            f"nothing is enforced and this may be perfectly correct — "
            f"it is yours to judge.",
        )
    return notes


def _consent_notes(
    conn: sqlite3.Connection,
    name: str,
    record: dict,
    manifest: PackageManifest,
    tree: Path,
) -> list[str]:
    """Report what the new version asks for that an update cannot grant.

    An update carries the operator's existing consent forward and
    grants nothing new: platform-capability verbs, allowlist proposals
    and the write-chat-tools opt-in are all install-time decisions.
    Anything the new version adds is reported here so the operator
    knows a re-install is what grants it.
    """
    notes: list[str] = []

    granted = granted_verbs_for_package(conn, name)
    declared = {c.verb for c in manifest.platform_capabilities}
    ungranted = sorted(declared - granted)
    if ungranted:
        notes.append(
            f"NEW PLATFORM CAPABILITIES, NOT GRANTED: the fetched "
            f"version declares verb(s) {', '.join(ungranted)}, which "
            f"grant PLATFORM-LEVEL TRUST. An update never grants trust, "
            f"so they will NOT be registered. Install the package again "
            f"to consent to them explicitly.",
        )
    dropped = sorted(granted - declared)
    if dropped:
        notes.append(
            f"the fetched version no longer declares granted verb(s) "
            f"{', '.join(dropped)}; the grant goes with them.",
        )

    added = compute_proposal_diff(manifest, record).added
    if added:
        notes.append(
            f"the fetched version proposes {len(added)} new allowlist "
            f"entry/entries ("
            + _listing(f"{t}:{v}" for t, v in added)
            + "). An update does NOT merge allowlist proposals into the "
              "platform's security policy; install the package again to "
              "apply them.",
        )

    from . import static_preview
    preview = static_preview.inspect_package_chat_tools(tree, manifest)
    if preview.write_tools and not record.get("write_chat_tools_allowed"):
        notes.append(
            f"the fetched version ships write-capable chat tool(s) "
            f"({_listing(preview.write_tools)}) and this install has "
            f"the write opt-in OFF, so they will not register. That "
            f"opt-in is an install-time decision and carries forward "
            f"unchanged.",
        )
    if preview.unresolved:
        notes.append(
            f"the static read of the fetched tree's chat tools could "
            f"not settle {len(preview.unresolved)} item(s); the "
            f"package's code is never run to build this plan, so "
            f"anything it could not read is reported rather than "
            f"guessed.",
        )
    return notes


def _plan_from_fetched(
    name: str,
    record: dict,
    spec: SourceSpec,
    fetched: FetchResult,
    *,
    conn: sqlite3.Connection,
) -> UpdatePlan:
    """Classify a fetched tree against the install.  Owns no I/O errors."""
    manifest = _fetched_manifest(name, fetched.tree)
    fetched_hash = compute_package_hash(fetched.tree)
    install_path = Path(record["install_path"])
    installed_version = str(record["version"])
    installed_hash = str(record["hash"])
    pristine_hash = _pristine_hash(record)

    def _plan(strategy, **kw) -> UpdatePlan:
        return UpdatePlan(
            name=name,
            installed_version=installed_version,
            installed_hash=installed_hash,
            fetched_version=manifest.version,
            fetched_hash=fetched_hash,
            resolved=fetched.resolved,
            source=spec,
            strategy=strategy,
            install_path=install_path,
            fetched_tree=Path(fetched.tree),
            staging=fetched.staging,
            **kw,
        )

    # ── nothing new ────────────────────────────────────────────────
    if fetched_hash == pristine_hash:
        notes = [
            f"UP TO DATE: {describe(spec)} ships exactly the bytes "
            f"{name!r} already has (tree hash {fetched_hash[:12]}...), "
            f"so there is nothing to apply and nothing will be written "
            f"— not to the install directory and not to the database.",
        ]
        live_hash = compute_package_hash(install_path)
        if live_hash != pristine_hash:
            notes.append(
                "the installed tree has local modifications; a no-op "
                "update leaves them exactly as they are.",
            )
        if manifest.version != installed_version:
            notes.append(
                f"the source calls these same bytes "
                f"{manifest.version!r} while they are recorded as "
                f"{installed_version!r}; since nothing changed, the "
                f"recorded version is left alone too.",
            )
        return _plan(STRATEGY_NOOP, notes=tuple(notes))

    # Reached only when the bytes differ, so an equal version string
    # here always means "same version, different bytes".
    notes = _version_notes(installed_version, manifest.version)
    notes += _consent_notes(conn, name, record, manifest, fetched.tree)

    live = _read_tree(install_path)
    shipped_new = _read_tree(fetched.tree)

    # ── the pristine anchor ────────────────────────────────────────
    try:
        shipped_old = archive_cache.load_pristine_tree(
            name, installed_version, pristine_hash,
            source_id=(record.get("source_id") or None),
        )
    except ArchiveCacheError as exc:
        # The documented fallback.  No anchor means no way to tell an
        # operator's edit from a shipped file, so the only honest thing
        # is to say what will be lost.
        overwritten = sorted(set(live) & set(shipped_new))
        removed = sorted(set(live) - set(shipped_new))
        notes.append(
            f"NO PRISTINE ANCHOR, SO LOCAL EDITS WILL BE LOST: the "
            f"pristine tree of the installed version "
            f"{installed_version!r} is not available ({exc}), so this "
            f"update CANNOT be merged with your changes. Applying it "
            f"REPLACES the installed tree at {install_path} with the "
            f"fetched one: every edit you made to a file below is lost, "
            f"with no backup.",
        )
        if overwritten:
            notes.append(
                f"{len(overwritten)} file(s) will be OVERWRITTEN: "
                + _listing(overwritten),
            )
        if removed:
            notes.append(
                f"{len(removed)} file(s) will be REMOVED (the new "
                f"version does not ship them): " + _listing(removed),
            )
        notes.append(
            "to keep your changes, copy the installed directory aside "
            "before applying, or restore the pristine archive for "
            f"{installed_version!r} into the package archive cache and "
            "plan again.",
        )
        return _plan(STRATEGY_CLOBBER, notes=tuple(notes))

    # ── three-way reconcile ────────────────────────────────────────
    plan = reconcile.classify(shipped_old, shipped_new, live)
    local_edits = tuple(
        d.path for d in plan.deltas if d.current_hash != d.old_hash
    )
    conflicts = tuple(d.path for d in plan.conflicts())

    if local_edits:
        notes.append(
            f"{len(local_edits)} path(s) differ from what the installed "
            f"version shipped, i.e. your own changes: "
            + _listing(local_edits)
            + ". Those that the new version does not also change are "
              "kept.",
        )
    else:
        notes.append(
            "the installed tree matches what its version shipped, so "
            "there are no local changes to merge.",
        )
    if conflicts:
        notes.append(
            f"{len(conflicts)} path(s) CONFLICT — changed both by you "
            f"and by the new version, so they cannot be merged "
            f"automatically: " + _listing(conflicts)
            + ". Applying with conflicts accepted takes the NEW "
              "version's content for each of them and discards your "
              "edit there.",
        )
    return _plan(
        STRATEGY_RECONCILE,
        local_edits=local_edits,
        conflicts=conflicts,
        notes=tuple(notes),
    )


# ── applying ────────────────────────────────────────────────────────


def _resolve_tree(
    plan: reconcile.ReconcilePlan,
    shipped_new: dict[str, bytes],
    live: dict[str, bytes],
    *,
    accept_conflicts: bool,
) -> tuple[dict[str, bytes], tuple[str, ...], tuple[str, ...]]:
    """Turn a classification into the exact bytes to install.

    The per-status decisions, which are the reconcile module's own
    semantics rather than new policy:

    * upstream moved alone (``UPSTREAM_ONLY``, ``ADDED_UPSTREAM``) —
      take the new version's bytes,
    * the operator moved alone (``USER_ONLY``, ``ADDED_USER``) — keep
      theirs, including keeping a file they deleted deleted,
    * both landed on the same content (``CONVERGED``) or neither moved
      (``UNCHANGED``) — either copy will do,
    * upstream removed a file the operator had not touched
      (``REMOVED_UPSTREAM``) — remove it,
    * a conflict — the new version wins, and only with
      ``accept_conflicts``.

    Returns ``(resolved_tree, preserved, overridden)``.
    """
    resolved: dict[str, bytes] = {}
    preserved: list[str] = []
    overridden: list[str] = []

    for delta in plan.deltas:
        path, status = delta.path, delta.status
        if status in (FileStatus.UPSTREAM_ONLY, FileStatus.ADDED_UPSTREAM):
            resolved[path] = shipped_new[path]
        elif status in (FileStatus.UNCHANGED, FileStatus.CONVERGED):
            # Identical either side; prefer the shipped bytes so the
            # installed tree stays byte-faithful to the new version.
            # Both statuses require the path in ``new``, so the fallback
            # is defensive only — and it raises rather than inventing
            # content for a file we cannot account for.
            if path in shipped_new:
                resolved[path] = shipped_new[path]
            elif path in live:  # pragma: no cover — defensive
                resolved[path] = live[path]
            else:  # pragma: no cover — defensive
                raise UpdateError(
                    f"{status.value} path {path!r} is in neither tree",
                )
        elif status in (FileStatus.USER_ONLY, FileStatus.ADDED_USER):
            preserved.append(path)
            if path in live:
                resolved[path] = live[path]
            # else: the operator deleted it and upstream did not change
            # it — honour the deletion by leaving it out.
        elif status is FileStatus.REMOVED_UPSTREAM:
            continue
        elif status.is_conflict:
            if not accept_conflicts:  # pragma: no cover — gated earlier
                raise UpdateError(
                    f"refusing to resolve conflicting path {path!r} "
                    f"without accept_conflicts",
                )
            overridden.append(path)
            if path in shipped_new:
                resolved[path] = shipped_new[path]
            # else: upstream removed a file the operator had edited
            # (REMOVED_UPSTREAM_CONFLICT) — accepting means it goes.
        else:  # pragma: no cover — defensive
            raise UpdateError(
                f"unhandled reconcile status {status!r} for {path!r}",
            )
    return resolved, tuple(preserved), tuple(overridden)


def _restart_posture(name: str) -> tuple[bool, str]:
    """Report whether the new code can be live in THIS process.

    The same test :mod:`carpenter.packages.reload` applies, applied
    without calling it: ``reload_packages`` would *register* a package,
    and an update must report the restart requirement rather than act
    on it.  An update is by definition a re-install, so the answer is
    yes-restart whenever the package's modules are already imported or
    its components are already registered here — a re-import would
    return the old module bodies (``loaders._import_package_module``
    caches them).
    """
    from . import reload as reload_mod
    from .registry import get_registry

    cached = reload_mod.cached_module_names(name)
    registered = get_registry().get(name) is not None
    if cached:
        return True, (
            f"{len(cached)} module(s) of {name!r} are already imported "
            f"in this process ({cached[0]}); an import returns the "
            f"cached old bodies, so the new code only runs after a "
            f"platform restart"
        )
    if registered:
        return True, (
            f"{name!r} is already registered in this process from the "
            f"previous version's files; a platform restart is needed to "
            f"pick up the new ones"
        )
    return False, (
        f"no module of {name!r} is imported in this process, so nothing "
        f"stale is cached here. A running platform that HAS loaded it "
        f"still needs a restart to run the new code"
    )


def apply_update(
    plan: UpdatePlan,
    *,
    conn: sqlite3.Connection | None = None,
    accept_conflicts: bool = False,
) -> UpdateResult:
    """Carry out ``plan``, installing the bytes it was built from.

    Installs the tree :func:`plan_update` fetched into quarantine —
    never a fresh fetch, so the bytes the operator approved are the
    bytes that land — after re-checking that the tree still hashes to
    what the plan recorded and that the install row has not moved under
    us.

    On success the quarantine is removed.  On a refusal it is KEPT, so
    the same plan can be applied again with ``accept_conflicts`` without
    a second fetch (a re-fetch would let the source swap the bytes the
    operator approved); a caller that gives up calls
    :func:`cleanup_plan`.

    Args:
        plan: An :class:`UpdatePlan` from :func:`plan_update`.
        conn: Open SQLite connection.  Omitted, a ``db_transaction()``
            is opened for the call.
        accept_conflicts: Required to apply a plan with conflicts, and
            it means "take the new version's content for every
            conflicting path, discarding my edit there".

    Returns:
        An :class:`UpdateResult`.  A no-op plan returns
        ``changed=False`` having written nothing at all.

    Raises:
        UpdateError: the plan is stale (the quarantine is gone, the
            fetched tree changed, the install record moved, the
            pristine anchor disappeared), it has conflicts and
            ``accept_conflicts`` is not set, or the apply itself failed.
            In every case the installed tree and the DB row are exactly
            as they were.
    """
    if conn is None:
        from ..db import db_transaction
        with db_transaction() as db:
            return apply_update(
                plan, conn=db, accept_conflicts=accept_conflicts,
            )
    result = _apply(plan, conn=conn, accept_conflicts=accept_conflicts)
    # Only a success retires the quarantine.  A refusal — conflicts not
    # accepted, a stale plan — has changed nothing, so the fetched bytes
    # the operator was shown stay available for a retry with different
    # arguments; the caller abandons them with ``cleanup_plan``.
    cleanup_plan(plan)
    return result


def _apply(
    plan: UpdatePlan,
    *,
    conn: sqlite3.Connection,
    accept_conflicts: bool,
) -> UpdateResult:
    name = plan.name

    if plan.strategy == STRATEGY_NOOP:
        # Deliberately no DB write and no disk write: "already current"
        # is not an event worth recording, and re-recording the row
        # would move ``installed_at`` for nothing.
        return UpdateResult(
            name=name,
            strategy=STRATEGY_NOOP,
            changed=False,
            from_version=plan.installed_version,
            to_version=plan.installed_version,
            from_hash=plan.installed_hash,
            to_hash=plan.installed_hash,
            shipped_hash=plan.fetched_hash,
            resolved=plan.resolved,
            dest_path=plan.install_path,
            notes=plan.notes,
        )

    record = get_install_record(conn, name)
    if record is None:
        raise UpdateError(
            f"package {name!r} is no longer installed, so the plan made "
            f"for it cannot be applied. Nothing was changed.",
        )
    if (
        str(record["hash"]) != plan.installed_hash
        or str(record["version"]) != plan.installed_version
    ):
        raise UpdateError(
            f"the install record for {name!r} changed after this plan "
            f"was made (now {record['version']} / "
            f"{str(record['hash'])[:12]}..., planned against "
            f"{plan.installed_version} / {plan.installed_hash[:12]}...). "
            f"Nothing was changed; plan the update again.",
        )

    tree = plan.fetched_tree
    if tree is None or not Path(tree).is_dir():
        raise UpdateError(
            f"the fetched tree for {name!r} is gone ({tree}), so there "
            f"is nothing to install. A quarantine does not survive the "
            f"process that fetched it, nor a cleanup: plan the update "
            f"again, then apply that plan.",
        )
    if compute_package_hash(tree) != plan.fetched_hash:
        raise UpdateError(
            f"the fetched tree for {name!r} at {tree} no longer hashes "
            f"to what the plan recorded "
            f"({plan.fetched_hash[:12]}...). Refusing to install bytes "
            f"that are not the ones the plan described. Nothing was "
            f"changed.",
        )

    install_path = Path(record["install_path"])
    live = _read_tree(install_path) if install_path.is_dir() else {}
    shipped_new = _read_tree(tree)
    notes = list(plan.notes)
    preserved: tuple[str, ...] = ()
    overridden: tuple[str, ...] = ()
    overwritten: tuple[str, ...] = ()

    if plan.strategy == STRATEGY_CLOBBER:
        resolved_tree = dict(shipped_new)
        overwritten = tuple(sorted(set(live) & set(shipped_new)))
    elif plan.strategy == STRATEGY_RECONCILE:
        try:
            shipped_old = archive_cache.load_pristine_tree(
                name, plan.installed_version, _pristine_hash(record),
                source_id=(record.get("source_id") or None),
            )
        except ArchiveCacheError as exc:
            raise UpdateError(
                f"the pristine tree of {name!r} "
                f"{plan.installed_version!r} is no longer available "
                f"({exc}), so this plan's three-way merge cannot be "
                f"carried out. Nothing was changed; plan the update "
                f"again — it will offer the clobbering fallback, which "
                f"says what it would lose.",
            ) from exc

        classified = reconcile.classify(shipped_old, shipped_new, live)
        current_conflicts = tuple(d.path for d in classified.conflicts())
        current_edits = tuple(
            d.path for d in classified.deltas
            if d.current_hash != d.old_hash
        )
        if (current_conflicts, current_edits) != (
            plan.conflicts, plan.local_edits,
        ):
            raise UpdateError(
                f"the installed tree of {name!r} changed after this "
                f"plan was made (conflicts now "
                f"{list(current_conflicts)}, local edits now "
                f"{list(current_edits)}). Nothing was changed; plan the "
                f"update again so the decision is made on what is "
                f"actually there.",
            )
        if current_conflicts and not accept_conflicts:
            raise UpdateError(
                f"{len(current_conflicts)} path(s) of {name!r} were "
                f"changed both by you and by the new version and cannot "
                f"be merged automatically: "
                f"{_listing(current_conflicts)}. Nothing was changed. "
                f"Apply with accept_conflicts (CLI: "
                f"--accept-conflicts) to take the NEW version's content "
                f"for those paths and discard your edits there, or copy "
                f"your changes out of {install_path} first.",
            )
        resolved_tree, preserved, overridden = _resolve_tree(
            classified, shipped_new, live,
            accept_conflicts=accept_conflicts,
        )
    else:
        raise UpdateError(
            f"unknown update strategy {plan.strategy!r} for {name!r}",
        )

    # The apply is transactional: _atomic_copy_into_place rolls the
    # previous tree back on failure and the DB row is written only
    # after the swap, so an exception here leaves both as they were.
    try:
        applied = apply_reconciled_install(
            name,
            plan.fetched_version,
            resolved_tree,
            conn=conn,
            dest_path=install_path,
            provenance=plan.source,
            resolved=plan.resolved,
            # The anchor for the NEXT update: what upstream shipped,
            # not the merged tree that is now on disk.
            shipped_hash=plan.fetched_hash,
        )
    except (InstallError, OSError) as exc:
        raise UpdateError(
            f"installing the updated tree for {name!r} failed: {exc}. "
            f"The previous install and its database row are unchanged.",
        ) from exc

    # Cache the new version's PRISTINE tree (the fetched bytes, not the
    # merged install) so the next update has an anchor.  Best-effort,
    # exactly as install treats the cache — but a failure matters
    # enough to report, because it is what turns the next update into a
    # clobber.
    archive_cached = False
    try:
        archive_cache.store_archive(
            name, plan.fetched_version, tree,
            source_id=source_id_of(plan.source),
        )
        archive_cached = True
    except Exception as exc:  # noqa: BLE001 — cache is best-effort
        logger.warning(
            "Updated %r but could not cache its pristine tree; the next "
            "update will have no anchor to merge against",
            name, exc_info=True,
        )
        notes.append(
            f"the new version's pristine tree could NOT be cached "
            f"({exc}); the next update of {name!r} will find no anchor "
            f"and will only be able to offer the clobbering fallback.",
        )

    # Restore the trigger-subscription record.  It lives INSIDE the
    # install directory while being excluded from the package hash, so
    # the atomic swap drops it — and a fresh process reads that file to
    # re-register the package's subscriptions.  Without this, an update
    # would silently stop a package's triggers from firing after the
    # next restart.  This is the same call install makes and it imports
    # no package python; in-process Trigger CLASSES are deliberately
    # NOT re-instantiated here, because that does import the package's
    # code, which is precisely what a restart is for.
    subscriptions_registered = 0
    try:
        from .installer import _install_trigger_subscriptions
        subscriptions_registered = _install_trigger_subscriptions(
            load_manifest(install_path / "manifest.yaml"), install_path,
        )
    except Exception as exc:  # noqa: BLE001 — best effort, but reported
        logger.warning(
            "Updated %r but could not restore its trigger subscriptions",
            name, exc_info=True,
        )
        notes.append(
            f"the package's trigger subscriptions could not be "
            f"re-recorded ({exc}); its triggers may not fire after the "
            f"next restart until it is installed again.",
        )

    needs_restart, restart_reason = _restart_posture(name)
    logger.info(
        "Updated %r %s -> %s (%s, hash %s, %d file(s))",
        name, plan.installed_version, applied.version, plan.strategy,
        applied.hash[:12], applied.files_written,
    )
    return UpdateResult(
        name=name,
        strategy=plan.strategy,
        changed=True,
        from_version=plan.installed_version,
        to_version=applied.version,
        from_hash=plan.installed_hash,
        to_hash=applied.hash,
        shipped_hash=plan.fetched_hash,
        resolved=plan.resolved,
        dest_path=applied.dest_path,
        files_written=applied.files_written,
        local_edits_preserved=preserved,
        conflicts_overridden=overridden,
        overwritten=overwritten,
        archive_cached=archive_cached,
        subscriptions_registered=subscriptions_registered,
        needs_restart=needs_restart,
        restart_reason=restart_reason,
        notes=tuple(notes),
    )
