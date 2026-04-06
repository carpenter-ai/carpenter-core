"""Handler for coding-change arc workflow.

Handles the three steps of a coding-change arc:
1. invoke-agent: Set up workspace and run the coding agent
2. generate-review: Generate diff and create a review link
3. await-approval: Process human approve/reject/revise decision

Registered in the main loop for coding-change.* event types.
"""

import asyncio
import logging
import os

from ... import config
from ..arcs import CODING_CHANGE_PREFIX, manager as arc_manager
from .. import workspace_manager
from ...agent import coding_dispatch, conversation
from ...db import get_db, db_connection
from ._arc_state import get_arc_state as _get_arc_state, set_arc_state as _set_arc_state
from ._notifications import notify_arc_conversation

logger = logging.getLogger(__name__)

# Step name constants
STEP_INVOKE_AGENT = "invoke-agent"
STEP_GENERATE_REVIEW = "generate-review"
STEP_APPROVAL = "approval"

# Maximum number of revision/rework attempts before auto-escalating to human review
MAX_REWORK_ATTEMPTS = 3

# Maximum characters to include in stdout preview in arc history entries
STDOUT_PREVIEW_MAX_CHARS = 500

# Maximum characters to include when truncating conflict diffs for context
CONFLICT_DIFF_MAX_CHARS = 8000


def _sync_config_seed_chat_tools(applied_files: list[str], source_dir: str):
    """Sync config_seed/chat_tools/ files to the runtime chat_tools directory.

    After a coding-change patches chat tool modules in config_seed/, the
    runtime config dir needs to be updated so the hot-reload heartbeat
    detects the change and loads the new/modified tools.
    """
    import shutil
    chat_tool_files = [f for f in applied_files if f.startswith("config_seed/chat_tools/")]
    if not chat_tool_files:
        return
    base_dir = config.CONFIG.get("base_dir", os.path.expanduser("~/carpenter"))
    runtime_dir = config.CONFIG.get("chat_tools_dir", "") or os.path.join(base_dir, "config", "chat_tools")
    if not os.path.isdir(runtime_dir):
        return
    for rel_path in chat_tool_files:
        src = os.path.join(source_dir, rel_path)
        # Strip config_seed/chat_tools/ prefix to get the filename
        filename = os.path.basename(rel_path)
        dest = os.path.join(runtime_dir, filename)
        try:
            shutil.copy2(src, dest)
            logger.info("Synced %s → %s", rel_path, dest)
        except OSError as e:
            logger.warning("Failed to sync %s to runtime: %s", rel_path, e)


def _notify_chat(arc_id: int, message: str):
    """Inject a system message into the chat conversation linked to this arc."""
    conv_id = _get_arc_state(arc_id, "conversation_id")
    if conv_id is not None:
        notify_arc_conversation(arc_id, message, conversation_id=conv_id)

async def _notify_and_respond(arc_id: int, message: str):
    """Inject a system message into the arc's conversation.

    Adds the notification so the chat agent sees it on the next user message.
    Does NOT auto-invoke the chat agent — running a chat invocation inside a
    work handler causes SQLite lock contention that freezes the event loop
    (the background thread holds a write lock while the main loop's
    work_queue.claim() blocks on the same lock).
    """
    _notify_chat(arc_id, message)


def _cancel_stale_changesets(source_dir: str, exclude_arc_id: int | None = None):
    """Cancel pending/waiting coding-change arcs for this source dir.

    Cleans up their workspaces and notifies the chat. New arc takes precedence.
    Active arcs are NOT cancelled — they may be mid-apply (e.g. approval handler
    running concurrently) and cancelling them would race with the apply.
    """
    with db_connection() as db:
        query = (
            "SELECT id FROM arcs "
            f"WHERE name LIKE '{CODING_CHANGE_PREFIX}%' "
            "AND status IN ('pending', 'waiting') "
            "AND goal LIKE ?"
        )
        params = [f"%{source_dir}%"]
        if exclude_arc_id is not None:
            query += " AND id != ?"
            params.append(exclude_arc_id)
        rows = db.execute(query, params).fetchall()

    for row in rows:
        old_id = row["id"]
        # Clean up workspace if it exists
        old_ws = _get_arc_state(old_id, "workspace_path")
        if old_ws:
            workspace_manager.cleanup_workspace(old_ws)
        arc_manager.update_status(old_id, "cancelled")
        logger.info("Cancelled stale coding-change arc %d for %s", old_id, source_dir)


