"""Resource manager: CRUD + lineage for the Resource abstraction.

See ``carpenter/core/resources/__init__.py`` for the big picture.  This
module is intentionally thin — it's a pure-insert / pure-read layer over
the ``resources`` and ``arc_resources`` tables.  Downstream PRs wire it
into ``fetch_web_content``, the template registry, the JUDGE verdict
path, the chat surface, and the weekly sweep.

All database access goes through ``db_connection()`` / ``db_transaction()``
to match the pattern used elsewhere in ``carpenter.core``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ...db import db_connection, db_transaction

logger = logging.getLogger(__name__)


_VALID_ROLES = {"input", "output"}
_VALID_VERDICTS_AT_INSERT = {"pending", "approved", "rejected"}
_VALID_VERDICTS_AT_UPDATE = {"approved", "rejected"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict | None:
    if row is None:
        return None
    # sqlite3.Row supports dict() conversion via keys()/indexing.
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def create_resource(
    *,
    content_type: str,
    file_path: str | None,
    produced_by_arc_id: int | None,
    source_descriptor: str | None = None,
    byte_size: int | None = None,
    content_hash: str | None = None,
    pinned: bool = False,
    kind: str | None = None,
) -> int:
    """Insert a raw-ingest Resource.

    ``produced_by_template`` and ``template_verdict`` are both NULL — this
    means the row is forever untrusted per ``resource_trust``.  The only
    path to trusted is ``derive_resource`` + approved ``mark_template_verdict``.

    ``kind`` (D24 SD12) is a free-form dataclass-name tag used by the
    JUDGE-dispatch wrapper to resolve a deserialiser for kind-typed
    handoffs.  Raw-ingest Resources rarely carry a ``kind``, but the
    column is allowed here for symmetry with ``derive_resource``.

    Returns the new resource id.
    """
    with db_transaction() as db:
        cursor = db.execute(
            "INSERT INTO resources "
            "(content_type, file_path, byte_size, content_hash, "
            " produced_by_arc_id, produced_by_template, template_verdict, "
            " source_descriptor, pinned, kind) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
            (
                content_type,
                file_path,
                byte_size,
                content_hash,
                produced_by_arc_id,
                source_descriptor,
                1 if pinned else 0,
                kind,
            ),
        )
        return cursor.lastrowid


def derive_resource(
    *,
    content_type: str,
    file_path: str | None,
    produced_by_arc_id: int,
    produced_by_template: str,
    template_verdict: str = "pending",
    source_descriptor: str | None = None,
    byte_size: int | None = None,
    content_hash: str | None = None,
    kind: str | None = None,
) -> int:
    """Insert a Resource derived from a template arc.

    Kept pure-insert so PR3 can wire up the auto-deprecation trigger
    (deprecate consumed inputs on arc completion) from the arc lifecycle
    instead of from here.

    Args:
        produced_by_template: non-empty template name.  Required — this
            is the provenance signal that makes trust derivable.
        template_verdict: 'pending' by default.  'approved' or 'rejected'
            also accepted at creation time; NULL is not allowed here
            because any derived resource should at least be in review.
        kind: D24 SD12 dispatch tag.  When set, the JUDGE-dispatch wrapper
            resolves it to a dataclass for the REVIEWER → JUDGE handoff.
            Optional for back-compat with raw-bytes JUDGE handlers.
    """
    if not produced_by_template:
        raise ValueError("produced_by_template is required for derive_resource")
    if template_verdict not in _VALID_VERDICTS_AT_INSERT:
        raise ValueError(
            f"Invalid template_verdict: {template_verdict!r}. "
            f"Valid values at creation: {sorted(_VALID_VERDICTS_AT_INSERT)}"
        )

    with db_transaction() as db:
        cursor = db.execute(
            "INSERT INTO resources "
            "(content_type, file_path, byte_size, content_hash, "
            " produced_by_arc_id, produced_by_template, template_verdict, "
            " source_descriptor, pinned, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                content_type,
                file_path,
                byte_size,
                content_hash,
                produced_by_arc_id,
                produced_by_template,
                template_verdict,
                source_descriptor,
                kind,
            ),
        )
        return cursor.lastrowid


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def mark_template_verdict(resource_id: int, verdict: str) -> None:
    """Flip a derived Resource's template_verdict.

    This is the ONE write path that can change a Resource's derived
    trust.  Callers (PR2 will expose a dispatch tool restricted to
    JUDGE arcs) are responsible for authorisation.

    Allowed verdicts: ``'approved'`` | ``'rejected'``.  Only valid on
    Resources where ``produced_by_template`` is NOT NULL (raw-ingest
    rows cannot be reclassified — their lack of provenance is the point).

    Semantics chosen explicitly:
    - Idempotent on repeat of the same verdict (approved -> approved is a no-op).
    - Transitions between approved and rejected are REJECTED with ValueError.
      The JUDGE verdict is a terminal decision; flipping it would silently
      change derived trust on every downstream consumer and is a red flag
      that the caller should create a new Resource instead.
    """
    if verdict not in _VALID_VERDICTS_AT_UPDATE:
        raise ValueError(
            f"Invalid verdict: {verdict!r}. "
            f"Allowed: {sorted(_VALID_VERDICTS_AT_UPDATE)}"
        )

    with db_transaction() as db:
        row = db.execute(
            "SELECT produced_by_template, template_verdict "
            "FROM resources WHERE id = ?",
            (resource_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Resource {resource_id} not found")
        if row["produced_by_template"] is None:
            raise ValueError(
                f"Resource {resource_id} has no produced_by_template; "
                "raw-ingest Resources cannot be reclassified"
            )
        current = row["template_verdict"]
        if current == verdict:
            return  # idempotent
        if current in _VALID_VERDICTS_AT_UPDATE and verdict != current:
            # approved <-> rejected is forbidden
            raise ValueError(
                f"Cannot transition Resource {resource_id} verdict "
                f"from {current!r} to {verdict!r}: verdict is terminal"
            )
        # current is 'pending' (or None, defensively) — allow transition
        db.execute(
            "UPDATE resources SET template_verdict = ? WHERE id = ?",
            (verdict, resource_id),
        )


# ---------------------------------------------------------------------------
# Arc <-> Resource links
# ---------------------------------------------------------------------------


def link_arc_resource(*, arc_id: int, resource_id: int, role: str) -> int:
    """Link an arc to a Resource with role 'input' or 'output'.

    For ``role='input'``: the arc's integrity_level is checked.  Untrusted
    arcs MUST NOT read Resources — this is the core enforcement point for
    the provenance-based trust model.

    For ``role='output'``: any arc may produce a Resource.  Trust is
    derived from the produced-by-template provenance on the Resource row,
    not from the producing arc's integrity_level.

    The UNIQUE constraint on (arc_id, resource_id, role) means repeated
    calls with the same triple return the existing arc_resources row id.

    Returns the arc_resources.id (new or existing).
    """
    if role not in _VALID_ROLES:
        raise ValueError(
            f"Invalid role: {role!r}. Valid roles: {sorted(_VALID_ROLES)}"
        )

    with db_transaction() as db:
        arc_row = db.execute(
            "SELECT id, integrity_level FROM arcs WHERE id = ?", (arc_id,)
        ).fetchone()
        if arc_row is None:
            raise ValueError(f"Arc {arc_id} not found")

        res_row = db.execute(
            "SELECT id FROM resources WHERE id = ?", (resource_id,)
        ).fetchone()
        if res_row is None:
            raise ValueError(f"Resource {resource_id} not found")

        if role == "input" and arc_row["integrity_level"] == "untrusted":
            raise ValueError(
                f"Untrusted arc {arc_id} cannot read Resource {resource_id} "
                "(input role forbidden for untrusted arcs)"
            )

        try:
            cursor = db.execute(
                "INSERT INTO arc_resources (arc_id, resource_id, role) "
                "VALUES (?, ?, ?)",
                (arc_id, resource_id, role),
            )
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            # UNIQUE(arc_id, resource_id, role) — return existing row id
            existing = db.execute(
                "SELECT id FROM arc_resources "
                "WHERE arc_id = ? AND resource_id = ? AND role = ?",
                (arc_id, resource_id, role),
            ).fetchone()
            if existing is None:
                raise
            return existing["id"]


# ---------------------------------------------------------------------------
# Deprecation
# ---------------------------------------------------------------------------


def deprecate_resource(resource_id: int) -> None:
    """Set ``deprecated_at = now`` on a Resource if not already set.

    Idempotent — repeat calls do not bump the timestamp.
    """
    with db_transaction() as db:
        db.execute(
            "UPDATE resources SET deprecated_at = ? "
            "WHERE id = ? AND deprecated_at IS NULL",
            (_now(), resource_id),
        )


def deprecate_inputs_of_arc(arc_id: int) -> int:
    """Mark all Resources linked as 'input' to ``arc_id`` as deprecated.

    Used by PR3 to auto-deprecate consumed inputs when a trusted arc
    completes successfully.

    Returns the number of Resources that were newly marked deprecated
    (rows whose deprecated_at was NULL before the update).
    """
    now = _now()
    with db_transaction() as db:
        cursor = db.execute(
            "UPDATE resources "
            "SET deprecated_at = ? "
            "WHERE deprecated_at IS NULL AND id IN ("
            "  SELECT resource_id FROM arc_resources "
            "  WHERE arc_id = ? AND role = 'input'"
            ")",
            (now, arc_id),
        )
        return cursor.rowcount or 0


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def get_resource(resource_id: int) -> dict | None:
    """Return the full resource row as a dict, or None if not found."""
    with db_connection() as db:
        row = db.execute(
            "SELECT * FROM resources WHERE id = ?", (resource_id,)
        ).fetchone()
        return _row_to_dict(row)


def list_resources_for_arc(arc_id: int, role: str | None = None) -> list[dict]:
    """Return Resources linked to an arc, optionally filtered by role.

    Joined via ``arc_resources``.  Ordered by the link creation time so
    callers see consumption / production order.
    """
    if role is not None and role not in _VALID_ROLES:
        raise ValueError(
            f"Invalid role: {role!r}. Valid roles: {sorted(_VALID_ROLES)}"
        )

    sql = (
        "SELECT r.* FROM resources r "
        "JOIN arc_resources ar ON ar.resource_id = r.id "
        "WHERE ar.arc_id = ?"
    )
    params: list = [arc_id]
    if role is not None:
        sql += " AND ar.role = ?"
        params.append(role)
    sql += " ORDER BY ar.created_at ASC, ar.id ASC"

    with db_connection() as db:
        rows = db.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]


def read_resource_content(
    resource_id: int,
    offset: int = 0,
    limit: int = 50_000,
    *,
    caller_arc_id: int | None,
) -> str:
    """Read the resource's file, returning ``text[offset:offset+limit]``.

    Returns the file contents sliced as ``text[offset:offset+limit]``.
    Raises ``FileNotFoundError`` if the Resource is deleted
    (``deleted_at`` set), has no ``file_path``, or the file is missing
    on disk.

    ``caller_arc_id`` is **mandatory and keyword-only** — every call
    site must declare its dispatch context explicitly so a future
    arc-context caller cannot silently bypass the trust gate by
    forgetting the parameter.  Two cases:

    - ``caller_arc_id=<int>``: the call is on behalf of an arc.  A
      defence-in-depth gate fires based on that arc's
      ``integrity_level``.  Trusted arcs reading a Resource whose
      derived trust is ``'untrusted'`` are refused with
      ``PermissionError``.  Untrusted / constrained arcs (REVIEWER,
      sandboxed EXECUTOR) pass through — they're explicitly allowed
      to read raw bytes.  Mirrors the invariant enforced at
      ``link_arc_resource`` for the ``input`` role.

    - ``caller_arc_id=None``: explicit declaration that the caller is
      the chat surface, a platform introspection path, or a test —
      i.e. there is no arc-dispatch context to gate against.  Those
      surfaces are responsible for their own trust gating; the chat
      tool in ``config_seed/chat_tools/resources.py`` keeps its
      ``is_trusted`` check so the two layers compose.

    Raises ``PermissionError`` when ``caller_arc_id`` refers to an arc
    that does not exist (treated as a failed gate rather than silently
    skipping the check).
    """
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit < 0:
        raise ValueError("limit must be >= 0")

    row = get_resource(resource_id)
    if row is None:
        raise FileNotFoundError(f"Resource {resource_id} not found")
    if row.get("deleted_at") is not None:
        raise FileNotFoundError(
            f"Resource {resource_id} is deleted (deleted_at set)"
        )

    # Defence-in-depth: a trusted arc cannot read an untrusted Resource.
    # This mirrors the invariant enforced at ``link_arc_resource`` for
    # the ``input`` role and guarantees that no new caller of this
    # function can silently violate the provenance trust model.
    if caller_arc_id is not None:
        from .trust import resource_trust

        with db_connection() as db:
            arc_row = db.execute(
                "SELECT integrity_level FROM arcs WHERE id = ?",
                (int(caller_arc_id),),
            ).fetchone()
        if arc_row is None:
            raise PermissionError(
                f"read_resource_content: caller arc {caller_arc_id} not found"
            )
        if (
            arc_row["integrity_level"] == "trusted"
            and resource_trust(row) == "untrusted"
        ):
            raise PermissionError(
                f"Trusted arc {caller_arc_id} cannot read untrusted "
                f"Resource {resource_id} "
                f"(template_verdict="
                f"{row.get('template_verdict')!r}, "
                f"produced_by_template="
                f"{row.get('produced_by_template')!r})"
            )

    file_path = row.get("file_path")
    if not file_path:
        raise FileNotFoundError(
            f"Resource {resource_id} has no file_path"
        )
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Resource {resource_id} file missing on disk: {file_path}"
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[offset : offset + limit]


# ---------------------------------------------------------------------------
# Storage path + finalize
# ---------------------------------------------------------------------------


def resource_storage_dir() -> Path:
    """Return the on-disk root directory for Resource blobs.

    Layout: ``{base_dir}/data/resources/`` in production, ``{base_dir}/resources/``
    in tests (where ``base_dir`` is already the per-test tmp_path and
    ``database_path`` lives directly under it rather than under a ``data/``
    subdir).

    Uses ``database_path`` as the anchor: Resources live alongside the DB so
    backup/wipe semantics stay coherent.  Falls back to ``~/carpenter/data``
    if config is absent (shouldn't happen in practice).
    """
    from ... import config as _config

    db_path = _config.CONFIG.get("database_path")
    if db_path:
        return Path(db_path).parent / "resources"
    return Path(os.path.expanduser("~/carpenter/data/resources"))


def resource_storage_path(resource_id: int, filename: str = "blob") -> Path:
    """Return the canonical on-disk path for a Resource's blob.

    ``{storage_root}/{resource_id}/{filename}``.  The per-id subdir gives
    each Resource a stable directory the sweep job can ``rmtree`` in one
    shot when a Resource is deleted.
    """
    return resource_storage_dir() / str(resource_id) / filename


def hash_file(path: str | Path) -> tuple[int, str]:
    """Return ``(byte_size, sha256_hex)`` for a file on disk.

    Streams the file so a large blob doesn't blow out memory.  Callers
    (``resource.finalize`` dispatch) use this to populate the Resource
    row's ``byte_size`` / ``content_hash`` columns after the producer arc
    has finished writing the blob.
    """
    path = Path(path)
    size = 0
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            size += len(chunk)
            h.update(chunk)
    return size, h.hexdigest()


def set_resource_file_path(resource_id: int, file_path: str) -> None:
    """Update an existing Resource row's ``file_path`` column.

    Used by the ``resource.create`` dispatch path: the row is inserted
    first (with ``file_path=None``) so the auto-generated id can be used
    to compute the canonical on-disk path via ``resource_storage_path``.
    The path is then written back here.

    Raises ``ValueError`` if the Resource does not exist.
    """
    with db_transaction() as db:
        row = db.execute(
            "SELECT id FROM resources WHERE id = ?", (resource_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Resource {resource_id} not found")
        db.execute(
            "UPDATE resources SET file_path = ? WHERE id = ?",
            (file_path, resource_id),
        )


def update_resource_content_stats(
    resource_id: int,
    byte_size: int,
    content_hash: str,
) -> None:
    """Update ``byte_size`` and ``content_hash`` on an existing Resource row.

    Used by the ``resource.finalize`` dispatch after a producer arc writes
    the blob to disk.  Does not touch ``file_path`` / ``content_type`` /
    provenance columns — those are fixed at creation time.

    Raises ``ValueError`` if the Resource does not exist.
    """
    with db_transaction() as db:
        row = db.execute(
            "SELECT id FROM resources WHERE id = ?", (resource_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Resource {resource_id} not found")
        db.execute(
            "UPDATE resources SET byte_size = ?, content_hash = ? WHERE id = ?",
            (byte_size, content_hash, resource_id),
        )


# ---------------------------------------------------------------------------
# Pin / retention
# ---------------------------------------------------------------------------


def pin(resource_id: int) -> None:
    """Mark a Resource as pinned (excluded from sweep)."""
    with db_transaction() as db:
        db.execute(
            "UPDATE resources SET pinned = 1 WHERE id = ?", (resource_id,)
        )


def unpin(resource_id: int) -> None:
    """Clear the pin flag on a Resource."""
    with db_transaction() as db:
        db.execute(
            "UPDATE resources SET pinned = 0 WHERE id = ?", (resource_id,)
        )


def set_retain_until(resource_id: int, ts: str | None) -> None:
    """Set (or clear, with ``None``) the ``retain_until`` timestamp."""
    with db_transaction() as db:
        db.execute(
            "UPDATE resources SET retain_until = ? WHERE id = ?",
            (ts, resource_id),
        )


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------


def get_lineage(resource_id: int) -> list[dict]:
    """Walk the provenance tree rooted at ``resource_id``.

    Order: [self, inputs-of-producing-arc, inputs-of-grandparent-arcs, ...].
    Deduplicates by resource id.  Cycle-safe (defensive — resources
    cannot legitimately cycle, but the traversal doesn't revisit ids).

    Returns a flat list of resource dicts.  Returns ``[]`` if the root
    resource does not exist.
    """
    root = get_resource(resource_id)
    if root is None:
        return []

    ordered: list[dict] = [root]
    seen: set[int] = {root["id"]}
    # BFS by generation over producing arcs
    frontier: list[dict] = [root]
    while frontier:
        next_frontier: list[dict] = []
        for res in frontier:
            arc_id = res.get("produced_by_arc_id")
            if arc_id is None:
                continue
            inputs = list_resources_for_arc(arc_id, role="input")
            for inp in inputs:
                if inp["id"] in seen:
                    continue
                seen.add(inp["id"])
                ordered.append(inp)
                next_frontier.append(inp)
        frontier = next_frontier
    return ordered
