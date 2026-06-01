"""File operations tool backend.

Provenance + cross-trust isolation (D10, 2026-04-29):

These handlers are exposed via dispatch (``files.read``, ``files.write``,
``files.list``).  Historically they had NO path allowlist, NO per-arc
scoping, and NO integrity_level check.  This is a real prompt-injection
gap: an untrusted arc can call ``files.write`` to materialise tainted
content at an arbitrary path; a later trusted arc calling ``files.read``
on that path smuggles untrusted bytes into a trusting AI's context.

The fix:

1.  ``handle_write`` looks up the writer's integrity_level (from the arc
    ``_caller_arc_id`` injected by the dispatch bridge) and INSERT-OR-
    REPLACEs a row into ``file_provenance`` keyed on the absolute realpath
    of the target.  An ``arc_history`` event of type ``file_written`` is
    appended on the writer's arc.

2.  ``handle_read`` looks up the writer's integrity_level for the same
    realpath.  If a row exists AND the writer was non-trusted AND the
    reader is trusted, the read is refused with a ``DispatchError``
    (status_code=403).  The error never echoes file bytes — only the
    path the reader supplied (which it already knows) and the writer's
    integrity level.

3.  Non-trusted writers are constrained to a workspace allowlist:
    ``{workspaces_dir}/arc-{arc_id}/...`` or
    ``{base_dir}/data/state/{arc_id}/...``.  Trusted arcs and
    chat-context callers (no caller arc) are unrestricted.

The provenance check sits inside the backend handler so that even though
``files.read`` and ``files.list`` are listed in
``_DEFAULT_SESSION_EXEMPT_TOOLS`` (i.e. exempt from session validation),
the cross-trust read refusal still applies.

This is forward-looking enforcement: pre-existing files (no provenance
row) read freely.  The policy applies to writes that happen *after*
enforcement is deployed.

Note on imports: ``DispatchError`` is imported lazily inside the
handlers because ``executor.dispatch_bridge`` imports from
``api.callbacks`` which in turn imports this module — top-level import
would form a cycle.
"""
import datetime
import json
import logging
import os

from .. import config
from ..db import db_connection, db_transaction
from ..security.platform_paths import (
    PATH_TIER_T0,
    PATH_TIER_T1,
    audit_path_decision,
    is_invisible,
    path_tier,
)

logger = logging.getLogger(__name__)


def _get_dispatch_error_cls():
    """Lazy import of DispatchError to avoid circular import via callbacks."""
    from ..executor.dispatch_bridge import DispatchError
    return DispatchError


def _arc_trust_context(arc_id: int | None) -> tuple[str | None, str | None]:
    """Return (integrity_level, agent_type) for an arc, or (None, None).

    ``agent_type`` is needed alongside ``integrity_level`` because the I2
    cross-trust read predicate carves out REVIEWER and JUDGE arcs (per
    ``docs/design.md`` §"Agent Types and Capabilities" + ``docs/trust-
    invariants.md`` §I3).  Trusted PLANNER / EXECUTOR / CHAT readers
    must still be refused: those run LLM agents whose context would be
    poisoned by untrusted bytes.  REVIEWERs are LLM agents, but they are
    specifically chartered to extract from untrusted sources via a
    constrained schema, with the surrounding review pipeline containing
    the data via JUDGE before any U->T promotion.  JUDGEs are not LLMs
    at all — they run deterministic platform code (``security/judge.py``)
    so there is no LLM context to poison.
    """
    if arc_id is None:
        return (None, None)
    with db_connection() as db:
        row = db.execute(
            "SELECT integrity_level, agent_type FROM arcs WHERE id = ?",
            (arc_id,),
        ).fetchone()
        if row is None:
            return (None, None)
        return (row["integrity_level"] or "trusted", row["agent_type"])


