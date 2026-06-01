"""Review diff API endpoint for Carpenter.

Generates one-time review links with rendered code diffs.
Each link has a unique UUID and can only be used once.
"""
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

import attrs
import cattrs
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .. import constants
from ..db import get_db, db_connection, db_transaction
from ..core.arcs import CODING_CHANGE_PREFIX, manager as arc_manager
from .static import read_asset, load_template

logger = logging.getLogger(__name__)

# In-memory store for review links (recovered from DB on startup)
_review_links: dict[str, dict] = {}


def recover_review_links():
    """Recover review links from arc_state after server restart.

    Scans arcs that are in 'waiting' or 'active' status and have a
    stored review_id + diff. Reconstructs the in-memory _review_links
    so that existing review URLs continue to work.
    """
    with db_connection() as db:
        try:
            # Find arcs with review data that aren't yet resolved
            rows = db.execute(
                "SELECT a.id as arc_id, a.status, a.name, a.goal "
                "FROM arcs a "
                "WHERE a.status IN ('waiting', 'active') "
                "AND EXISTS (SELECT 1 FROM arc_state s WHERE s.arc_id = a.id AND s.key = 'review_id')"
            ).fetchall()

            recovered = 0
            for row in rows:
                arc_id = row["arc_id"]
                # Get all state for this arc
                state_rows = db.execute(
                    "SELECT key, value_json FROM arc_state WHERE arc_id = ?",
                    (arc_id,),
                ).fetchall()
                state = {}
                for sr in state_rows:
                    try:
                        state[sr["key"]] = json.loads(sr["value_json"])
                    except (json.JSONDecodeError, TypeError):
                        state[sr["key"]] = sr["value_json"]

                review_id = state.get("review_id")
                diff = state.get("diff")
                if not review_id or not diff:
                    continue

                # Skip if already in memory (shouldn't happen on fresh start)
                if review_id in _review_links:
                    continue

                _review_links[review_id] = {
                    "review_type": "diff",
                    "diff_content": diff,
                    "arc_id": arc_id,
                    "title": f"Coding changes: {row['goal'] or row['name']}",
                    "changed_files": state.get("changed_files", []),
                    "reviewer": "user",
                    "created_at": "(recovered)",
                    "used": False,
                }
                recovered += 1

            if recovered:
                logger.info("Recovered %d review link(s) from database", recovered)
        except (sqlite3.Error, OSError, ValueError) as e:
            logger.warning("Failed to recover review links: %s", e)


@attrs.define
class ReviewRequest:
    code_file_id: int
    arc_id: int | None = None
    reviewer: str = "user"


@attrs.define
class DiffReviewRequest:
    diff_content: str
    arc_id: int | None = None
    title: str = "Diff Review"
    changed_files: list[str] = attrs.Factory(list)
    reviewer: str = "user"


@attrs.define
class ReviewDecision:
    decision: str  # "approved", "rejected", or "revise"
    comment: str = ""
    followup_goal: str = ""


