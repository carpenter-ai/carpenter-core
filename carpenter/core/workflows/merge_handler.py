"""Handler for template-driven merge resolution.

Handles the three steps of the merge-resolution template:
1. attempt-merge: Try a mechanistic git merge
2. resolve-conflicts: AI-assisted conflict resolution
3. review-resolution: Human review of the resolution

Triggered when a merge conflict is detected and auto_resolve_merge_conflicts
is enabled.

Uses dulwich (pure Python git library) instead of shelling out to git CLI.
"""

import io
import logging
import os

import dulwich.porcelain as porcelain
from dulwich.repo import Repo

from ... import config
from ..arcs import manager as arc_manager
from .. import workspace_manager
from ...db import get_db, db_connection
from ._arc_state import get_arc_state as _get_arc_state, set_arc_state as _set_arc_state

logger = logging.getLogger(__name__)

# Step name constants
STEP_ATTEMPT_MERGE = "attempt-merge"
STEP_RESOLVE_CONFLICTS = "resolve-conflicts"
STEP_REVIEW_RESOLUTION = "review-resolution"

# Maximum characters to include when truncating conflict diffs for context
CONFLICT_DIFF_MAX_CHARS = 8000

# Author/committer identity used for automated merge operations.
# Can be configured via the "git_identity" config key.
def _get_git_identity() -> bytes:
    """Get the git identity to use for automated merge operations."""
    identity_str = config.CONFIG.get("git_identity", "Carpenter <carpenter@localhost>")
    return identity_str.encode("utf-8")


_GIT_IDENTITY = _get_git_identity()


def create_merge_resolution_arc(
    source_dir: str,
    target_ref: str,
    merge_type: str,
    parent_arc_id: int | None = None,
    conflict_context: dict | None = None,
) -> int | None:
    """Create a merge-resolution arc from the template.

    Only creates if auto_resolve_merge_conflicts is enabled.

    Args:
        source_dir: The git repo with the conflict.
        target_ref: The ref being merged (branch name or origin/main).
        merge_type: "branch" or "remote".
        parent_arc_id: Optional parent arc to nest under.
        conflict_context: Optional dict with conflict details (files, diff).

    Returns the arc ID, or None if auto-resolve is disabled.
    """
    if not config.CONFIG.get("auto_resolve_merge_conflicts", False):
        return None

    template_name = config.CONFIG.get("merge_resolution_template", "merge-resolution")

    from ..engine import template_manager

    template = template_manager.get_template_by_name(template_name)
    if not template:
        logger.error("Merge resolution template '%s' not found", template_name)
        return None

    # Create the parent arc
    arc_id = arc_manager.create_arc(
        name="merge-resolution",
        goal=f"Resolve merge conflict in {source_dir}",
        parent_id=parent_arc_id,
    )

    # Instantiate template steps as children
    child_ids = template_manager.instantiate_template(template["id"], arc_id)

    # Store merge context in parent arc state
    _set_arc_state(arc_id, "source_dir", source_dir)
    _set_arc_state(arc_id, "target_ref", target_ref)
    _set_arc_state(arc_id, "merge_type", merge_type)
    if conflict_context:
        _set_arc_state(arc_id, "conflict_context", conflict_context)

    # Enqueue the first step
    from ..engine import work_queue
    work_queue.enqueue(
        f"merge-resolution.{STEP_ATTEMPT_MERGE}",
        {"arc_id": arc_id, "source_dir": source_dir, "target_ref": target_ref, "merge_type": merge_type},
    )

    arc_manager.update_status(arc_id, "active")
    logger.info("Created merge-resolution arc %d with %d child steps", arc_id, len(child_ids))
    return arc_id


def _run_dulwich_fetch(repo_path: str, remote: str) -> None:
    """Fetch from a remote, suppressing stderr progress output."""
    null = io.BytesIO()
    porcelain.fetch(repo_path, remote, errstream=null)


