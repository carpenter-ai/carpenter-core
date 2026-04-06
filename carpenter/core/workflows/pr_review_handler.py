"""Handler for PR review arc workflow.

Handles the steps of a pr-review arc (created by webhook dispatch):
1. fetch-pr: Fetch PR metadata and diff from the forge API
2. ai-review: Run AI code review on the diff
3. post-review: Post review verdict and comments to the forge
4. notify: Notify the user of review results

Registered in the coordinator for pr-review.* event types.
"""

import json
import logging

from ... import config
from ..arcs import manager as arc_manager
from ..engine import work_queue
from ._arc_state import get_arc_state as _get_arc_state, set_arc_state as _set_arc_state
from .coding_change_handler import (
    _notify_chat,
    _notify_and_respond,
)
from ...tool_backends import git as git_backend
from ...tool_backends import forgejo_api as forgejo_api_backend

logger = logging.getLogger(__name__)

# Step name constants
STEP_FETCH_PR = "fetch-pr"
STEP_AI_REVIEW = "ai-review"
STEP_POST_REVIEW = "post-review"
STEP_NOTIFY = "notify"

# PR review system prompt for the AI reviewer
_PR_REVIEW_SYSTEM_PROMPT = """\
You are a code reviewer. Review the following pull request diff and provide:
1. A summary of the changes
2. Any issues found (bugs, security concerns, style problems)
3. Suggestions for improvement
4. An overall verdict: APPROVED, REQUEST_CHANGES, or COMMENT

Format your response as JSON with these keys:
- summary: Brief summary of the changes
- issues: List of issues found (each with "file", "line" (optional), "severity", "description")
- suggestions: List of improvement suggestions
- verdict: One of "APPROVED", "REQUEST_CHANGES", or "COMMENT"
- body: The full review comment to post (markdown formatted)
"""


async def handle_fetch_pr(work_id: int, payload: dict):
    """Fetch PR metadata and diff from the forge API.

    Payload keys:
        arc_id: The pr-review arc ID
    """
    arc_id = payload.get("arc_id")
    if not arc_id:
        logger.error("fetch-pr: missing arc_id in payload")
        return

    # Activate arc
    arc = arc_manager.get_arc(arc_id)
    if arc and arc["status"] == "pending":
        arc_manager.update_status(arc_id, "active")

    # Get PR coordinates from arc state (set by webhook_dispatch_handler)
    repo_owner = _get_arc_state(arc_id, "repo_owner", "")
    repo_name = _get_arc_state(arc_id, "repo_name", "")
    pr_number = _get_arc_state(arc_id, "pr_number")

    if not repo_owner or not repo_name or not pr_number:
        logger.error(
            "fetch-pr: missing PR coordinates for arc %d "
            "(repo_owner=%s, repo_name=%s, pr_number=%s)",
            arc_id, repo_owner, repo_name, pr_number,
        )
        arc_manager.update_status(arc_id, "failed")
        return

    try:
        # Fetch PR metadata
        pr_result = forgejo_api_backend.handle_get_pr({
            "repo_owner": repo_owner,
            "repo_name": repo_name,
            "pr_number": pr_number,
        })

        if "error" in pr_result:
            arc_manager.add_history(arc_id, "error", {
                "message": f"Failed to fetch PR: {pr_result['error']}",
            })
            arc_manager.update_status(arc_id, "failed")
            await _notify_and_respond(
                arc_id, f"Failed to fetch PR #{pr_number}: {pr_result['error']}"
            )
            return

        _set_arc_state(arc_id, "pr_metadata", pr_result)

        # Fetch PR diff
        diff_result = forgejo_api_backend.handle_get_pr_diff({
            "repo_owner": repo_owner,
            "repo_name": repo_name,
            "pr_number": pr_number,
        })

        if "error" in diff_result:
            arc_manager.add_history(arc_id, "error", {
                "message": f"Failed to fetch diff: {diff_result['error']}",
            })
            arc_manager.update_status(arc_id, "failed")
            await _notify_and_respond(
                arc_id, f"Failed to fetch diff for PR #{pr_number}: {diff_result['error']}"
            )
            return

        _set_arc_state(arc_id, "pr_diff", diff_result.get("diff", ""))

        arc_manager.add_history(arc_id, "pr_fetched", {
            "pr_number": pr_number,
            "title": pr_result.get("title", ""),
            "diff_lines": len(diff_result.get("diff", "").splitlines()),
        })

        # Enqueue AI review step
        work_queue.enqueue(f"pr-review.{STEP_AI_REVIEW}", {"arc_id": arc_id})

    except Exception as e:  # broad catch: forge API calls may raise anything
        logger.exception("fetch-pr failed for arc %d", arc_id)
        arc_manager.add_history(arc_id, "error", {"message": str(e)})
        arc_manager.update_status(arc_id, "failed")
        await _notify_and_respond(arc_id, f"PR fetch failed: {e}")