async def create_review_link(request: Request):
    """Create a one-time review link for a code file.

    Returns the review UUID and URL.
    """
    body = cattrs.structure(await request.json(), ReviewRequest)
    review_id = str(uuid.uuid4())

    # Get the code file
    with db_connection() as db:
        row = db.execute(
            "SELECT * FROM code_files WHERE id = ?", (body.code_file_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Code file not found")
        file_path = row["file_path"]

    # Read code content
    try:
        with open(file_path) as f:
            code_content = f.read()
    except OSError:
        code_content = "(file not found on disk)"

    _review_links[review_id] = {
        "code_file_id": body.code_file_id,
        "arc_id": body.arc_id,
        "reviewer": body.reviewer,
        "code_content": code_content,
        "file_path": file_path,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "used": False,
    }

    return JSONResponse(content={
        "review_id": review_id,
        "url": f"/api/review/{review_id}",
    })


def create_diff_review(
    diff_content: str,
    arc_id: int | None = None,
    title: str = "Diff Review",
    changed_files: list[str] | None = None,
    reviewer: str = "user",
    outcome: str | None = None,
    filename_map: dict[str, str] | None = None,
    attempt_count: int | None = None,
    review_type: str = "diff",
    qqr_signal: object | None = None,
) -> dict:
    """Create a diff review link programmatically (called from handlers).

    Args:
        diff_content: Unified diff content
        arc_id: Associated arc ID
        title: Review title
        changed_files: List of changed file paths
        reviewer: Reviewer name/ID
        outcome: Review outcome (APPROVE, REWORK, MAJOR, etc.)
        filename_map: Mapping of obfuscated filenames to originals
        attempt_count: Current attempt number (for retry tracking)
        review_type: "diff" (default)

    Returns dict with review_id and url.
    """
    review_id = str(uuid.uuid4())

    _review_links[review_id] = {
        "review_type": review_type,
        "diff_content": diff_content,
        "arc_id": arc_id,
        "title": title,
        "changed_files": changed_files or [],
        "reviewer": reviewer,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "used": False,
        # Enhanced metadata for review outcomes
        "outcome": outcome,
        "filename_map": filename_map or {},
        "attempt_count": attempt_count,
        # Quarantined Quality Reviewer signal (None / QqrSignal). Rendered
        # in the human-review panel alongside the main reviewer's verdict.
        "qqr_signal": qqr_signal,
    }

    return {
        "review_id": review_id,
        "url": f"/api/review/{review_id}",
    }


async def create_diff_review_endpoint(request: Request):
    """Create a one-time diff review link via API.

    Returns the review UUID and URL.
    """
    body = cattrs.structure(await request.json(), DiffReviewRequest)
    return JSONResponse(content=create_diff_review(
        diff_content=body.diff_content,
        arc_id=body.arc_id,
        title=body.title,
        changed_files=body.changed_files,
        reviewer=body.reviewer,
    ))


def _render_diff_html(diff_content: str) -> str:
    """Render a unified diff as HTML with green/red line coloring."""
    lines = []
    for line in diff_content.split("\n"):
        escaped = (
            line.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        if line.startswith("+++") or line.startswith("---"):
            lines.append(f'<span class="diff-header">{escaped}</span>')
        elif line.startswith("@@"):
            lines.append(f'<span class="diff-hunk">{escaped}</span>')
        elif line.startswith("+"):
            lines.append(f'<span class="diff-add">{escaped}</span>')
        elif line.startswith("-"):
            lines.append(f'<span class="diff-del">{escaped}</span>')
        else:
            lines.append(f'<span class="diff-ctx">{escaped}</span>')
    return "".join(lines)


async def view_review(request: Request):
    """View a code or diff review with approve/reject/revise buttons.

    Returns an HTML page with syntax-highlighted content and action buttons.
    """
    review_id = request.path_params["review_id"]
    review = _review_links.get(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review link not found or expired")

    if review["used"]:
        raise HTTPException(status_code=410, detail="Review link already used")

    review_type = review.get("review_type", "code")

    if review_type == "diff":
        return _render_diff_review_page(review_id, review)
    else:
        return _render_code_review_page(review_id, review)


def _render_code_review_page(review_id: str, review: dict) -> HTMLResponse:
    """Render the code review HTML page."""
    code = review["code_content"]
    file_path = review["file_path"]

    code_escaped = (
        code.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

    css = read_asset("review-code.css")
    js = read_asset("review-code.js").replace(
        "__DECIDE_URL__", f"/api/review/{review_id}/decide"
    )

    html = load_template(
        "review-code.html",
        review_id_short=review_id[:8],
        css=css,
        js=js,
        file_path=file_path,
        code_escaped=code_escaped,
    )

    return HTMLResponse(content=html)


def _render_qqr_block(qqr_signal: object) -> str:
    """Render a small panel for the Quarantined Quality Reviewer verdict.

    ``qqr_signal`` is expected to be either None or a
    :class:`carpenter.review.qqr.QqrSignal`. ``reason_html`` is already
    HTML-escaped by the QqrSignal layer; we only frame it.
    """
    if qqr_signal is None:
        return ""
    try:
        verdict = getattr(qqr_signal, "verdict", None)
        category = getattr(qqr_signal, "category", "none")
        confidence = getattr(qqr_signal, "confidence", "low")
        reason_html = getattr(qqr_signal, "reason_html", "")
    except Exception:  # pragma: no cover — defensive
        return ""
    verdict_str = getattr(verdict, "value", str(verdict)).upper() if verdict else "UNKNOWN"
    color = {
        "APPROVE": "#a6e22e",
        "MINOR": "#e6db74",
        "MAJOR": "#f92672",
        "ABSTAIN": "#75715e",
    }.get(verdict_str, "#75715e")
    return (
        '<div style="margin: 15px 0;">'
        '<h3 style="color: #66d9ef; margin-bottom: 10px;">'
        'Quarantined Quality Reviewer (QQR)</h3>'
        f'<div style="background: #1e1e1e; padding: 12px; border-radius: 4px; '
        f'margin: 8px 0; border-left: 3px solid {color};">'
        f'<div style="margin: 4px 0;"><strong>Verdict:</strong> '
        f'<span style="color: {color}; font-weight: bold;">{verdict_str}</span></div>'
        f'<div style="margin: 4px 0; color: #ccc;"><strong>Category:</strong> {category} '
        f'<span style="margin-left: 12px;"><strong>Confidence:</strong> {confidence}</span></div>'
        f'<div style="margin: 8px 0; color: #ddd;">{reason_html}</div>'
        '<div style="margin: 8px 0; font-size: 11px; color: #888;">'
        'QQR sees only sanitised code + a deterministic, T-only summary of '
        'the user request. It does NOT see chat history.'
        '</div></div></div>'
    )


def _render_diff_review_page(review_id: str, review: dict) -> HTMLResponse:
    """Render the diff review HTML page with colored diff lines."""
    title = review.get("title", "Diff Review")
    changed_files = review.get("changed_files", [])
    diff_html = _render_diff_html(review["diff_content"])

    # Build outcome badge
    outcome = review.get("outcome")
    attempt_count = review.get("attempt_count")
    outcome_badge = ""
    if outcome:
        badge_colors = {
            "APPROVE": "#a6e22e",
            "REWORK": "#e6db74",
            "MAJOR": "#f92672",
            "REJECTED": "#ae81ff",
        }
        badge_icons = {
            "APPROVE": "\u2705",
            "REWORK": "\u26a0\ufe0f",
            "MAJOR": "\U0001f6a8",
            "REJECTED": "\U0001f6ab",
        }
        color = badge_colors.get(outcome, "#75715e")
        icon = badge_icons.get(outcome, "\u2022")
        attempt_text = (
            f" (Attempt {attempt_count}/{constants.MAX_REWORK_RETRIES})"
            if outcome == "REWORK" and attempt_count else ""
        )
        outcome_badge = f'<div class="outcome-badge" style="background: {color};">{icon} {outcome}{attempt_text}</div>'

    # Build filename mapping display (for internal reference)
    filename_info = ""
    filename_map = review.get("filename_map", {})
    if filename_map:
        map_items = "".join(
            f"<li><code>{obf}</code> \u2192 <code>{orig}</code></li>"
            for obf, orig in sorted(filename_map.items())
        )
        filename_info = f'<div class="filename-map"><strong>Filename Mapping (internal):</strong><ul>{map_items}</ul></div>'

    files_list = ""
    if changed_files:
        files_items = "".join(f"<li>{f}</li>" for f in changed_files)
        files_list = f'<div class="files-list"><strong>Changed files:</strong><ul>{files_items}</ul></div>'

    # Build AI reviews section
    ai_reviews_html = ""
    arc_id = review.get("arc_id")
    if arc_id:
        ai_reviews = _get_ai_reviews(arc_id)
        if ai_reviews:
            cards = []
            for ar in ai_reviews:
                status_color = {"completed": "#a6e22e", "active": "#66d9ef", "pending": "#75715e"}.get(ar["status"], "#75715e")
                status_badge = f'<span style="color: {status_color}; font-weight: bold;">{ar["status"]}</span>'
                model_label = ar["model"]

                if ar["status"] == "completed" and ar["findings"]:
                    f = ar["findings"]
                    verdict = f.get("verdict", "unknown")
                    verdict_color = "#a6e22e" if verdict == "approve" else "#e6db74"
                    verdict_html = f'<div style="margin: 8px 0;"><strong>Verdict:</strong> <span style="color: {verdict_color}; font-weight: bold;">{verdict.upper()}</span></div>'
                    summary_html = f'<div style="margin: 8px 0;">{f.get("summary", "")}</div>' if f.get("summary") else ""
                    issues = f.get("issues", [])
                    issues_html = ""
                    if issues:
                        items = "".join(f"<li>{iss}</li>" for iss in issues)
                        issues_html = f'<div style="margin: 8px 0;"><strong>Issues:</strong><ul style="margin: 4px 0; padding-left: 20px;">{items}</ul></div>'
                    recs = f.get("recommendations", [])
                    recs_html = ""
                    if recs:
                        items = "".join(f"<li>{rec}</li>" for rec in recs)
                        recs_html = f'<div style="margin: 8px 0;"><strong>Recommendations:</strong><ul style="margin: 4px 0; padding-left: 20px;">{items}</ul></div>'
                    body = f'{verdict_html}{summary_html}{issues_html}{recs_html}'
                elif ar["status"] in ("active", "pending"):
                    body = '<div style="color: #75715e; font-style: italic;">Review in progress...</div>'
                else:
                    body = '<div style="color: #75715e; font-style: italic;">No findings available.</div>'

                cards.append(
                    f'<div style="background: #1e1e1e; padding: 12px; border-radius: 4px; margin: 8px 0; border-left: 3px solid {status_color};">'
                    f'<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">'
                    f'<strong style="color: #66d9ef;">{model_label}</strong> {status_badge}'
                    f'</div>{body}</div>'
                )
            cards_html = "".join(cards)
            ai_reviews_html = f'<div style="margin: 15px 0;"><h3 style="color: #66d9ef; margin-bottom: 10px;">AI Reviews</h3>{cards_html}</div>'

    # Build QQR (Quarantined Quality Reviewer) panel.
    # Rendered alongside the main reviewer card so a human can see whether
    # both reviewers concurred. ``reason_html`` is already HTML-escaped at
    # the QqrSignal layer; we only style it.
    qqr_html = _render_qqr_block(review.get("qqr_signal"))
    if qqr_html:
        ai_reviews_html = ai_reviews_html + qqr_html

    # Build action buttons
    action_buttons = (
        '<button class="btn btn-approve" onclick="decide(\'approve\')">Approve</button>'
        '<button class="btn btn-revise" onclick="decide(\'revise\')">Revise</button>'
        '<button class="btn btn-reject" onclick="decide(\'reject\')">Reject</button>'
    )

    css = read_asset("review-diff.css")
    js = read_asset("review-diff.js").replace(
        "__DECIDE_URL__", f"/api/review/{review_id}/decide"
    ).replace(
        "__AI_REVIEW_URL__", f"/api/review/{review_id}/request-ai-review"
    )

    html = load_template(
        "review-diff.html",
        review_id_short=review_id[:8],
        css=css,
        js=js,
        title=title,
        outcome_badge=outcome_badge,
        filename_info=filename_info,
        ai_reviews_html=ai_reviews_html,
        files_list=files_list,
        diff_html=diff_html,
        action_buttons=action_buttons,
    )

    return HTMLResponse(content=html)


async def submit_decision(request: Request):
    """Submit approve/reject decision for a review."""
    review_id = request.path_params["review_id"]
    decision = cattrs.structure(await request.json(), ReviewDecision)

    review = _review_links.get(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review link not found")

    if review["used"]:
        raise HTTPException(status_code=410, detail="Review already submitted")

    # Mark as used
    review["used"] = True

    review_type = review.get("review_type", "code")

    if review_type == "code":
        # Update code file review status
        with db_transaction() as db:
            db.execute(
                "UPDATE code_files SET review_status = ? WHERE id = ?",
                (decision.decision, review["code_file_id"]),
            )

    # Log to arc history if arc_id is set
    if review.get("arc_id"):
        history_content = {
            "review_type": review_type,
            "decision": decision.decision,
            "comment": decision.comment,
            "reviewer": review.get("reviewer", "user"),
        }
        if review_type == "code":
            history_content["code_file_id"] = review["code_file_id"]

        arc_manager.add_history(
            review["arc_id"],
            "review_decision",
            history_content,
            code_file_id=review.get("code_file_id"),
            actor=review.get("reviewer", "user"),
        )

        # For diff reviews, enqueue approval handler and wake the main loop
        if review_type == "diff":
            from ..core.engine import work_queue
            from ..core.engine.main_loop import wake_signal
            work_queue.enqueue(
                f"{CODING_CHANGE_PREFIX}.approval",
                {
                    "arc_id": review["arc_id"],
                    "decision": decision.decision,
                    "feedback": decision.comment,
                },
            )
            wake_signal.set()

    # Optional: trigger a follow-up PLANNER arc when approving with a
    # follow-up goal. The review screen is trusted human input, so the new
    # arc is created at trusted integrity. It is a sibling of the current
    # arc (same parent_id) — or a new root if the current arc is a root.
    new_followup_arc_id: int | None = None
    if (
        review.get("arc_id")
        and decision.decision == "approve"
        and decision.followup_goal
        and decision.followup_goal.strip()
    ):
        try:
            current_arc = arc_manager.get_arc(review["arc_id"])
            sibling_parent_id = current_arc.get("parent_id") if current_arc else None
            new_followup_arc_id = arc_manager.create_arc(
                name="followup-from-review",
                goal=decision.followup_goal.strip(),
                parent_id=sibling_parent_id,
                integrity_level="trusted",
                agent_type="PLANNER",
            )
            # If the new arc is a root (no parent), create_arc only auto-enqueues
            # arc.dispatch for root arcs that have a code_file_id. Manually
            # enqueue to ensure the planner starts promptly.
            if sibling_parent_id is None:
                from ..core.engine import work_queue
                from ..core.engine.main_loop import wake_signal
                work_queue.enqueue(
                    "arc.dispatch",
                    {"arc_id": new_followup_arc_id},
                    idempotency_key=f"arc_dispatch:{new_followup_arc_id}",
                )
                wake_signal.set()
            arc_manager.add_history(
                review["arc_id"],
                "followup_triggered",
                {
                    "child_arc_id": new_followup_arc_id,
                    "goal": decision.followup_goal.strip(),
                    "parent_id": sibling_parent_id,
                },
                actor=review.get("reviewer", "user"),
            )
        except (sqlite3.Error, ValueError, KeyError) as exc:
            logger.warning(
                "Failed to create follow-up arc for review %s: %s", review_id, exc
            )
            new_followup_arc_id = None

    if review.get("arc_id"):

        # Inject immediate chat notification so user sees it on next poll
        from ..core.workflows.coding_change_handler import _get_arc_state
        from ..agent import conversation
        conv_id = _get_arc_state(review["arc_id"], "conversation_id")
        if conv_id is not None:
            labels = {"approve": "approved", "reject": "rejected", "revise": "revision requested"}
            label = labels.get(decision.decision, decision.decision)
            msg = f"Review decision: {label}."
            if decision.comment:
                msg += f" Feedback: {decision.comment}"
            conversation.add_message(int(conv_id), "system", msg, arc_id=review["arc_id"])

    response_body: dict = {
        "review_id": review_id,
        "decision": decision.decision,
        "recorded": True,
    }
    if new_followup_arc_id is not None:
        response_body["followup_arc_id"] = new_followup_arc_id
    return JSONResponse(content=response_body)


@attrs.define
class AIReviewRequest:
    model: str
    focus_areas: str | None = None


def _get_ai_reviews(arc_id: int) -> list[dict]:
    """Get ad-hoc REVIEWER arcs for a coding-change arc.

    Returns a list of dicts with reviewer_arc_id, model, status, and
    findings (if completed).
    """
    with db_connection() as db:
        rows = db.execute(
            "SELECT a.id, a.name, a.status, a.model_policy_id "
            "FROM arcs a "
            "WHERE a.parent_id = ? "
            "  AND a.agent_type = 'REVIEWER' "
            "  AND a.from_template = 0 "
            "  AND a.arc_role = 'worker' "
            "ORDER BY a.created_at DESC",
            (arc_id,),
        ).fetchall()

        reviews = []
        for row in rows:
            review = {
                "reviewer_arc_id": row["id"],
                "model": "unknown",
                "status": row["status"],
                "findings": None,
            }

            # Get model name from model_policies
            if row["model_policy_id"]:
                policy_row = db.execute(
                    "SELECT model FROM model_policies WHERE id = ?",
                    (row["model_policy_id"],),
                ).fetchone()
                if policy_row and policy_row["model"]:
                    # Extract short name from "provider:model_id"
                    model_str = policy_row["model"]
                    review["model"] = model_str.split(":")[-1] if ":" in model_str else model_str

            # Get findings from arc_state if completed
            if row["status"] == "completed":
                findings_row = db.execute(
                    "SELECT value_json FROM arc_state "
                    "WHERE arc_id = ? AND key = 'review_findings'",
                    (row["id"],),
                ).fetchone()
                if findings_row:
                    try:
                        review["findings"] = json.loads(findings_row["value_json"])
                    except (json.JSONDecodeError, TypeError):
                        pass

            reviews.append(review)

        return reviews


async def request_ai_review_endpoint(request: Request):
    """Request an ad-hoc AI review for a coding-change arc."""
    review_id = request.path_params["review_id"]
    body = cattrs.structure(await request.json(), AIReviewRequest)

    review = _review_links.get(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review link not found")

    arc_id = review.get("arc_id")
    if not arc_id:
        raise HTTPException(status_code=400, detail="No arc associated with this review")

    from ..tool_backends.arc import handle_request_ai_review
    result = handle_request_ai_review({
        "target_arc_id": arc_id,
        "model": body.model,
        "focus_areas": body.focus_areas,
    })

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return JSONResponse(content={
        "reviewer_arc_id": result["arc_id"],
        "model": body.model,
    })


def get_review(review_id: str) -> dict | None:
    """Get review data by ID (for testing)."""
    return _review_links.get(review_id)


def clear_reviews():
    """Clear all review links (for testing)."""
    _review_links.clear()


routes = [
    Route("/api/review/create", create_review_link, methods=["POST"]),
    Route("/api/review/create-diff", create_diff_review_endpoint, methods=["POST"]),
    Route("/api/review/{review_id}/decide", submit_decision, methods=["POST"]),
    Route("/api/review/{review_id}/request-ai-review", request_ai_review_endpoint, methods=["POST"]),
    Route("/api/review/{review_id}", view_review, methods=["GET"]),
]