def _is_cross_trust_read_refused(
    writer_integrity: str,
    caller_integrity: str | None,
    caller_agent_type: str | None,
) -> bool:
    """Return True iff the (writer, caller) pair violates I2.

    Policy (per ``docs/design.md`` §"Agent Types and Capabilities" +
    ``docs/trust-invariants.md`` §I2 + §I3):

    - Trusted CHAT / PLANNER / EXECUTOR readers MUST NOT see bytes
      produced by a non-trusted writer.  These run LLM agents in a
      trusted context and reading raw untrusted bytes would smuggle
      attacker-controlled content into that LLM's context window.
    - REVIEWER readers ARE permitted.  REVIEWERs are LLM agents
      specifically chartered to extract from untrusted sources via a
      constrained schema, and the surrounding review pipeline contains
      the data via structured verdicts before any U->T promotion.
    - JUDGE readers ARE permitted.  JUDGEs are not LLMs at all — they
      run deterministic platform code (``security/judge.py``,
      ``core/arc_dispatch_handler.py::_run_judge_checks``) so there is
      no LLM context to poison.  In practice JUDGE's ``allowed_tools``
      does not currently include ``files.read``, so this is policy
      correctness rather than a live capability change; if a future
      change exposes ``files.read`` to JUDGE, the predicate is already
      consistent with I3.
    """
    if writer_integrity == "trusted":
        return False
    if caller_integrity != "trusted":
        return False
    if caller_agent_type in ("REVIEWER", "JUDGE"):
        return False
    return True


def _workspace_allowed_prefixes(arc_id: int) -> list[str]:
    """Return the list of realpath prefixes a non-trusted arc may write to.

    Used for the per-arc workspace allowlist enforced on writes from
    non-trusted arcs.  Trusted arcs and chat callers bypass this check.
    """
    workspaces_dir = config.get_config("workspaces_dir", "")
    base_dir = config.get_config("base_dir", "")
    prefixes: list[str] = []
    if workspaces_dir:
        prefixes.append(
            os.path.realpath(os.path.join(workspaces_dir, f"arc-{arc_id}"))
            + os.sep
        )
    if base_dir:
        prefixes.append(
            os.path.realpath(
                os.path.join(base_dir, "data", "state", str(arc_id))
            )
            + os.sep
        )
    return prefixes


def _per_arc_workspace_roots() -> list[str]:
    """Return the realpath prefixes under which any per-arc workspace
    may live.  Used for the symlink-TOCTOU read-side check.
    """
    workspaces_dir = config.get_config("workspaces_dir", "")
    base_dir = config.get_config("base_dir", "")
    roots: list[str] = []
    if workspaces_dir:
        roots.append(os.path.realpath(workspaces_dir) + os.sep)
    if base_dir:
        roots.append(
            os.path.realpath(os.path.join(base_dir, "data", "state"))
            + os.sep
        )
    return roots


def _is_workspace_path(lexical_path: str) -> bool:
    """Return True if ``lexical_path`` is lexically inside any per-arc
    workspace root.  ``lexical_path`` is expected to be absolute and
    lexically normalised (no symlink resolution).  Workspace roots are
    realpath-resolved so installs with a symlinked ``workspaces_dir``
    still match correctly.
    """
    for root in _per_arc_workspace_roots():
        if lexical_path == root.rstrip(os.sep) or lexical_path.startswith(root):
            return True
    return False


def _record_provenance(
    realpath: str, writer_arc_id: int, writer_integrity: str
) -> None:
    """INSERT OR REPLACE a provenance row + append arc_history event."""
    now = datetime.datetime.utcnow().isoformat()
    with db_transaction() as db:
        db.execute(
            "INSERT OR REPLACE INTO file_provenance "
            "(path, writer_arc_id, writer_integrity_level, written_at) "
            "VALUES (?, ?, ?, ?)",
            (realpath, writer_arc_id, writer_integrity, now),
        )
        db.execute(
            "INSERT INTO arc_history "
            "(arc_id, entry_type, content_json, actor) "
            "VALUES (?, ?, ?, ?)",
            (
                writer_arc_id,
                "file_written",
                json.dumps({
                    "path": realpath,
                    "writer_integrity_level": writer_integrity,
                }),
                "platform",
            ),
        )


