"""Handler for external coding-change arc workflow.

Handles the steps of an external-coding-change arc:
1. clone-and-branch: Clone external repo and create feature branch
2. invoke-agent: Run coding agent in the cloned workspace
3. local-review: (optional) Generate diff for local approval before push
4. push-and-pr: Commit, push to fork, create PR

Registered in the coordinator for external-coding-change.* event types.
"""

import logging
import time

from ... import config
from ..arcs import CODING_CHANGE_PREFIX, manager as arc_manager
from ..engine import work_queue
from ._arc_state import get_arc_state as _get_arc_state, set_arc_state as _set_arc_state
from .coding_change_handler import (
    _notify_chat,
    _notify_and_respond,
)
from ...agent import coding_dispatch
from ...tool_backends import git as git_backend
from ...tool_backends import forgejo_api as forgejo_api_backend

logger = logging.getLogger(__name__)

# Step name constants
STEP_CLONE_AND_BRANCH = "clone-and-branch"
STEP_INVOKE_AGENT = "invoke-agent"
STEP_LOCAL_REVIEW = "local-review"
STEP_PUSH_AND_PR = "push-and-pr"

# Maximum characters to include in stdout preview in arc history entries
STDOUT_PREVIEW_MAX_CHARS = 500


async def handle_clone_and_branch(work_id: int, payload: dict):
    """Clone the external repo and create a feature branch.

    Payload keys:
        arc_id: The parent external-coding-change arc ID
        repo_url: Upstream repository URL
        fork_url: Fork repository URL
        branch_name: Feature branch name
        workspace: Path for the clone
    """
    arc_id = payload.get("arc_id")
    repo_url = payload.get("repo_url")
    fork_url = payload.get("fork_url")
    branch_name = payload.get("branch_name")
    workspace = payload.get("workspace")

    if not arc_id or not repo_url:
        logger.error("clone-and-branch: missing required fields in payload")
        return

    # Activate arc
    arc = arc_manager.get_arc(arc_id)
    if arc and arc["status"] == "pending":
        arc_manager.update_status(arc_id, "active")

    # Default workspace from config
    if not workspace:
        import os
        workspaces_dir = config.CONFIG.get("workspaces_dir", "/tmp")
        workspace = os.path.join(workspaces_dir, f"ext-{arc_id}")

    # Default branch name
    if not branch_name:
        branch_name = f"tc/change-{arc_id}"

    try:
        # Clone or reconfigure repo
        setup_result = git_backend.handle_setup_repo({
            "repo_url": repo_url,
            "workspace": workspace,
            "fork_url": fork_url or repo_url,
        })

        if not setup_result.get("success"):
            error = setup_result.get("error", "unknown error")
            arc_manager.add_history(arc_id, "error", {"message": f"Clone failed: {error}"})
            arc_manager.update_status(arc_id, "failed")
            await _notify_and_respond(arc_id, f"Failed to clone repository: {error}")
            return

        # Create feature branch
        branch_result = git_backend.handle_create_branch({
            "workspace": workspace,
            "branch_name": branch_name,
        })

        _set_arc_state(arc_id, "workspace_path", workspace)
        _set_arc_state(arc_id, "branch_name", branch_name)
        _set_arc_state(arc_id, "repo_url", repo_url)
        _set_arc_state(arc_id, "fork_url", fork_url or repo_url)

        arc_manager.add_history(arc_id, "cloned", {
            "workspace": workspace,
            "branch_name": branch_name,
            "branch_created": branch_result.get("created", False),
        })

        # Enqueue next step
        work_queue.enqueue(
            f"external-{CODING_CHANGE_PREFIX}.{STEP_INVOKE_AGENT}",
            {"arc_id": arc_id},
            idempotency_key=f"ext-cc-agent-{arc_id}-{int(time.time())}",
            max_retries=work_queue.SINGLE_ATTEMPT,
        )

    except Exception as e:  # broad catch: git operations may raise anything
        logger.exception("clone-and-branch failed for arc %d", arc_id)
        arc_manager.add_history(arc_id, "error", {"message": str(e)})
        arc_manager.update_status(arc_id, "failed")
        await _notify_and_respond(arc_id, f"Clone and branch failed: {e}")


