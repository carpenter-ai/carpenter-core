"""Handler for skill-kb-review template workflow.

Triggered when an agent writes to a KB entry under the ``skills/`` path.
Runs a tiered review pipeline based on conversation taint status:

- **classify-source**: Python-only — checks conversation taint
- **text-review**: Python-only — progressive text review for tainted sources,
  auto-passes for clean sources
- **intent-review**: AI REVIEWER agent (dispatched by arc_dispatch_handler)
- **human-escalation**: Python-only — auto-skipped for clean sources when
  intent-review passes; blocks and notifies for tainted sources

The Python-only steps (classify-source, text-review, human-escalation) are
called from ``arc_dispatch_handler`` when it detects child arcs whose
parent arc is a ``skill-kb-review`` instance.
"""

import logging

from ... import config
from ...agent import conversation
from ...db import get_db, db_transaction
from ._arc_state import get_arc_state as _get_arc_state, set_arc_state as _set_arc_state
from ._notifications import notify_arc_conversation
from ...security.trust import is_conversation_tainted
from ..arcs import manager as arc_manager
from .. import notifications
from ..engine import work_queue

logger = logging.getLogger(__name__)

# Step name constants
STEP_CLASSIFY_SOURCE = "classify-source"
STEP_TEXT_REVIEW = "text-review"
STEP_INTENT_REVIEW = "intent-review"
STEP_HUMAN_ESCALATION = "human-escalation"


def _complete_and_propagate(arc_id: int) -> None:
    """Mark arc completed, freeze it, and propagate to siblings."""
    arc_manager.update_status(arc_id, "completed")
    arc_manager.freeze_arc(arc_id)
    from ..arcs.dispatch_handler import _propagate_completion
    _propagate_completion(arc_id)


# ── Trigger ──────────────────────────────────────────────────────────────

def trigger_review(
    path: str,
    content_hash: str,
    conversation_id: int | None = None,
) -> int | None:
    """Create a skill-kb-review arc for a modified skill KB entry.

    Args:
        path: The KB path that was written (e.g. ``skills/fibonacci``).
        content_hash: SHA-256 hash of the new content.
        conversation_id: The conversation that triggered the write, if any.

    Returns:
        The parent arc ID, or None if reviews are disabled.
    """
    review_config = config.CONFIG.get("skill_kb_review", {})
    if not review_config.get("enabled", True):
        logger.debug("Skill-KB review disabled, skipping for %s", path)
        return None

    from ..engine import template_manager

    template = template_manager.get_template_by_name("skill-kb-review")
    if not template:
        logger.warning("skill-kb-review template not found, skipping review for %s", path)
        return None

    # Create the parent arc
    parent_id = arc_manager.create_arc(
        name="skill-kb-review",
        goal=f"Review skill KB modification: {path}",
    )

    # Instantiate template steps as children
    child_ids = template_manager.instantiate_template(template["id"], parent_id)

    # Store context in parent arc state
    _set_arc_state(parent_id, "kb_path", path)
    _set_arc_state(parent_id, "content_hash", content_hash)
    if conversation_id is not None:
        _set_arc_state(parent_id, "conversation_id", conversation_id)
        # Link the parent arc to the conversation so child arcs (especially
        # intent-review) can find it via _find_arc_conversation().
        with db_transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO conversation_arcs "
                "(conversation_id, arc_id) VALUES (?, ?)",
                (conversation_id, parent_id),
            )

    arc_manager.update_status(parent_id, "active")

    # Enqueue the first child (classify-source) via arc.dispatch
    if child_ids:
        work_queue.enqueue(
            "arc.dispatch",
            {"arc_id": child_ids[0]},
            idempotency_key=f"arc_dispatch:{child_ids[0]}",
        )

    logger.info(
        "Created skill-kb-review arc %d for %s (%d steps)",
        parent_id, path, len(child_ids),
    )
    return parent_id


# ── Step handlers (called from arc_dispatch_handler) ─────────────────────

