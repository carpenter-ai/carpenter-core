"""Python step handlers for the skill-kb-review template.

Three Python-only steps ship in this package:

- :func:`handle_classify_source` — reads the parent arc's
  ``conversation_id`` state and classifies the source as clean or
  tainted, stashing ``_source_tainted`` on the parent for siblings.
- :func:`handle_text_review` — progressive text review on tainted KB
  content; auto-passes for clean sources.
- :func:`handle_human_escalation` — auto-completes when the source is
  clean and the AI intent review passed; otherwise notifies and blocks
  awaiting manual trigger.

Registered in :mod:`__init__` via :func:`register_handlers` and routed
to by the generic ``handler_registry`` lookup in
``carpenter.core.arcs.dispatch_handler``. The trigger (subscription on
the ``kb.entry_written`` event) is declared in
``skill-kb-review.yaml``; the platform emits the event from
``kb/store.py`` and the template's own subscription creates the parent
arc. No platform code knows about this feature — the legacy intercept
and the ``carpenter/core/workflows/skill_kb_review_handler.py`` module
were removed in Phase E4.
"""

from __future__ import annotations

import logging

from carpenter import config
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.workflows._arc_state import (
    get_arc_state as _get_arc_state,
    set_arc_state as _set_arc_state,
)
from carpenter.core.workflows._notifications import notify_arc_conversation
from carpenter.security.trust import is_conversation_tainted

logger = logging.getLogger(__name__)


STEP_INTENT_REVIEW = "intent-review"


def _complete_and_propagate(arc_id: int) -> None:
    """Mark arc completed, freeze it, and propagate completion to siblings.

    Same shape as the reflection template's handlers: we reuse the
    platform's ``_propagate_completion`` helper rather than duplicating
    the sibling-advance logic inside the template.
    """
    from carpenter.core.arcs.dispatch_handler import _propagate_completion

    arc_manager.update_status(arc_id, "completed")
    arc_manager.freeze_arc(arc_id)
    _propagate_completion(arc_id)


async def handle_classify_source(arc_id: int, arc_info: dict) -> None:
    """Classify whether the KB modification source is clean or tainted.

    Reads ``conversation_id`` from the parent arc state and checks the
    conversation_taint table (platform primitive, shared with other
    security workflows). Stores ``_source_tainted`` on the parent arc so
    sibling steps can read it.

    Also — on first entry — links the parent arc to the triggering
    conversation in ``conversation_arcs`` so that downstream steps
    (notably the ``intent-review`` AI step) can find the conversation
    via the platform's ``_find_arc_conversation`` lookup. The
    subscription-based ``create_arc`` action doesn't know about
    conversations, so the link is the template's responsibility.
    """
    parent_id = arc_info.get("parent_id")
    if parent_id is None:
        logger.error("classify-source arc %d has no parent", arc_id)
        arc_manager.update_status(arc_id, "failed")
        return

    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    conversation_id = _get_arc_state(parent_id, "conversation_id")
    kb_path = _get_arc_state(parent_id, "kb_path", "unknown")

    # Link parent arc to conversation (idempotent via INSERT OR IGNORE).
    # Must happen before the taint check so that the link exists even if
    # taint detection raises — downstream steps still need the link.
    if conversation_id is not None:
        try:
            from carpenter.db import db_transaction
            with db_transaction() as db:
                db.execute(
                    "INSERT OR IGNORE INTO conversation_arcs "
                    "(conversation_id, arc_id) VALUES (?, ?)",
                    (conversation_id, parent_id),
                )
        except Exception:
            logger.exception(
                "classify-source arc %d: failed to link parent arc %d "
                "to conversation %s", arc_id, parent_id, conversation_id,
            )

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

    For tainted sources, runs ``run_progressive_text_review()`` on the
    KB entry content. If escalation is triggered, stores a ``fail``
    verdict so the downstream ``human-escalation`` step blocks.
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
        _set_arc_state(arc_id, "_verdict", {
            "verdict": "pass", "reason": "clean source",
        })
        arc_manager.add_history(arc_id, "text_review", {
            "skipped": True,
            "reason": "source not tainted",
        })
        logger.info("text-review arc %d: auto-pass (clean source)", arc_id)
        _complete_and_propagate(arc_id)
        return

    from carpenter.kb.store import KBStore
    store = KBStore()
    entry = store.get_entry(kb_path)
    content = entry["content"] if entry else ""

    if not content:
        _set_arc_state(arc_id, "_verdict", {
            "verdict": "pass", "reason": "empty content",
        })
        arc_manager.add_history(arc_id, "text_review", {
            "skipped": True,
            "reason": "no content to review",
        })
        _complete_and_propagate(arc_id)
        return

    try:
        from carpenter.review.injection_defense import run_progressive_text_review
        escalate, flags = run_progressive_text_review([content])
    except (ImportError, ValueError, RuntimeError):
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
        "text-review arc %d: %s (%d flags)",
        arc_id, verdict.upper(), len(flags),
    )

    _complete_and_propagate(arc_id)


async def handle_human_escalation(arc_id: int, arc_info: dict) -> None:
    """Auto-skip on clean + passing intent, otherwise block + notify.

    When the arc is in a state where human approval is required, a
    chat/urgent notification is emitted and the arc is left ``active``
    — waiting for an external ``arc.manual_trigger`` to complete it.
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
        arc_manager.add_history(arc_id, "auto_skipped", {
            "reason": "clean source with passing intent review",
        })
        logger.info(
            "Auto-completed human-escalation arc %d (clean source)", arc_id,
        )
        _complete_and_propagate(arc_id)
        return

    if tainted and not human_for_tainted:
        arc_manager.add_history(arc_id, "auto_skipped", {
            "reason": "human_escalation_for_tainted=False",
        })
        logger.info(
            "Auto-completed human-escalation arc %d (config override)", arc_id,
        )
        _complete_and_propagate(arc_id)
        return

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

    Sends both a chat message (so the user sees it in conversation) and
    a notification (so it routes to email if configured).
    """
    kb_path = _get_arc_state(parent_id, "kb_path", "unknown")
    conv_id = _get_arc_state(parent_id, "conversation_id")

    msg = (
        f"A skill KB modification to '{kb_path}' requires your approval. "
        f"The source conversation was tainted (exposed to untrusted content). "
        f"Review arc #{parent_id}, then trigger 'arc.manual_trigger' on "
        f"arc #{arc_id} to approve."
    )

    notify_arc_conversation(
        arc_id, msg, conversation_id=conv_id,
        also_notify=True, priority="urgent", category="review_needed",
    )