def _check_duplicate_tool_names(
    workspace_path: str, changed_files: list[str],
    source_dir: str = "",
) -> list[tuple[str, str, str]]:
    """Check if any new tool YAML in the workspace duplicates existing tool names.

    Returns list of (tool_name, new_file, existing_file) tuples for duplicates.
    Only considers truly new tool names — tools added to a file that were not
    present in the source version of that same file.
    """
    from pathlib import Path as _Path

    tool_yaml_files = [
        f for f in changed_files
        if ("tool-defaults/" in f or "config_seed/tools/" in f) and f.endswith(".yaml")
    ]
    if not tool_yaml_files:
        return []

    try:
        import yaml as _yaml
    except ImportError:
        return []

    def _load_tool_names(path: _Path) -> set[str]:
        """Extract tool names from a YAML file."""
        if not path.exists():
            return set()
        try:
            data = _yaml.safe_load(path.read_text())
            if isinstance(data, dict) and isinstance(data.get("tools"), list):
                return {
                    tool["name"] for tool in data["tools"]
                    if isinstance(tool, dict) and "name" in tool
                }
        except (OSError, ValueError, KeyError) as _exc:
            pass
        return set()

    # Find truly NEW tool names (in workspace but not in source version of same file)
    new_tools: dict[str, str] = {}  # name -> rel_path
    for rel_path in tool_yaml_files:
        ws_names = _load_tool_names(_Path(workspace_path) / rel_path)
        src_names = _load_tool_names(_Path(source_dir) / rel_path) if source_dir else set()
        for name in ws_names - src_names:
            new_tools[name] = rel_path

    if not new_tools:
        return []

    # Load ALL existing tool names from source config_seed/tools/
    existing: dict[str, str] = {}
    if source_dir:
        source_td = _Path(source_dir) / "config_seed" / "tools"
        if source_td.is_dir():
            try:
                for yf in sorted(source_td.glob("*.yaml")):
                    for name in _load_tool_names(yf):
                        existing[name] = yf.name
            except OSError as _exc:
                pass

    duplicates = []
    for name, new_file in new_tools.items():
        if name in existing:
            duplicates.append((name, new_file, existing[name]))

    return duplicates