def _lookup_provenance(realpath: str) -> dict | None:
    """Return the provenance row for ``realpath`` (or None)."""
    with db_connection() as db:
        row = db.execute(
            "SELECT writer_arc_id, writer_integrity_level, written_at "
            "FROM file_provenance WHERE path = ?",
            (realpath,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)


def chat_read_provenance_check(path: str) -> str | None:
    """Return a refusal message if a chat-tool ``read_file`` on ``path``
    would violate I2, else None.

    Chat agents are implicitly TRUSTED (per ``docs/design.md``
    §"Agent Types and Capabilities": "CHAT — Context is TRUSTED only")
    and have no caller-arc id to inject into ``handle_read``, so the
    cross-trust check there is unreachable on the chat path.  Chat tools
    must therefore consult ``file_provenance`` directly before reading.

    Returns a denial string (matching the ``_check_path`` style used by
    sibling chat tools) when the path's recorded writer is non-trusted;
    the bytes themselves are never opened.  Predates-enforcement files
    (no provenance row) read freely.
    """
    realpath = os.path.realpath(path)
    # Platform-integrity tier check (I12).  T0 paths are invisible —
    # return a chat-friendly denial without echoing bytes or revealing
    # whether the file exists beyond what the caller already knows.
    if path_tier(realpath) == PATH_TIER_T0:
        audit_path_decision(
            None,
            "t0_read_refused",
            realpath,
            {"tool": "chat.read_file"},
        )
        return (
            "Access denied: path is platform-invisible "
            "(credentials, platform database, or other restricted "
            "platform state)."
        )
    prov = _lookup_provenance(realpath)
    if prov is None:
        return None
    writer_integrity = prov["writer_integrity_level"]
    if writer_integrity == "trusted":
        return None
    logger.warning(
        "chat read_file refused: file at %s written by %s arc %s; "
        "chat context is TRUSTED-only (I2)",
        realpath,
        writer_integrity,
        prov["writer_arc_id"],
    )
    # Refusal message must NOT echo file bytes.  Path is information
    # the caller already has.
    return (
        "Access denied: this file was written by a non-trusted arc; "
        "chat agents may not read it (I2 — trusted context isolation)."
    )


def handle_read(params: dict) -> dict:
    """Read a file, enforcing cross-trust provenance refusal.

    If a non-trusted arc previously wrote to this path (per the
    ``file_provenance`` table) and the caller is a trusted arc, refuse
    with a 403-style DispatchError.  The error must NOT echo file bytes.

    Symlink-TOCTOU hardening (PR #293 follow-up): if the supplied path
    lexically lives inside a per-arc workspace AND ``realpath(path)``
    differs from the lexically-normalised path, refuse the read.  A
    tainted writer could otherwise replace a workspace file with a
    symlink pointing at an unrelated on-disk file and bypass the
    provenance lookup (which is keyed on realpath of the *target*, not
    the workspace path).  Workspace files are platform-managed and not
    expected to traverse symlinks; this check is specific to workspace
    paths so non-workspace reads (e.g. trusted arcs reading config or
    KB files via symlinks) are unaffected.
    """
    path = params["path"]
    lexical_path = os.path.normpath(os.path.abspath(path))
    realpath = os.path.realpath(path)

    caller_arc_id = params.get("_caller_arc_id")
    caller_integrity, caller_agent_type = _arc_trust_context(caller_arc_id)

    # Symlink-TOCTOU hardening.  Triggers only for workspace paths to
    # avoid affecting trusted reads of legitimately-symlinked config or
    # KB files.  Refuse iff the path is inside a per-arc workspace AND
    # symlink resolution diverged from lexical normalisation.
    if lexical_path != realpath and _is_workspace_path(lexical_path):
        logger.warning(
            "files.read refused: workspace path %s contains symlink "
            "redirection (realpath=%s); refusing for caller arc %s",
            lexical_path,
            realpath,
            caller_arc_id,
        )
        DispatchError = _get_dispatch_error_cls()
        raise DispatchError(
            "workspace file path resolves through a symlink; "
            "refusing to follow symlinks inside per-arc workspaces",
            status_code=403,
        )

    # Platform-integrity tier check (I12).  T0 paths (credentials,
    # platform.db, secrets) are invisible — refuse the read regardless
    # of caller integrity.  This is the first enforcement target for
    # tier classification on the read path.  Reads of T1 (platform
    # source) are intentionally allowed: trusted coding agents
    # legitimately read source to understand context.
    if path_tier(realpath) == PATH_TIER_T0:
        audit_path_decision(
            caller_arc_id,
            "t0_read_refused",
            realpath,
            {
                "tool": "files.read",
                "caller_integrity": caller_integrity,
            },
        )
        logger.warning(
            "files.read refused: T0 (invisible) path %s for caller arc %s",
            realpath,
            caller_arc_id,
        )
        DispatchError = _get_dispatch_error_cls()
        raise DispatchError(
            "path is platform-invisible (T0)",
            status_code=403,
        )

    # Cross-trust read refusal — I2 enforcement on the dispatch-bridge
    # path.  Forward-looking: files with no provenance row predate
    # enforcement and read freely.  REVIEWER carve-out is in the
    # predicate (see ``_is_cross_trust_read_refused``).  Chat-tool reads
    # do not flow through here — they are enforced separately in
    # ``chat_read_provenance_check`` because the chat tool has no
    # ``_caller_arc_id`` to inject (chat is implicitly TRUSTED per
    # ``docs/design.md``).
    prov = _lookup_provenance(realpath)
    if prov is not None:
        writer_integrity = prov["writer_integrity_level"]
        if _is_cross_trust_read_refused(
            writer_integrity, caller_integrity, caller_agent_type
        ):
            logger.warning(
                "files.read refused: trusted %s arc %s reading file "
                "written by %s arc %s at %s",
                caller_agent_type or "?",
                caller_arc_id,
                writer_integrity,
                prov["writer_arc_id"],
                realpath,
            )
            DispatchError = _get_dispatch_error_cls()
            raise DispatchError(
                "file path was written by a non-trusted arc; "
                "trusted arcs may not read it",
                status_code=403,
            )

    with open(path, 'r') as f:
        return {"content": f.read()}


def handle_write(params: dict) -> dict:
    """Write a file, recording provenance and enforcing the per-arc
    workspace allowlist for non-trusted writers.

    Trusted arcs (and chat-context callers with no arc) are unrestricted.
    Non-trusted (constrained / untrusted) writers may only write into
    ``{workspaces_dir}/arc-{arc_id}/`` or
    ``{base_dir}/data/state/{arc_id}/`` subtrees, resolved via realpath.
    """
    path = params["path"]
    content = params["content"]

    caller_arc_id = params.get("_caller_arc_id")
    caller_integrity, _ = _arc_trust_context(caller_arc_id)

    parent = os.path.dirname(path)
    # Resolve the realpath of the *target* path.  We resolve through
    # any pre-existing parent so symlink redirection in the parent dir
    # cannot be used to escape the workspace allowlist.
    if parent and os.path.exists(parent):
        target_realpath = os.path.realpath(
            os.path.join(os.path.realpath(parent), os.path.basename(path))
        )
    else:
        target_realpath = os.path.realpath(path)

    # Per-arc workspace allowlist for non-trusted writers.
    if (
        caller_arc_id is not None
        and caller_integrity is not None
        and caller_integrity != "trusted"
    ):
        prefixes = _workspace_allowed_prefixes(caller_arc_id)
        if not any(
            target_realpath == p.rstrip(os.sep)
            or target_realpath.startswith(p)
            for p in prefixes
        ):
            logger.warning(
                "files.write refused: %s arc %s writing outside workspace "
                "to %s (allowed prefixes: %s)",
                caller_integrity,
                caller_arc_id,
                target_realpath,
                prefixes,
            )
            DispatchError = _get_dispatch_error_cls()
            raise DispatchError(
                "non-trusted arcs may only write inside their own "
                "workspace; refusing",
                status_code=403,
            )

    # Platform-integrity tier check (I12) on the write path.  Applies to
    # ALL callers including trusted: T0 paths (credentials, platform.db)
    # are invisible, and T1 paths (carpenter source tree, config_seed,
    # security tests) must only be modified via the coding-change
    # workflow — which writes to a workspace copy, NOT through
    # ``files.write``.  Coding-change workflows use direct ``open(...)``
    # in ``carpenter/agent/coding_agent.py`` against workspace dirs
    # (T2) so this gate is safe and does not interfere with that path.
    target_tier = path_tier(target_realpath)
    if target_tier == PATH_TIER_T0:
        audit_path_decision(
            caller_arc_id,
            "t0_write_refused",
            target_realpath,
            {
                "tool": "files.write",
                "caller_integrity": caller_integrity,
            },
        )
        logger.warning(
            "files.write refused: T0 (invisible) path %s for caller arc %s",
            target_realpath,
            caller_arc_id,
        )
        DispatchError = _get_dispatch_error_cls()
        raise DispatchError(
            "path is platform-invisible (T0)",
            status_code=403,
        )
    if target_tier == PATH_TIER_T1:
        audit_path_decision(
            caller_arc_id,
            "t1_write_refused",
            target_realpath,
            {
                "tool": "files.write",
                "caller_integrity": caller_integrity,
            },
        )
        logger.warning(
            "files.write refused: T1 (platform) path %s for caller arc %s",
            target_realpath,
            caller_arc_id,
        )
        DispatchError = _get_dispatch_error_cls()
        raise DispatchError(
            "direct write to platform path (T1) is not allowed; "
            "use the coding-change workflow",
            status_code=403,
        )

    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, 'w') as f:
        f.write(content)

    # Record provenance for any caller-arc-bearing write.  Chat-context
    # writes (no caller arc) leave no row; that's intentional — there is
    # no untrusted-writer claim to record, and a future caller-arc-bearing
    # write to the same path will overwrite the row anyway.
    if caller_arc_id is not None:
        # If we couldn't determine the caller's integrity (e.g. arc row
        # vanished), record as the most conservative ('untrusted') so a
        # later trusted reader will be refused — fail-closed.
        recorded_integrity = caller_integrity or "untrusted"
        _record_provenance(
            target_realpath, caller_arc_id, recorded_integrity
        )

    return {"success": True}