async def handle_invoke_agent(work_id: int, payload: dict):
    """Run the coding agent in the cloned workspace.

    Payload keys:
        arc_id: The parent external-coding-change arc ID
        prompt: (optional) Override the coding instruction
        coding_agent: (optional) Name of the coding agent profile
    """
    arc_id = payload.get("arc_id")
    if not arc_id:
        logger.error("invoke-agent: missing arc_id in payload")
        return

    workspace = _get_arc_state(arc_id, "workspace_path")
    prompt = payload.get("prompt") or _get_arc_state(arc_id, "prompt", "")
    agent_name = payload.get("coding_agent") or _get_arc_state(arc_id, "coding_agent")

    if not workspace:
        logger.error("invoke-agent: no workspace for arc %d", arc_id)
        arc_manager.update_status(arc_id, "failed")
        return

    from ... import thread_pools
    try:
        # Run the coding agent in the work-handler pool (long-running)
        result = await thread_pools.run_in_work_pool(
            coding_dispatch.invoke_coding_agent, workspace, prompt, agent_name,
        )
        _set_arc_state(arc_id, "agent_result", result)

        arc_manager.add_history(arc_id, "agent_completed", {
            "exit_code": result.get("exit_code", -1),
            "iterations": result.get("iterations", 0),
            "stdout_preview": result.get("stdout", "")[:STDOUT_PREVIEW_MAX_CHARS],
        })

        # Try to create verification arcs (AI review before push)
        from ..arcs.verification import try_create_verification_arcs
        verification_created = try_create_verification_arcs(
            arc_id, label=f"external-{CODING_CHANGE_PREFIX}",
        )

        if not verification_created:
            # No verification — proceed directly to review/push
            local_review = _get_arc_state(arc_id, "local_review", False)
            if local_review:
                work_queue.enqueue(
                    f"external-{CODING_CHANGE_PREFIX}.{STEP_LOCAL_REVIEW}",
                    {"arc_id": arc_id},
                    idempotency_key=f"ext-cc-review-{arc_id}-{int(time.time())}",
                    max_retries=work_queue.SINGLE_ATTEMPT,
                )
            else:
                # Skip to push-and-pr
                work_queue.enqueue(
                    f"external-{CODING_CHANGE_PREFIX}.{STEP_PUSH_AND_PR}",
                    {"arc_id": arc_id},
                    idempotency_key=f"ext-cc-push-{arc_id}-{int(time.time())}",
                    max_retries=work_queue.SINGLE_ATTEMPT,
                )

    except Exception as e:  # broad catch: coding agent may raise anything
        logger.exception("invoke-agent failed for arc %d", arc_id)
        arc_manager.add_history(arc_id, "error", {"message": str(e)})
        arc_manager.update_status(arc_id, "failed")
        await _notify_and_respond(arc_id, f"Coding agent failed: {e}")


async def handle_local_review(work_id: int, payload: dict):
    """Generate diff for local review before push.

    Payload keys:
        arc_id: The parent external-coding-change arc ID
    """
    arc_id = payload.get("arc_id")
    if not arc_id:
        logger.error("local-review: missing arc_id in payload")
        return

    workspace = _get_arc_state(arc_id, "workspace_path")
    if not workspace:
        logger.error("local-review: no workspace for arc %d", arc_id)
        arc_manager.update_status(arc_id, "failed")
        return

    try:
        from .. import workspace_manager
        diff = workspace_manager.get_diff(workspace)

        if not diff.strip():
            arc_manager.add_history(arc_id, "no_changes", {"message": "No changes were made"})
            arc_manager.update_status(arc_id, "completed")
            await _notify_and_respond(arc_id, "Coding agent finished but made no changes.")
            return

        changed_files = workspace_manager.get_changed_files(workspace)
        _set_arc_state(arc_id, "diff", diff)
        _set_arc_state(arc_id, "changed_files", changed_files)

        # Create review link
        from ...api.review import create_diff_review
        review_data = create_diff_review(
            diff_content=diff,
            arc_id=arc_id,
            title=f"External coding changes (arc #{arc_id})",
            changed_files=changed_files,
        )
        _set_arc_state(arc_id, "review_url", review_data.get("url", ""))
        _set_arc_state(arc_id, "review_id", review_data.get("review_id", ""))

        arc_manager.update_status(arc_id, "waiting")
        n_files = len(changed_files)
        await _notify_and_respond(
            arc_id,
            f"Review ready ({n_files} file{'s' if n_files != 1 else ''} changed). "
            f"Open the review: {review_data.get('url', '')}",
        )

    except Exception as e:  # broad catch: workspace/review operations may raise anything
        logger.exception("local-review failed for arc %d", arc_id)
        arc_manager.add_history(arc_id, "error", {"message": str(e)})
        arc_manager.update_status(arc_id, "failed")
        await _notify_and_respond(arc_id, f"Local review failed: {e}")