async def handle_invoke_agent(work_id: int, payload: dict):
    """Handle the invoke-agent step of a coding-change arc.

    Payload keys:
        arc_id: The parent coding-change arc ID
        source_dir: Directory to copy into workspace
        prompt: The coding instruction
        coding_agent: (optional) Name of the coding agent profile
    """
    arc_id = payload.get("arc_id")
    source_dir = payload.get("source_dir")
    prompt = payload.get("prompt", "")
    agent_name = payload.get("coding_agent")

    if not arc_id or not source_dir:
        logger.error("invoke-agent: missing arc_id or source_dir in payload")
        return

    # Activate arc: pending -> active, or verify already active (revision/rework).
    # Bail out if the arc is in a terminal status — a previous attempt may have
    # failed/cancelled it, and continuing would produce orphan work items.
    arc = arc_manager.get_arc(arc_id)
    if not arc:
        logger.error("invoke-agent: arc %d not found", arc_id)
        return
    current_status = arc["status"]
    if current_status == "pending":
        arc_manager.update_status(arc_id, "active")
    elif current_status == "active":
        pass  # Already active (revision/rework re-invocation) — continue
    else:
        # Arc is in a terminal or unexpected status — do not proceed.
        logger.warning(
            "invoke-agent: arc %d has status '%s', expected 'pending' or 'active'. "
            "Skipping to avoid orphan work.",
            arc_id, current_status,
        )
        return

    # Cancel any existing changesets for this source dir (new one wins)
    _cancel_stale_changesets(source_dir, exclude_arc_id=arc_id)

    # Store original prompt on first run (not revisions) for revision feedback
    if not _get_arc_state(arc_id, "original_prompt"):
        _set_arc_state(arc_id, "original_prompt", prompt)

    try:
        rework_count = _get_arc_state(arc_id, "rework_count", 0)
        old_ws = _get_arc_state(arc_id, "workspace_path")

        # For revision re-runs, keep the existing workspace so the agent
        # can make incremental changes instead of starting from scratch.
        # A fresh workspace forces the agent to redo ALL work plus the
        # revision, which often exceeds the iteration budget.
        if rework_count > 0 and old_ws and os.path.isdir(old_ws):
            workspace_path = old_ws
            logger.info(
                "Reusing workspace %s for arc %d revision %d",
                workspace_path, arc_id, rework_count,
            )
        else:
            if old_ws:
                workspace_manager.cleanup_workspace(old_ws)
            label = f"{CODING_CHANGE_PREFIX}-{arc_id}"
            workspace_path, base_sha = workspace_manager.create_workspace(source_dir, label)
            _set_arc_state(arc_id, "workspace_path", workspace_path)
            _set_arc_state(arc_id, "source_dir", source_dir)
            if base_sha:
                _set_arc_state(arc_id, "workspace_base_sha", base_sha)

        arc_manager.add_history(
            arc_id, "workspace_created",
            {"workspace_path": workspace_path, "source_dir": source_dir},
        )

        # Run the coding agent in the work-handler pool (long-running)
        from ... import thread_pools
        result = await thread_pools.run_in_work_pool(
            coding_dispatch.invoke_coding_agent, workspace_path, prompt, agent_name
        )

        # Re-check arc status after the long-running agent call.
        # During the await, the main loop continued processing other work items.
        # If the arc was cancelled/failed (e.g., by user action or stale cleanup),
        # do not enqueue further work.
        arc_after = arc_manager.get_arc(arc_id)
        if not arc_after or arc_after["status"] != "active":
            logger.warning(
                "invoke-agent: arc %d status changed to '%s' during coding agent "
                "execution. Not enqueuing generate-review.",
                arc_id, arc_after["status"] if arc_after else "deleted",
            )
            return

        _set_arc_state(arc_id, "agent_result", result)

        arc_manager.add_history(
            arc_id, "agent_completed",
            {
                "exit_code": result.get("exit_code", -1),
                "iterations": result.get("iterations", 0),
                "stdout_preview": result.get("stdout", "")[:STDOUT_PREVIEW_MAX_CHARS],
            },
        )

        # Enqueue the next step
        from ..engine import work_queue
        import time as _time
        work_queue.enqueue(
            f"{CODING_CHANGE_PREFIX}.{STEP_GENERATE_REVIEW}",
            {"arc_id": arc_id},
            idempotency_key=f"{CODING_CHANGE_PREFIX}-review-{arc_id}-{int(_time.time())}",
            max_retries=work_queue.SINGLE_ATTEMPT,
        )

    except Exception as e:  # broad catch: coding agent may raise anything
        logger.exception("invoke-agent failed for arc %d", arc_id)
        arc_manager.add_history(
            arc_id, "error", {"message": str(e)},
        )
        # Clean up workspace on failure
        ws = _get_arc_state(arc_id, "workspace_path")
        if ws:
            workspace_manager.cleanup_workspace(ws)
        try:
            arc_manager.update_status(arc_id, "failed")
        except ValueError:
            # Arc is already in a terminal status. Log but don't propagate —
            # the error history entry above is the important record.
            logger.warning(
                "invoke-agent: could not transition arc %d to 'failed' "
                "(already terminal)",
                arc_id,
            )
        await _notify_and_respond(arc_id, f"Coding change failed: {e}")