def handle_list(params: dict) -> dict:
    directory = params["dir"]
    raw = os.listdir(directory)
    filtered: list[str] = []
    removed = 0
    for name in raw:
        try:
            if is_invisible(os.path.join(directory, name)):
                removed += 1
                continue
        except Exception:  # noqa: BLE001 — fail open on classifier errors
            # Classifier failure must not block legitimate listings.
            # The classifier itself logs a WARNING on failure; entry is
            # kept rather than dropped to avoid hiding legitimate files.
            pass
        filtered.append(name)
    if removed:
        # Audit row must NOT include the filtered filenames — leaking
        # the names partially defeats T0 invisibility.  Record only the
        # count.
        audit_path_decision(
            params.get("_caller_arc_id"),
            "listing_filtered",
            directory,
            {"dir": directory, "removed_count": removed},
        )
    return {"files": filtered}


def handle_file_count(params: dict) -> dict:
    """Count the number of files (not subdirectories) in a directory.

    Args:
        params: Dict with 'directory' key containing the directory path to count files in

    Returns:
        Dict with 'file_count' containing the number of files as an integer
    """
    directory = params["directory"]

    # Handle non-existent directory
    if not os.path.exists(directory):
        return {"file_count": 0, "error": f"Directory does not exist: {directory}"}

    # Handle path that is not a directory
    if not os.path.isdir(directory):
        return {"file_count": 0, "error": f"Path is not a directory: {directory}"}

    try:
        # Count only files, not subdirectories — and filter T0 entries
        # so file_count is consistent with handle_list (invisible files
        # don't contribute to the count).
        entries = os.listdir(directory)
        kept: list[str] = []
        removed = 0
        for entry in entries:
            try:
                if is_invisible(os.path.join(directory, entry)):
                    removed += 1
                    continue
            except Exception:  # noqa: BLE001
                pass
            kept.append(entry)
        if removed:
            audit_path_decision(
                params.get("_caller_arc_id"),
                "listing_filtered",
                directory,
                {"dir": directory, "removed_count": removed},
            )
        file_count = sum(1 for entry in kept
                        if os.path.isfile(os.path.join(directory, entry)))

        return {"file_count": file_count}
    except PermissionError:
        return {"file_count": 0, "error": f"Permission denied accessing directory: {directory}"}
    except OSError as e:
        return {"file_count": 0, "error": f"Error accessing directory {directory}: {str(e)}"}