def is_skill_kb_review_step(arc_info: dict) -> bool:
    """Check if an arc is a Python-only skill-kb-review step.

    Returns True for classify-source, text-review, and human-escalation
    arcs whose parent is a skill-kb-review arc.
    """
    arc_name = arc_info.get("name", "")
    if arc_name not in (STEP_CLASSIFY_SOURCE, STEP_TEXT_REVIEW, STEP_HUMAN_ESCALATION):
        return False

    parent_id = arc_info.get("parent_id")
    if parent_id is None:
        return False

    parent = arc_manager.get_arc(parent_id)
    return parent is not None and parent.get("name") == "skill-kb-review"


async def handle_classify_source(arc_id: int, arc_info: dict) -> None:
    """Classify whether the KB modification source is clean or tainted.

    Reads conversation_id from the parent arc state and checks the
    conversation_taint table.  Stores ``_source_tainted`` on the parent
    arc so sibling steps can read it.
    """
    parent_id = arc_info.get("parent_id")
    if parent_id is None:
        logger.error("classify-source arc %d has no parent", arc_id)
        arc_manager.update_status(arc_id, "failed")
        return

    # Activate
    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    conversation_id = _get_arc_state(parent_id, "conversation_id")
    kb_path = _get_arc_state(parent_id, "kb_path", "unknown")

    tainted = False
    if conversation_id is not None:
        tainted = is_conversation_tainted(conversation_id)

    _set_arc_state(parent_id, "_source_tainted", tainted)
    arc_manager.add_history(arc_id, "classify_source", {
        "tainted": tainted,
        "conversation_id": conversation_id,
        "kb_path": kb_path,
    })

    logger.info(
        "classify-source arc %d: conversation %s is %s for %s",
        arc_id, conversation_id, "TAINTED" if tainted else "CLEAN", kb_path,
    )

    _complete_and_propagate(arc_id)


async def handle_text_review(arc_id: int, arc_info: dict) -> None:
    """Progressive text review for tainted sources; auto-pass for clean.

    For tainted sources, runs ``run_progressive_text_review()`` on the KB
    entry content.  If escalation is triggered, sets ``_verdict`` to
    ``"fail"`` which blocks subsequent template steps.
    """
    parent_id = arc_info.get("parent_id")
    if parent_id is None:
        logger.error("text-review arc %d has no parent", arc_id)
        arc_manager.update_status(arc_id, "failed")
        return

    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    tainted = _get_arc_state(parent_id, "_source_tainted", False)
    kb_path = _get_arc_state(parent_id, "kb_path", "")

    if not tainted:
        # Clean source — auto-pass
        _set_arc_state(arc_id, "_verdict", {"verdict": "pass", "reason": "clean source"})
        arc_manager.add_history(arc_id, "text_review", {
            "skipped": True,
            "reason": "source not tainted",
        })
        logger.info("text-review arc %d: auto-pass (clean source)", arc_id)
        _complete_and_propagate(arc_id)
        return

    # Tainted source — run progressive text review
    from ...kb.store import KBStore
    store = KBStore()
    entry = store.get_entry(kb_path)
    content = entry["content"] if entry else ""

    if not content:
        _set_arc_state(arc_id, "_verdict", {"verdict": "pass", "reason": "empty content"})
        arc_manager.add_history(arc_id, "text_review", {
            "skipped": True,
            "reason": "no content to review",
        })
        _complete_and_propagate(arc_id)
        return

    try:
        from ...review.injection_defense import run_progressive_text_review
        escalate, flags = run_progressive_text_review([content])
    except (ImportError, ValueError, RuntimeError) as _exc:
        logger.exception("text-review arc %d: progressive review failed", arc_id)
        _set_arc_state(arc_id, "_verdict", {
            "verdict": "fail",
            "reason": "text review error",
        })
        arc_manager.add_history(arc_id, "text_review_error", {
            "error": "progressive text review raised an exception",
        })
        _complete_and_propagate(arc_id)
        return

    if escalate:
        verdict = "fail"
        reason = "progressive text review flagged content for escalation"
    else:
        verdict = "pass"
        reason = "progressive text review passed"

    _set_arc_state(arc_id, "_verdict", {"verdict": verdict, "reason": reason})
    arc_manager.add_history(arc_id, "text_review", {
        "escalate": escalate,
        "flags": flags,
        "verdict": verdict,
    })

    logger.info(
        "text-review arc %d: %s (%d flags)", arc_id, verdict.upper(), len(flags),
    )

    _complete_and_propagate(arc_id)