async def handle_attempt_merge(work_id: int, payload: dict):
    """Handle the attempt-merge step: try a mechanistic git merge.

    If merge succeeds, complete the entire arc (skip remaining steps).
    If merge fails, capture conflict state and activate the resolve step.
    """
    arc_id = payload.get("arc_id")
    source_dir = payload.get("source_dir")
    target_ref = payload.get("target_ref")
    merge_type = payload.get("merge_type", "branch")

    if not arc_id or not source_dir:
        logger.error("attempt-merge: missing arc_id or source_dir")
        return

    try:
        if merge_type == "remote":
            # Fetch and fast-forward merge
            _run_dulwich_fetch(source_dir, "origin")
            merge_sha, conflicts = porcelain.merge(
                source_dir,
                target_ref.encode(),
                message=f"Merge {target_ref}".encode(),
                author=_GIT_IDENTITY,
                committer=_GIT_IDENTITY,
            )
        else:
            # Branch merge (no-ff)
            merge_sha, conflicts = porcelain.merge(
                source_dir,
                target_ref.encode(),
                no_ff=True,
                message=f"Merge {target_ref}".encode(),
                author=_GIT_IDENTITY,
                committer=_GIT_IDENTITY,
            )

        if not conflicts:
            # Success -- complete entire arc hierarchy
            arc_manager.add_history(arc_id, "merge_succeeded", {
                "merge_type": merge_type, "target_ref": target_ref,
            })
            # Mark all child arcs as completed
            _complete_arc_tree(arc_id)
            logger.info("Merge succeeded for arc %d", arc_id)
            return

        # Merge conflict -- capture state
        conflicting_files = [c.decode() if isinstance(c, bytes) else c for c in conflicts]

        # Get conflict diff
        conflict_diff = ""
        try:
            buf = io.BytesIO()
            porcelain.diff(source_dir, outstream=buf)
            conflict_diff = buf.getvalue().decode("utf-8", errors="replace")
        except Exception:
            pass

        # Abort: reset to pre-merge state
        r = Repo(source_dir)
        try:
            head_symrefs = r.refs.get_symrefs()
            head_ref = head_symrefs[b"HEAD"]
            branch_name = head_ref.split(b"/")[-1].decode()
            porcelain.reset(source_dir, "hard", f"refs/heads/{branch_name}")
        except Exception:
            porcelain.reset(source_dir, "hard", "HEAD")

        # Store conflict state for the resolve step
        _set_arc_state(arc_id, "conflict_diff", conflict_diff)
        _set_arc_state(arc_id, "conflicting_files", conflicting_files)
        _set_arc_state(arc_id, "merge_stderr", f"Conflict in {len(conflicting_files)} file(s)")

        arc_manager.add_history(arc_id, "merge_conflict", {
            "conflicting_files": conflicting_files,
            "merge_type": merge_type,
        })

        # Enqueue the resolve step
        from ..engine import work_queue
        work_queue.enqueue(
            f"merge-resolution.{STEP_RESOLVE_CONFLICTS}",
            {"arc_id": arc_id, "source_dir": source_dir, "target_ref": target_ref},
        )
        logger.info("Merge conflict for arc %d, %d files conflicting", arc_id, len(conflicting_files))

    except Exception as e:
        logger.exception("Error in attempt-merge for arc %d", arc_id)
        arc_manager.add_history(arc_id, "error", {"message": str(e)})
        arc_manager.update_status(arc_id, "failed")