async def handle_push_and_pr(work_id: int, payload: dict):
    """Commit changes, push to fork, and create PR.

    Payload keys:
        arc_id: The parent external-coding-change arc ID
    """
    arc_id = payload.get("arc_id")
    if not arc_id:
        logger.error("push-and-pr: missing arc_id in payload")
        return

    workspace = _get_arc_state(arc_id, "workspace_path")
    branch_name = _get_arc_state(arc_id, "branch_name")
    commit_message = _get_arc_state(arc_id, "commit_message", "Changes by Carpenter")
    pr_title = _get_arc_state(arc_id, "pr_title", "")
    pr_body = _get_arc_state(arc_id, "pr_body", "")
    repo_owner = _get_arc_state(arc_id, "repo_owner", "")
    repo_name = _get_arc_state(arc_id, "repo_name", "")
    fork_user = _get_arc_state(arc_id, "fork_user", "")

    if not workspace or not branch_name:
        logger.error("push-and-pr: missing workspace/branch for arc %d", arc_id)
        arc_manager.update_status(arc_id, "failed")
        return

    try:
        # Get changed files
        from .. import workspace_manager
        changed_files = workspace_manager.get_changed_files(workspace)
        if not changed_files:
            arc_manager.add_history(arc_id, "no_changes", {"message": "No changes to push"})
            arc_manager.update_status(arc_id, "completed")
            await _notify_and_respond(arc_id, "No changes to push.")
            return

        # Commit and push
        push_result = git_backend.handle_commit_and_push({
            "workspace": workspace,
            "branch_name": branch_name,
            "commit_message": commit_message,
            "files": changed_files,
        })

        if not push_result.get("pushed"):
            error = push_result.get("error", "unknown error")
            arc_manager.add_history(arc_id, "push_failed", {"error": error})
            arc_manager.update_status(arc_id, "failed")
            await _notify_and_respond(arc_id, f"Push failed: {error}")
            return

        _set_arc_state(arc_id, "commit_sha", push_result.get("commit_sha", ""))

        arc_manager.add_history(arc_id, "pushed", {
            "branch": branch_name,
            "commit_sha": push_result.get("commit_sha", ""),
        })

        # Create PR if we have repo coordinates
        if repo_owner and repo_name and fork_user:
            if not pr_title:
                arc = arc_manager.get_arc(arc_id)
                pr_title = arc.get("goal", "Changes by Carpenter") if arc else "Changes by Carpenter"

            pr_result = forgejo_api_backend.handle_create_pr({
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "branch_name": branch_name,
                "pr_title": pr_title,
                "pr_body": pr_body,
                "fork_user": fork_user,
            })

            if "error" in pr_result:
                arc_manager.add_history(arc_id, "pr_failed", {"error": pr_result["error"]})
                arc_manager.update_status(arc_id, "failed")
                await _notify_and_respond(arc_id, f"PR creation failed: {pr_result['error']}")
                return

            pr_url = pr_result.get("pr_url", "")
            pr_number = pr_result.get("pr_number", 0)
            _set_arc_state(arc_id, "pr_url", pr_url)
            _set_arc_state(arc_id, "pr_number", pr_number)

            arc_manager.add_history(arc_id, "pr_created", {
                "pr_number": pr_number,
                "pr_url": pr_url,
            })

            arc_manager.update_status(arc_id, "completed")
            await _notify_and_respond(
                arc_id,
                f"PR #{pr_number} created: {pr_url}",
            )
        else:
            # No PR — just push
            arc_manager.update_status(arc_id, "completed")
            await _notify_and_respond(
                arc_id,
                f"Changes pushed to {branch_name}.",
            )

    except Exception as e:  # broad catch: git push/PR operations may raise anything
        logger.exception("push-and-pr failed for arc %d", arc_id)
        arc_manager.add_history(arc_id, "error", {"message": str(e)})
        arc_manager.update_status(arc_id, "failed")
        await _notify_and_respond(arc_id, f"Push and PR failed: {e}")


def register_handlers(register_fn):
    """Register external-coding-change handlers with the main loop.

    Args:
        register_fn: The main_loop.register_handler function.
    """
    register_fn(f"external-{CODING_CHANGE_PREFIX}.{STEP_CLONE_AND_BRANCH}", handle_clone_and_branch)
    register_fn(f"external-{CODING_CHANGE_PREFIX}.{STEP_INVOKE_AGENT}", handle_invoke_agent)
    register_fn(f"external-{CODING_CHANGE_PREFIX}.{STEP_LOCAL_REVIEW}", handle_local_review)
    register_fn(f"external-{CODING_CHANGE_PREFIX}.{STEP_PUSH_AND_PR}", handle_push_and_pr)