async def handle_human_escalation(arc_id: int, arc_info: dict) -> None:
    """Handle the human-escalation step as a Python-only dispatch.

    Called from arc_dispatch_handler when a human-escalation arc under a
    skill-kb-review parent is dispatched.  Decides whether to auto-complete
    (clean source + intent review passed) or block with notification (tainted).
    """
    parent_id = arc_info.get("parent_id")
    if parent_id is None:
        logger.error("human-escalation arc %d has no parent", arc_id)
        arc_manager.update_status(arc_id, "failed")
        return

    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    tainted = _get_arc_state(parent_id, "_source_tainted", False)
    review_config = config.CONFIG.get("skill_kb_review", {})
    human_for_tainted = review_config.get("human_escalation_for_tainted", True)

    # Find the intent-review sibling and check its verdict
    intent_passed = True
    children = arc_manager.get_children(parent_id)
    for child in children:
        if child.get("name") == STEP_INTENT_REVIEW:
            intent_verdict = _get_arc_state(child["id"], "_verdict")
            if isinstance(intent_verdict, dict):
                intent_passed = intent_verdict.get("verdict", "pass") == "pass"
            elif isinstance(intent_verdict, str):
                intent_passed = intent_verdict == "pass"
            break

    if not tainted and intent_passed:
        # Clean source with clean intent — auto-complete
        arc_manager.add_history(arc_id, "auto_skipped", {
            "reason": "clean source with passing intent review",
        })
        logger.info(
            "Auto-completed human-escalation arc %d (clean source)", arc_id,
        )
        _complete_and_propagate(arc_id)
    elif tainted and not human_for_tainted:
        # Config says skip human escalation even for tainted
        arc_manager.add_history(arc_id, "auto_skipped", {
            "reason": "human_escalation_for_tainted=False",
        })
        logger.info(
            "Auto-completed human-escalation arc %d (config override)", arc_id,
        )
        _complete_and_propagate(arc_id)
    else:
        # Human escalation required — notify and wait for manual trigger.
        # Guard against duplicate notifications from heartbeat re-dispatch.
        already_notified = _get_arc_state(arc_id, "_escalation_notified", False)
        if not already_notified:
            _notify_human_escalation(parent_id, arc_id)
            _set_arc_state(arc_id, "_escalation_notified", True)
            arc_manager.add_history(arc_id, "awaiting_human", {
                "tainted": tainted,
                "intent_passed": intent_passed,
            })
        logger.info(
            "human-escalation arc %d: awaiting manual trigger "
            "(tainted=%s, intent_passed=%s)",
            arc_id, tainted, intent_passed,
        )


def _notify_human_escalation(parent_id: int, arc_id: int) -> None:
    """Notify the user that a skill KB modification requires human approval.

    Sends both a chat message (so the user sees it in conversation) and a
    notification (so it routes to email if configured).
    """
    kb_path = _get_arc_state(parent_id, "kb_path", "unknown")
    conv_id = _get_arc_state(parent_id, "conversation_id")

    msg = (
        f"A skill KB modification to '{kb_path}' requires your approval. "
        f"The source conversation was tainted (exposed to untrusted content). "
        f"Review arc #{parent_id}, then trigger 'arc.manual_trigger' on "
        f"arc #{arc_id} to approve."
    )

    # Inject system message into the linked conversation
    notify_arc_conversation(
        arc_id, msg, conversation_id=conv_id,
        also_notify=True, priority="urgent", category="review_needed"
    )