async def handle_resolve_conflicts(work_id: int, payload: dict):
    """Handle the resolve-conflicts step: AI-assisted conflict resolution.

    Creates an isolated workspace with both sides of the conflict,
    invokes a coding agent to resolve, and stores the resolution diff.
    """
    arc_id = payload.get("arc_id")
    source_dir = payload.get("source_dir")
    target_ref = payload.get("target_ref")

    if not arc_id or not source_dir:
        logger.error("resolve-conflicts: missing arc_id or source_dir")
        return

    conflicting_files = _get_arc_state(arc_id, "conflicting_files", [])
    conflict_diff = _get_arc_state(arc_id, "conflict_diff", "")

    if not conflicting_files:
        logger.warning("resolve-conflicts: no conflicting files for arc %d", arc_id)
        arc_manager.update_status(arc_id, "failed")
        return

    try:
        # Create a workspace from current source state for the agent to work in
        ws_path, base_sha = workspace_manager.create_workspace(source_dir, f"merge-resolve-{arc_id}")
        _set_arc_state(arc_id, "resolve_workspace", ws_path)

        # Build a prompt with conflict context
        from ...agent import templates
        prompt = templates.render(
            "merge_resolve_conflicts",
            conflicting_files=", ".join(conflicting_files),
            target_ref=target_ref,
            conflict_diff=conflict_diff[:CONFLICT_DIFF_MAX_CHARS],
        )

        # Run the coding agent
        from ...agent import coding_dispatch
        from ... import thread_pools
        result = await thread_pools.run_in_work_pool(
            coding_dispatch.invoke_coding_agent, ws_path, prompt,
        )
        _set_arc_state(arc_id, "resolve_result", result)

        # Get the resolution diff
        resolution_diff = workspace_manager.get_diff(ws_path)
        if not resolution_diff.strip():
            logger.warning("AI resolution produced no changes for arc %d", arc_id)
            arc_manager.update_status(arc_id, "failed")
            return

        _set_arc_state(arc_id, "resolution_diff", resolution_diff)
        changed_files = workspace_manager.get_changed_files(ws_path)
        _set_arc_state(arc_id, "resolution_files", changed_files)

        arc_manager.add_history(arc_id, "resolution_proposed", {
            "changed_files": changed_files,
            "diff_lines": len(resolution_diff.splitlines()),
        })

        # Create a review for human approval
        from ...api.review import create_diff_review
        review_data = create_diff_review(
            diff_content=resolution_diff,
            arc_id=arc_id,
            title=f"Merge conflict resolution ({len(conflicting_files)} file(s))",
            changed_files=changed_files,
        )
        review_url = review_data.get("url", "")
        _set_arc_state(arc_id, "resolution_review_url", review_url)

        arc_manager.update_status(arc_id, "waiting")
        logger.info("Merge resolution proposed for arc %d, awaiting review at %s", arc_id, review_url)

    except Exception:  # broad catch: workspace/coding agent may raise anything
        logger.exception("resolve-conflicts failed for arc %d", arc_id)
        # Clean up workspace
        ws = _get_arc_state(arc_id, "resolve_workspace")
        if ws:
            workspace_manager.cleanup_workspace(ws)
        arc_manager.update_status(arc_id, "failed")


async def handle_review_resolution(work_id: int, payload: dict):
    """Handle the review-resolution step: human approval of conflict resolution.

    Payload keys:
        arc_id: The merge-resolution arc ID
        decision: "approve" or "reject"
    """
    arc_id = payload.get("arc_id")
    decision = payload.get("decision", "")

    if not arc_id:
        logger.error("review-resolution: missing arc_id")
        return

    source_dir = _get_arc_state(arc_id, "source_dir")
    ws_path = _get_arc_state(arc_id, "resolve_workspace")

    if decision == "approve":
        if ws_path and source_dir:
            try:
                applied = workspace_manager.apply_to_source(ws_path, source_dir)
                arc_manager.add_history(arc_id, "resolution_applied", {"files": applied})
            except RuntimeError as e:
                logger.error("Failed to apply resolution for arc %d: %s", arc_id, e)
                arc_manager.update_status(arc_id, "failed")
                return

        # Clean up and complete
        if ws_path:
            workspace_manager.cleanup_workspace(ws_path)
        _complete_arc_tree(arc_id)
        logger.info("Merge resolution approved and applied for arc %d", arc_id)

    elif decision == "reject":
        if ws_path:
            workspace_manager.cleanup_workspace(ws_path)
        arc_manager.add_history(arc_id, "resolution_rejected", {})
        arc_manager.update_status(arc_id, "failed")
        logger.info("Merge resolution rejected for arc %d", arc_id)

    else:
        logger.warning("Unknown decision '%s' for review-resolution arc %d", decision, arc_id)


def _complete_arc_tree(arc_id: int):
    """Mark the arc and all its children as completed."""
    with db_connection() as db:
        # Get child arcs
        rows = db.execute(
            "SELECT id FROM arcs WHERE parent_id = ? AND status NOT IN ('completed', 'cancelled', 'failed')",
            (arc_id,),
        ).fetchall()
        for row in rows:
            arc_manager.update_status(row["id"], "completed")
        arc_manager.update_status(arc_id, "completed")


def register_handlers(register_fn):
    """Register merge-resolution handlers with the main loop."""
    register_fn(f"merge-resolution.{STEP_ATTEMPT_MERGE}", handle_attempt_merge)
    register_fn(f"merge-resolution.{STEP_RESOLVE_CONFLICTS}", handle_resolve_conflicts)
    register_fn(f"merge-resolution.{STEP_REVIEW_RESOLUTION}", handle_review_resolution)