async def handle_generate_review(work_id: int, payload: dict):
    """Handle the generate-review step of a coding-change arc.

    Reads diff from workspace and creates a review link.
    """
    arc_id = payload.get("arc_id")
    if not arc_id:
        logger.error("generate-review: missing arc_id in payload")
        return

    # Verify arc is in a valid status before proceeding.
    # If the arc was cancelled/failed between invoke-agent and this handler,
    # continuing would produce orphan state or overwrite failure diagnostics.
    # We accept 'pending' because startup recovery may have reset 'active'
    # back to 'pending' if the server restarted between invoke-agent and
    # generate-review (the work item was already enqueued, so the agent
    # completed successfully).
    arc_check = arc_manager.get_arc(arc_id)
    if not arc_check:
        logger.error("generate-review: arc %d not found", arc_id)
        return
    if arc_check["status"] == "pending":
        logger.info(
            "generate-review: arc %d is 'pending' (likely startup recovery). "
            "Transitioning to 'active' and proceeding.",
            arc_id,
        )
        arc_manager.update_status(arc_id, "active")
    elif arc_check["status"] != "active":
        logger.warning(
            "generate-review: arc %d has status '%s', expected 'active'. "
            "Skipping to avoid overwriting failure state.",
            arc_id, arc_check["status"],
        )
        return

    workspace_path = _get_arc_state(arc_id, "workspace_path")
    if not workspace_path:
        logger.error("generate-review: no workspace_path in arc_state for arc %d", arc_id)
        arc_manager.add_history(
            arc_id, "error",
            {"message": "No workspace_path found in arc_state"},
        )
        arc_manager.update_status(arc_id, "failed")
        return

    # Verify workspace directory exists before trying to read diff
    if not os.path.isdir(workspace_path):
        logger.error(
            "generate-review: workspace directory missing for arc %d: %s",
            arc_id, workspace_path,
        )
        arc_manager.add_history(
            arc_id, "error",
            {"message": f"Workspace directory was deleted: {workspace_path}"},
        )
        arc_manager.update_status(arc_id, "failed")
        return

    try:
        diff = workspace_manager.get_diff(workspace_path)

        if not diff.strip():
            arc_manager.add_history(
                arc_id, "no_changes", {"message": "No changes were made"},
            )
            workspace_manager.cleanup_workspace(workspace_path)
            arc_manager.update_status(arc_id, "completed")
            await _notify_and_respond(arc_id, "Coding agent finished but made no changes.")
            return

        changed_files = workspace_manager.get_changed_files(workspace_path)

        # Warn about file patterns that suggest agent confusion
        _CONFUSION_FILES = {"config.yaml", "config.json", "config.toml"}
        _CONFUSION_PREFIXES = ("kb/", ".carpenter_")
        suspicious = [
            f for f in changed_files
            if f in _CONFUSION_FILES
            or any(f.startswith(p) for p in _CONFUSION_PREFIXES)
        ]
        if suspicious:
            logger.warning(
                "generate-review: arc %d has suspicious changed files "
                "(possible agent confusion): %s",
                arc_id, suspicious,
            )
            arc_manager.add_history(
                arc_id, "warning",
                {"message": f"Suspicious files in diff: {suspicious}",
                 "suspicious_files": suspicious},
            )

        _set_arc_state(arc_id, "diff", diff)
        _set_arc_state(arc_id, "changed_files", changed_files)

        # Check for duplicate tool names before proceeding to review.
        source_dir = _get_arc_state(arc_id, "source_dir", "")
        dupes = _check_duplicate_tool_names(workspace_path, changed_files, source_dir)
        if dupes:
            names = ", ".join(d[0] for d in dupes)
            msg = (
                f"Duplicate tool name(s) detected: {names}. "
                f"These names already exist in the platform tool definitions. "
                f"The coding-change arc has been cancelled."
            )
            arc_manager.add_history(
                arc_id, "error",
                {"message": msg, "duplicate_tools": [d[0] for d in dupes]},
            )
            await _notify_and_respond(arc_id, msg)
            workspace_manager.cleanup_workspace(workspace_path)
            arc_manager.update_status(arc_id, "failed")
            return

        # Create review link
        from ...api.review import create_diff_review
        review_data = create_diff_review(
            diff_content=diff,
            arc_id=arc_id,
            title=f"Coding changes for {source_dir}",
            changed_files=changed_files,
        )
        review_url = review_data.get("url", "")
        _set_arc_state(arc_id, "review_url", review_url)
        _set_arc_state(arc_id, "review_id", review_data.get("review_id", ""))

        arc_manager.add_history(
            arc_id, "review_created",
            {
                "review_url": review_url,
                "changed_files": changed_files,
                "diff_lines": len(diff.splitlines()),
            },
        )

        # Create verification arcs (pre-approval) if enabled.
        # Verification runs BEFORE human approval so the human sees AI feedback.
        # The arc stays "active" — the judge handler will transition to "waiting"
        # after verification (and docs) complete.
        from ..arcs.verification import try_create_verification_arcs
        verification_enabled = try_create_verification_arcs(arc_id, label="arc")

        n_files = len(changed_files)
        if verification_enabled:
            # Keep arc active — judge handler will transition to "waiting"
            await _notify_and_respond(
                arc_id,
                f"Review ready ({n_files} file{'s' if n_files != 1 else ''} changed). "
                f"AI verification in progress. Review: {review_url}",
            )
        else:
            # No verification — go directly to waiting for human approval.
            arc = arc_manager.get_arc(arc_id)
            if arc and arc["status"] == "active":
                arc_manager.update_status(arc_id, "waiting")
            else:
                actual_status = arc["status"] if arc else "deleted"
                logger.error(
                    "generate-review: arc %d expected 'active' but found '%s' "
                    "at transition point. This is a logic error — the arc "
                    "status should not change between handler entry and here.",
                    arc_id, actual_status,
                )
                arc_manager.add_history(
                    arc_id, "error",
                    {
                        "message": (
                            f"generate-review could not transition to 'waiting': "
                            f"arc status was '{actual_status}' (expected 'active')"
                        ),
                    },
                )
                return
            await _notify_and_respond(
                arc_id,
                f"Review ready ({n_files} file{'s' if n_files != 1 else ''} changed). "
                f"Open the review: {review_url}",
            )

    except Exception as e:  # broad catch: workspace/review operations may raise anything
        logger.exception("generate-review failed for arc %d", arc_id)
        arc_manager.add_history(arc_id, "error", {"message": str(e)})
        # Clean up workspace on failure
        if workspace_path:
            workspace_manager.cleanup_workspace(workspace_path)
        try:
            arc_manager.update_status(arc_id, "failed")
        except ValueError:
            logger.warning(
                "generate-review: could not transition arc %d to 'failed' "
                "(already terminal)",
                arc_id,
            )
        await _notify_and_respond(arc_id, f"Coding change failed: {e}")