async def handle_ai_review(work_id: int, payload: dict):
    """Run AI code review on the PR diff.

    Payload keys:
        arc_id: The pr-review arc ID
    """
    arc_id = payload.get("arc_id")
    if not arc_id:
        logger.error("ai-review: missing arc_id in payload")
        return

    pr_metadata = _get_arc_state(arc_id, "pr_metadata", {})
    pr_diff = _get_arc_state(arc_id, "pr_diff", "")

    if not pr_diff:
        logger.warning("ai-review: empty diff for arc %d", arc_id)
        _set_arc_state(arc_id, "review_result", {
            "verdict": "COMMENT",
            "body": "No changes to review (empty diff).",
            "summary": "Empty diff",
            "issues": [],
            "suggestions": [],
        })
        work_queue.enqueue(f"pr-review.{STEP_POST_REVIEW}", {"arc_id": arc_id})
        return

    # Build the review prompt
    pr_title = pr_metadata.get("title", "")
    pr_body = pr_metadata.get("body", "")
    head_branch = pr_metadata.get("head_branch", "")
    base_branch = pr_metadata.get("base_branch", "")

    user_prompt = (
        f"## Pull Request: {pr_title}\n"
        f"Branch: {head_branch} → {base_branch}\n"
    )
    if pr_body:
        user_prompt += f"\n### Description\n{pr_body}\n"
    user_prompt += f"\n### Diff\n```diff\n{pr_diff}\n```\n"

    try:
        # Resolve model for PR review
        from ...agent.model_resolver import get_model_for_role, create_client_for_model, parse_model_string
        model_str = get_model_for_role("pr_review")
        # Fall back to code_review slot if pr_review not configured
        if not config.CONFIG.get("model_roles", {}).get("pr_review"):
            model_str = get_model_for_role("code_review")

        client = create_client_for_model(model_str)
        _, model_name = parse_model_string(model_str)

        # Call the AI in the work-handler pool (long-running)
        from ... import thread_pools
        messages = [{"role": "user", "content": user_prompt}]
        response = await thread_pools.run_in_work_pool(
            client.chat, messages, model_name,
            system=_PR_REVIEW_SYSTEM_PROMPT,
            max_tokens=4096,
        )

        # Parse the response
        response_text = ""
        if isinstance(response, dict):
            # Anthropic-style response
            content = response.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        response_text += block.get("text", "")
            elif isinstance(content, str):
                response_text = content
            # OpenAI-style response (ollama, local)
            if not response_text:
                choices = response.get("choices", [])
                if choices:
                    response_text = choices[0].get("message", {}).get("content", "")
        elif isinstance(response, str):
            response_text = response

        # Try to parse as JSON, fall back to using the text as-is
        review_result = _parse_review_response(response_text)
        _set_arc_state(arc_id, "review_result", review_result)
        _set_arc_state(arc_id, "review_model", model_str)

        arc_manager.add_history(arc_id, "ai_review_completed", {
            "verdict": review_result.get("verdict", "COMMENT"),
            "issues_count": len(review_result.get("issues", [])),
            "model": model_str,
        })

        # Enqueue post-review step
        work_queue.enqueue(f"pr-review.{STEP_POST_REVIEW}", {"arc_id": arc_id})

    except Exception as e:  # broad catch: AI client calls may raise anything
        logger.exception("ai-review failed for arc %d", arc_id)
        arc_manager.add_history(arc_id, "error", {"message": str(e)})
        arc_manager.update_status(arc_id, "failed")
        await _notify_and_respond(arc_id, f"AI review failed: {e}")


def _parse_review_response(text: str) -> dict:
    """Parse AI review response, trying JSON first then plain text.

    Returns a dict with keys: verdict, body, summary, issues, suggestions.
    """
    # Try to extract JSON from the response
    # The AI might wrap it in ```json ... ``` or return it directly
    json_text = text.strip()

    # Strip markdown code fences if present
    if json_text.startswith("```"):
        lines = json_text.split("\n")
        # Remove first line (```json) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        json_text = "\n".join(lines)

    try:
        result = json.loads(json_text)
        # Validate required fields
        if isinstance(result, dict) and "verdict" in result:
            # Normalize verdict
            verdict = result.get("verdict", "COMMENT").upper()
            if verdict not in ("APPROVED", "REQUEST_CHANGES", "COMMENT"):
                verdict = "COMMENT"
            result["verdict"] = verdict
            result.setdefault("body", text)
            result.setdefault("summary", "")
            result.setdefault("issues", [])
            result.setdefault("suggestions", [])
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Fall back to plain text — use the whole response as the review body
    # Try to detect verdict from text
    verdict = "COMMENT"
    text_lower = text.lower()
    if "approve" in text_lower and "request" not in text_lower:
        verdict = "APPROVED"
    elif "request changes" in text_lower or "request_changes" in text_lower:
        verdict = "REQUEST_CHANGES"

    summary_max = config.get_config("pr_review_summary_max_length", 200)
    return {
        "verdict": verdict,
        "body": text,
        "summary": text[:summary_max] if len(text) > summary_max else text,
        "issues": [],
        "suggestions": [],
    }


