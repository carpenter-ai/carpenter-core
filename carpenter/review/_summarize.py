"""Deterministic distillation of the user's *trusted* request.

Used by the Quarantined Quality Reviewer (QQR) to produce a small, T-only
summary of *what the user asked for* without exposing the QQR LLM to any
tainted bytes that might be present elsewhere in the conversation history.

The two valid sources, in priority order:

1. ``arc.goal`` — when an ``arc_id`` is provided and the arc has a goal,
   this is T by definition (set by trusted platform code or by the
   originating planner) and is preferred over the conversation feed.
2. The most recent **user-role** message in the conversation — the user's
   own typed text. We deliberately do NOT walk further back; only the
   single most-recent user turn is admitted, and only if it is a plain
   text message (no structured ``content_json`` payload — which would
   indicate tool I/O, never a typed-by-user request).

No assistant messages, no system messages, no tool outputs are ever
included. No LLM is invoked. No recursion through history.

The output is deterministically truncated to ``MAX_SUMMARY_CHARS`` and
returned with a fixed ``[trusted-request]`` label so the QQR prompt can
reference it without string interpolation of runtime data into the
hardcoded system prompt.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Hard cap on the distilled summary. Long enough to convey intent for
# typical chat tasks, short enough to make prompt-injection-via-length
# attacks unappealing.
MAX_SUMMARY_CHARS = 1000

# Fixed marker so the QQR prompt references the summary without ever
# embedding free-form user-controlled text in the system prompt itself.
TRUSTED_REQUEST_MARKER = "[trusted-request]"

# Returned when no T-validated request can be distilled. Allows QQR to run
# (with a sentinel) rather than crash, while signaling unambiguously that
# no chat-history context was admitted.
EMPTY_SUMMARY = f"{TRUSTED_REQUEST_MARKER} (no trusted request available)"


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_SUMMARY_CHARS:
        return text
    return text[:MAX_SUMMARY_CHARS].rstrip() + " […truncated]"


def _arc_goal(arc_id: int) -> Optional[str]:
    """Return ``arc.goal`` for ``arc_id`` if present, else None.

    arc.goal is T by construction: it is written by trusted platform code
    or by the originating PLANNER (whose own integrity is T). We never
    read any other arc field — no state, no history, no description.
    """
    try:
        from ..core.arcs import manager as arc_manager
    except Exception:  # pragma: no cover — import-time bug
        logger.exception("QQR summariser: arc manager import failed")
        return None

    try:
        arc = arc_manager.get_arc(arc_id)
    except Exception:
        logger.exception("QQR summariser: get_arc(%d) failed", arc_id)
        return None

    if not arc:
        return None
    goal = arc.get("goal")
    if not goal:
        return None
    if not isinstance(goal, str):
        # Defensive — schema says TEXT, but never trust the row blindly.
        return None
    return goal


def _most_recent_user_message(conversation_id: int) -> Optional[str]:
    """Return the most recent user-role plain-text message text, or None.

    Skips messages with ``content_json`` (structured tool I/O), system
    messages, and assistant messages. Returns the raw user-typed text;
    truncation/labelling is the caller's responsibility.
    """
    try:
        from ..agent import conversation as conversation_mod
    except Exception:  # pragma: no cover — import-time bug
        logger.exception("QQR summariser: conversation import failed")
        return None

    try:
        messages = conversation_mod.get_messages(conversation_id)
    except Exception:
        logger.exception(
            "QQR summariser: get_messages(%d) failed", conversation_id,
        )
        return None

    # Walk newest-first; admit only the single most recent user-role
    # message with plain text content.
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        if msg.get("content_json"):
            # Structured payload (tool result echoed back, etc.) — refuse.
            continue
        content = msg.get("content")
        if not content or not isinstance(content, str):
            continue
        return content
    return None


def summarize_trusted_request(
    conversation_id: int,
    arc_id: int | None = None,
) -> str:
    """Produce the T-only distilled request for the QQR call.

    Priority:
      1. If ``arc_id`` is provided and the arc has a non-empty goal, use it.
      2. Otherwise, use the most recent user-role plain-text message in the
         conversation.
      3. Otherwise return :data:`EMPTY_SUMMARY`.

    Result is wrapped with :data:`TRUSTED_REQUEST_MARKER` and truncated to
    :data:`MAX_SUMMARY_CHARS`. No LLM is invoked. No untrusted bytes
    (assistant messages, tool results, system prose) are ever consulted.

    This function MUST remain pure-Python and MUST NOT import any module
    that could perform I/O against external resources (web, KB, etc.).
    """
    # Source (1): trusted arc goal.
    if arc_id is not None:
        goal = _arc_goal(arc_id)
        if goal:
            return f"{TRUSTED_REQUEST_MARKER} {_truncate(goal)}"

    # Source (2): most recent user message.
    user_text = _most_recent_user_message(conversation_id)
    if user_text:
        return f"{TRUSTED_REQUEST_MARKER} {_truncate(user_text)}"

    return EMPTY_SUMMARY