async def handle_approval(work_id: int, payload: dict):
    """Handle the await-approval step of a coding-change arc.

    Payload keys:
        arc_id: The coding-change arc ID
        decision: "approve", "reject", or "revise"
        feedback: Optional feedback text (used with revise)
    """
    arc_id = payload.get("arc_id")
    decision = payload.get("decision", "")
    feedback = payload.get("feedback", "")

    if not arc_id:
        logger.error("approval: missing arc_id in payload")
        return

    workspace_path = _get_arc_state(arc_id, "workspace_path")
    source_dir = _get_arc_state(arc_id, "source_dir")

    # Guard: reject approval if verification is still running
    if _get_arc_state(arc_id, "_verification_pending", False):
        logger.warning("approval: arc %d has verification pending, rejecting", arc_id)
        await _notify_and_respond(
            arc_id,
            "Cannot process approval: AI verification is still in progress. "
            "Please wait for verification to complete.",
        )
        return

    # Reactivate from waiting
    arc = arc_manager.get_arc(arc_id)
    if arc and arc["status"] == "waiting":
        arc_manager.update_status(arc_id, "active")

    if decision == "approve":
        if workspace_path and source_dir:
            try:
                applied = workspace_manager.apply_to_source(workspace_path, source_dir)
            except workspace_manager.MergeConflictError as e:
                # Try auto-resolve if configured
                if config.CONFIG.get("auto_resolve_merge_conflicts", False):
                    from .merge_handler import create_merge_resolution_arc
                    merge_arc = create_merge_resolution_arc(
                        source_dir=source_dir,
                        target_ref=f"_carpenter_merge_{arc_id}",
                        merge_type="branch",
                        parent_arc_id=arc_id,
                        conflict_context={
                            "conflicting_files": e.conflicting_files,
                            "conflict_diff": e.conflict_diff[:CONFLICT_DIFF_MAX_CHARS] if e.conflict_diff else "",
                        },
                    )
                    if merge_arc:
                        arc_manager.update_status(arc_id, "waiting")
                        await _notify_and_respond(
                            arc_id,
                            f"Merge conflict — {len(e.conflicting_files)} file(s). "
                            f"Auto-resolution arc {merge_arc} created.",
                        )
                        return

                # Fall back: surface merge conflict to user with details
                from ...api.review import create_diff_review
                review_data = create_diff_review(
                    diff_content=e.conflict_diff or str(e),
                    arc_id=arc_id,
                    title=f"Merge conflict: {len(e.conflicting_files)} file(s)",
                    changed_files=e.conflicting_files,
                )
                review_url = review_data.get("url", "")
                _set_arc_state(arc_id, "conflict_review_url", review_url)
                arc_manager.add_history(
                    arc_id, "merge_conflict",
                    {"conflicting_files": e.conflicting_files, "review_url": review_url},
                )
                arc_manager.update_status(arc_id, "waiting")
                await _notify_and_respond(
                    arc_id,
                    f"Merge conflict — {len(e.conflicting_files)} file(s) conflict. "
                    f"Review: {review_url}",
                )
                return
            except RuntimeError as e:
                arc_manager.add_history(
                    arc_id, "apply_failed", {"error": str(e)},
                )
                await _notify_and_respond(
                    arc_id,
                    f"Failed to apply changes — source has diverged. Error: {e}",
                )
                arc_manager.update_status(arc_id, "failed")
                return

            arc_manager.add_history(
                arc_id, "changes_applied",
                {"files": applied, "source_dir": source_dir},
            )

            # Sync config_seed/chat_tools/ → runtime config/chat_tools/ so the
            # hot-reload heartbeat picks up newly added or modified chat tools.
            _sync_config_seed_chat_tools(applied, source_dir)

        if workspace_path:
            workspace_manager.cleanup_workspace(workspace_path)
        arc_manager.update_status(arc_id, "completed")
        await _notify_and_respond(arc_id, f"Changes approved and applied to {source_dir}.")

        # Percolate KB entries for changed source files
        if workspace_path and source_dir and applied:
            try:
                from ...kb.autogen import queue_source_changes
                queue_source_changes(applied, source_dir)
            except (ImportError, ValueError, OSError) as e:
                logger.debug("KB percolation skipped: %s", e)

    elif decision == "reject":
        arc_manager.add_history(
            arc_id, "rejected",
            {"feedback": feedback},
        )
        if workspace_path:
            workspace_manager.cleanup_workspace(workspace_path)
        arc_manager.update_status(arc_id, "cancelled")
        await _notify_and_respond(arc_id, "Changes rejected. Workspace cleaned up.")

    elif decision == "revise":
        # Track review attempts and check for auto-escalation
        rework_count = _get_arc_state(arc_id, "rework_count", 0)
        review_attempts = _get_arc_state(arc_id, "review_attempts", 0)
        review_history = _get_arc_state(arc_id, "review_history", [])

        # Increment counters
        rework_count += 1
        review_attempts += 1

        # Record this attempt in history
        import time
        review_history.append({
            "attempt": review_attempts,
            "outcome": "REWORK",
            "reason": feedback,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

        # Check for auto-escalation
        if rework_count >= MAX_REWORK_ATTEMPTS:
            # Auto-escalate to MAJOR
            arc_manager.add_history(
                arc_id, "auto_escalated",
                {
                    "reason": f"Agent failed to satisfy reviewer after {MAX_REWORK_ATTEMPTS} attempts",
                    "rework_count": rework_count,
                    "feedback": feedback,
                },
            )
            _set_arc_state(arc_id, "escalated", True)
            _set_arc_state(arc_id, "rework_count", rework_count)
            _set_arc_state(arc_id, "review_attempts", review_attempts)
            _set_arc_state(arc_id, "review_history", review_history)

            # Keep workspace but require human decision
            arc_manager.update_status(arc_id, "waiting")
            await _notify_and_respond(
                arc_id,
                f"Auto-escalated to MAJOR: Agent failed to satisfy reviewer after {MAX_REWORK_ATTEMPTS} attempts.\n\n"
                f"Latest feedback: {feedback}\n\n"
                f"Options:\n"
                f"1. Approve changes anyway (if acceptable)\n"
                f"2. Reject and abandon this approach\n"
                f"3. Expand the plan or provide different guidance",
            )
            return

        # Update arc state with new counts
        _set_arc_state(arc_id, "rework_count", rework_count)
        _set_arc_state(arc_id, "review_attempts", review_attempts)
        _set_arc_state(arc_id, "review_history", review_history)

        arc_manager.add_history(
            arc_id, "revision_requested",
            {
                "feedback": feedback,
                "attempt": review_attempts,
                "rework_count": rework_count,
            },
        )

        # Re-run the agent with feedback appended to the original prompt.
        # Frame clearly so the coding agent understands this is a revision.
        from ...agent import templates
        original_prompt = _get_arc_state(arc_id, "original_prompt", "")
        revised_prompt = templates.render(
            "revision_feedback",
            original_prompt=original_prompt,
            rework_count=rework_count,
            feedback=feedback,
        )

        from ..engine import work_queue
        # Clear the old idempotency key suffix to allow re-enqueue
        work_queue.enqueue(
            f"{CODING_CHANGE_PREFIX}.{STEP_INVOKE_AGENT}",
            {
                "arc_id": arc_id,
                "source_dir": source_dir,
                "prompt": revised_prompt,
                "coding_agent": _get_arc_state(arc_id, "coding_agent"),
            },
            idempotency_key=f"{CODING_CHANGE_PREFIX}-revise-{arc_id}-{int(time.time())}",
            max_retries=work_queue.SINGLE_ATTEMPT,
        )
        _notify_chat(
            arc_id,
            f"Revision requested (Attempt {rework_count}/{MAX_REWORK_ATTEMPTS}). Coding agent is reworking the changes..."
        )
    else:
        logger.warning("Unknown decision '%s' for arc %d", decision, arc_id)


def register_handlers(register_fn):
    """Register all coding-change handlers with the main loop.

    Args:
        register_fn: The main_loop.register_handler function.
    """
    register_fn(f"{CODING_CHANGE_PREFIX}.{STEP_INVOKE_AGENT}", handle_invoke_agent)
    register_fn(f"{CODING_CHANGE_PREFIX}.{STEP_GENERATE_REVIEW}", handle_generate_review)
    register_fn(f"{CODING_CHANGE_PREFIX}.{STEP_APPROVAL}", handle_approval)