async def handle_post_review(work_id: int, payload: dict):
    """Post review verdict and comments to the forge.

    Payload keys:
        arc_id: The pr-review arc ID
    """
    arc_id = payload.get("arc_id")
    if not arc_id:
        logger.error("post-review: missing arc_id in payload")
        return

    repo_owner = _get_arc_state(arc_id, "repo_owner", "")
    repo_name = _get_arc_state(arc_id, "repo_name", "")
    pr_number = _get_arc_state(arc_id, "pr_number")
    review_result = _get_arc_state(arc_id, "review_result", {})

    if not repo_owner or not repo_name or not pr_number:
        logger.error("post-review: missing PR coordinates for arc %d", arc_id)
        arc_manager.update_status(arc_id, "failed")
        return

    verdict = review_result.get("verdict", "COMMENT")
    body = review_result.get("body", "No review comments.")

    # Build line comments from issues if available
    comments = []
    for issue in review_result.get("issues", []):
        if isinstance(issue, dict) and issue.get("file") and issue.get("line"):
            comments.append({
                "path": issue["file"],
                "new_position": issue["line"],
                "body": issue.get("description", ""),
            })

    try:
        post_result = forgejo_api_backend.handle_post_pr_review({
            "repo_owner": repo_owner,
            "repo_name": repo_name,
            "pr_number": pr_number,
            "body": body,
            "event": verdict,
            "comments": comments if comments else None,
        })

        if "error" in post_result:
            arc_manager.add_history(arc_id, "post_review_failed", {
                "error": post_result["error"],
            })
            arc_manager.update_status(arc_id, "failed")
            await _notify_and_respond(
                arc_id,
                f"Failed to post review to PR #{pr_number}: {post_result['error']}",
            )
            return

        review_id = post_result.get("review_id")
        _set_arc_state(arc_id, "forge_review_id", review_id)

        arc_manager.add_history(arc_id, "review_posted", {
            "verdict": verdict,
            "review_id": review_id,
            "pr_number": pr_number,
        })

        # Enqueue notify step
        work_queue.enqueue(f"pr-review.{STEP_NOTIFY}", {"arc_id": arc_id})

    except Exception as e:  # broad catch: forge API calls may raise anything
        logger.exception("post-review failed for arc %d", arc_id)
        arc_manager.add_history(arc_id, "error", {"message": str(e)})
        arc_manager.update_status(arc_id, "failed")
        await _notify_and_respond(arc_id, f"Review posting failed: {e}")


async def handle_notify(work_id: int, payload: dict):
    """Notify the user of review results.

    Payload keys:
        arc_id: The pr-review arc ID
    """
    arc_id = payload.get("arc_id")
    if not arc_id:
        logger.error("notify: missing arc_id in payload")
        return

    pr_number = _get_arc_state(arc_id, "pr_number")
    review_result = _get_arc_state(arc_id, "review_result", {})
    pr_metadata = _get_arc_state(arc_id, "pr_metadata", {})
    webhook_data = _get_arc_state(arc_id, "webhook_data", {})

    verdict = review_result.get("verdict", "COMMENT")
    summary = review_result.get("summary", "")
    pr_title = pr_metadata.get("title", webhook_data.get("pr_title", ""))
    html_url = pr_metadata.get("html_url", webhook_data.get("html_url", ""))

    # Build notification message
    verdict_label = {
        "APPROVED": "Approved",
        "REQUEST_CHANGES": "Changes Requested",
        "COMMENT": "Commented",
    }.get(verdict, verdict)

    message = f"PR #{pr_number} review complete: {verdict_label}"
    if pr_title:
        message += f" — {pr_title}"
    if html_url:
        message += f"\n{html_url}"
    if summary:
        message += f"\n\nSummary: {summary}"

    # Send notification via the notifications system
    from .. import notifications
    notifications.notify(
        message,
        priority="normal",
        category="pr_review",
    )

    # Mark arc as completed
    arc_manager.update_status(arc_id, "completed")
    arc_manager.add_history(arc_id, "review_notified", {
        "verdict": verdict,
        "pr_number": pr_number,
    })

    # Also send to chat if there's a conversation
    await _notify_and_respond(arc_id, message)

    logger.info(
        "PR review arc %d completed: PR #%s verdict=%s",
        arc_id, pr_number, verdict,
    )


def register_handlers(register_fn):
    """Register PR review handlers with the main loop.

    Args:
        register_fn: The main_loop.register_handler function.
    """
    register_fn(f"pr-review.{STEP_FETCH_PR}", handle_fetch_pr)
    register_fn(f"pr-review.{STEP_AI_REVIEW}", handle_ai_review)
    register_fn(f"pr-review.{STEP_POST_REVIEW}", handle_post_review)
    register_fn(f"pr-review.{STEP_NOTIFY}", handle_notify)
