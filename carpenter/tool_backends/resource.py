"""Resource tool backends.

Handles:
  - ``resource.submit_verdict`` — JUDGE-only flip of Resource template_verdict.
  - ``resource.finalize`` — producer arcs commit their written blob
    (fills byte_size/content_hash and optionally deprecates input
    Resources consumed by the same arc).
  - ``resource.create`` — any arc registers a new raw Resource it will
    subsequently write a blob to and then ``resource.finalize``.
"""

import logging
import os

from ..core.resources import (
    create_resource,
    deprecate_inputs_of_arc,
    hash_file,
    link_arc_resource,
    mark_template_verdict,
    resource_storage_path,
    set_resource_file_path,
    update_resource_content_stats,
)
from ..db import db_connection

logger = logging.getLogger(__name__)


def _caller_agent_type(params: dict) -> str | None:
    """Look up the calling arc's agent_type from the DB.

    Dispatch bridge injects ``_caller_arc_id``; we map that back to the
    stored ``agent_type`` column.  Returns ``None`` if no caller arc
    (chat path) or the arc is missing.
    """
    arc_id = params.get("_caller_arc_id")
    if arc_id is None:
        return None
    with db_connection() as db:
        row = db.execute(
            "SELECT agent_type FROM arcs WHERE id = ?", (arc_id,),
        ).fetchone()
    if row is None:
        return None
    return row["agent_type"]


def handle_submit_verdict(params: dict) -> dict:
    """Submit a JUDGE verdict on a Resource's template_verdict.

    Params:
        resource_id: int  — the Resource to flip.
        verdict: str      — 'approved' or 'rejected'.

    We require JUDGE explicitly even though the agent-type whitelist
    already filters the call — two layers, so a config override that
    accidentally widens ``allowed_tools`` still can't bypass the
    JUDGE-only requirement.
    """
    caller_agent_type = _caller_agent_type(params)
    if caller_agent_type != "JUDGE":
        raise PermissionError(
            f"resource.submit_verdict requires JUDGE agent_type; "
            f"caller is {caller_agent_type!r}"
        )

    resource_id = params.get("resource_id")
    verdict = params.get("verdict")
    if resource_id is None:
        raise ValueError("resource.submit_verdict requires resource_id")
    if verdict not in ("approved", "rejected"):
        raise ValueError(
            f"resource.submit_verdict requires verdict in "
            f"('approved', 'rejected'); got {verdict!r}"
        )

    mark_template_verdict(int(resource_id), verdict)
    return {"ok": True, "resource_id": int(resource_id), "verdict": verdict}


def handle_finalize(params: dict) -> dict:
    """Finalize a Resource after its producing arc has written the blob.

    Params:
        resource_id: int — the Resource whose file_path is now populated.
        deprecate_inputs: bool, optional (default False) — when True, mark
            all Resources linked as ``input`` to the caller arc as
            deprecated.  REVIEWER arcs pass ``True`` after committing
            their derived output so the consumed raw Resource is retired.

    The caller arc (identified via ``_caller_arc_id`` injected by the
    dispatch bridge) must be the Resource's ``produced_by_arc_id`` —
    this prevents one arc finalising another arc's Resource.

    Hashes the file at ``file_path`` and updates ``byte_size`` +
    ``content_hash`` on the Resource row.

    Returns ``{"ok": True, "resource_id": ..., "byte_size": ..., "content_hash": ...}``.
    """
    resource_id = params.get("resource_id")
    if resource_id is None:
        raise ValueError("resource.finalize requires resource_id")
    resource_id = int(resource_id)

    caller_arc_id = params.get("_caller_arc_id")
    # Look up the Resource and confirm the caller is the producer.
    with db_connection() as db:
        row = db.execute(
            "SELECT produced_by_arc_id, file_path FROM resources WHERE id = ?",
            (resource_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"Resource {resource_id} not found")

    produced_by = row["produced_by_arc_id"]
    # The chat surface can finalise on behalf of a freshly-created Resource
    # when there's no caller arc (produced_by_arc_id is NULL).  When both
    # are present, they must match.
    if caller_arc_id is not None and produced_by is not None:
        if int(caller_arc_id) != int(produced_by):
            raise PermissionError(
                f"resource.finalize: arc {caller_arc_id} is not the "
                f"producer of Resource {resource_id} "
                f"(producer is arc {produced_by})"
            )

    file_path = row["file_path"]
    if not file_path:
        raise ValueError(
            f"Resource {resource_id} has no file_path; cannot finalize"
        )

    byte_size, content_hash = hash_file(file_path)
    update_resource_content_stats(resource_id, byte_size, content_hash)

    deprecated_count = 0
    if params.get("deprecate_inputs") and caller_arc_id is not None:
        deprecated_count = deprecate_inputs_of_arc(int(caller_arc_id))

    return {
        "ok": True,
        "resource_id": resource_id,
        "byte_size": byte_size,
        "content_hash": content_hash,
        "deprecated_inputs": deprecated_count,
    }


def handle_create(params: dict) -> dict:
    """Register a new raw Resource row owned by the caller arc.

    Params:
        content_type: str — required.  A free-form label (e.g. ``'html'``,
            ``'application/json'``, ``'text-summary'``).  NOT validated and
            NOT a trust claim: raw Resources created via this path are
            ``produced_by_template = NULL`` and therefore forever
            ``'untrusted'`` per ``resource_trust``.  Templates and review
            arcs decide what to do with bytes based on this label.
        source_descriptor: str, optional — free-form description of where
            the bytes came from (URL, webhook id, etc.).

    Reads ``_caller_arc_id`` from params (injected by the dispatch bridge).
    The resulting Resource is linked to the caller arc with role='output'.

    Returns ``{"resource_id": int, "file_path": str}``.  The caller is
    expected to write the blob at ``file_path`` and then call
    ``resource.finalize`` to populate ``byte_size`` / ``content_hash``.
    The parent directory is created eagerly; the blob file itself is not.
    """
    content_type = params.get("content_type")
    if not isinstance(content_type, str) or not content_type:
        raise ValueError("resource.create requires non-empty content_type")

    caller_arc_id = params.get("_caller_arc_id")
    if caller_arc_id is None:
        raise ValueError(
            "resource.create requires arc context (_caller_arc_id missing); "
            "this tool is only callable from a running arc"
        )
    caller_arc_id = int(caller_arc_id)

    source_descriptor = params.get("source_descriptor")
    if source_descriptor is not None and not isinstance(source_descriptor, str):
        raise ValueError(
            "resource.create source_descriptor must be a string when provided"
        )

    # Insert the row first so the id can be used to compute the canonical
    # path — same two-step pattern as fetch_web_content in invocation.py.
    resource_id = create_resource(
        content_type=content_type,
        file_path=None,
        produced_by_arc_id=caller_arc_id,
        source_descriptor=source_descriptor,
    )
    path = resource_storage_path(resource_id)
    os.makedirs(path.parent, exist_ok=True)
    # Do NOT create the file here — the caller writes the blob, then calls
    # resource.finalize to populate byte_size/content_hash.

    set_resource_file_path(resource_id, str(path))

    # Link the Resource to the caller arc as an output.  Untrusted arcs
    # are allowed to PRODUCE Resources (link_arc_resource only gates the
    # input role), so no integrity check is needed here.
    link_arc_resource(
        arc_id=caller_arc_id, resource_id=resource_id, role="output"
    )

    return {"resource_id": resource_id, "file_path": str(path)}
