"""Conversation persistence and context window management.

Two context modes:

1. **Single-conversation medium** (Signal, WhatsApp, Telegram, etc.):
   Uses get_or_create_conversation() which applies a 6-hour time boundary.
   Messages separated by more than 6 hours trigger a new conversation,
   carrying over the last ~10 messages as prior context.

2. **Conversation-specific UI** (web UI with tabs/dropdown):
   Uses get_last_conversation() or explicit conversation_id. No time-based
   truncation — the full conversation history is available to the agent
   until the context window is approximately full.

Multi-conversation support: each conversation can have a title, be
archived, and be linked to specific arcs.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone, timedelta

from ..db import get_db, db_connection, db_transaction
from .. import config

logger = logging.getLogger(__name__)


def get_last_conversation(exclude_archived: bool = True) -> int:
    """Get the most recent conversation, or create one if none exist.

    Unlike get_or_create_conversation(), this does NOT apply time-based
    boundaries. It returns the latest conversation regardless of how old
    the last message is. Intended for conversation-specific UIs (web UI
    with tabs/dropdown) where each conversation keeps its full history.

    Args:
        exclude_archived: Skip archived conversations (default True).

    Returns:
        Conversation ID.
    """
    with db_connection() as db:
        archive_filter = "WHERE archived = FALSE" if exclude_archived else ""
        row = db.execute(
            f"SELECT id FROM conversations {archive_filter} "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

        if row is None:
            return _create_conversation(db)

        return row["id"]


def get_or_create_conversation() -> int:
    """Get the current active conversation or create a new one.

    Applies a time-based context boundary: if the last message is older
    than context_compaction_hours (default 6), starts a new conversation
    with prior context carried over.

    Intended for single-conversation mediums (Signal, WhatsApp, Telegram)
    where only one conversation stream exists and context management is
    automatic.

    Returns:
        Conversation ID.
    """
    with db_connection() as db:
        # Find the most recent conversation
        row = db.execute(
            "SELECT id, last_message_at FROM conversations "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

        if row is None:
            return _create_conversation(db)

        threshold_hours = config.CONFIG.get("context_compaction_hours", 6)

        if row["last_message_at"] is None:
            # Conversation exists but has no messages — reuse it
            return row["id"]

        last_msg_time = datetime.fromisoformat(row["last_message_at"])
        # Make naive if necessary for comparison
        now = datetime.now(timezone.utc)
        if last_msg_time.tzinfo is None:
            last_msg_time = last_msg_time.replace(tzinfo=timezone.utc)

        if now - last_msg_time > timedelta(hours=threshold_hours):
            # Context boundary — start new conversation
            old_conv_id = row["id"]
            new_conv_id = _create_conversation(db)
            threading.Thread(
                target=generate_summary, args=(old_conv_id,), daemon=True
            ).start()
            return new_conv_id

        return row["id"]


def _create_conversation(db) -> int:
    """Create a new conversation. Returns the conversation ID."""
    cursor = db.execute(
        "INSERT INTO conversations (last_message_at) VALUES (CURRENT_TIMESTAMP)"
    )
    conv_id = cursor.lastrowid
    db.commit()
    return conv_id


def add_message(
    conversation_id: int,
    role: str,
    content: str,
    arc_id: int | None = None,
    content_json: str | None = None,
) -> int:
    """Add a message to a conversation.

    Args:
        conversation_id: The conversation to add to.
        role: Message role ("user", "assistant", "system", "tool_result").
        content: Message text (plain-text summary).
        arc_id: Optional arc this message relates to.
        content_json: Optional JSON-serialized API content blocks.

    Returns:
        Message ID.
    """
    # Sanitize strings to prevent UnicodeEncodeError from surrogate
    # characters that some LLM backends may introduce.
    if isinstance(content, str):
        content = content.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(content_json, str):
        content_json = content_json.encode("utf-8", errors="replace").decode("utf-8")
    with db_transaction() as db:
        cursor = db.execute(
            "INSERT INTO messages (conversation_id, role, content, arc_id, content_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversation_id, role, content, arc_id, content_json),
        )
        msg_id = cursor.lastrowid

        # Update conversation's last_message_at
        db.execute(
            "UPDATE conversations SET last_message_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (conversation_id,),
        )
        return msg_id


def get_messages(conversation_id: int) -> list[dict]:
    """Get all messages in a conversation, ordered chronologically.

    Returns:
        List of message dicts with role, content, arc_id, created_at.
    """
    with db_connection() as db:
        rows = db.execute(
            "SELECT * FROM messages WHERE conversation_id = ? "
            "ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_conversation(conversation_id: int) -> dict | None:
    """Get a conversation by ID."""
    with db_connection() as db:
        row = db.execute(
            "SELECT * FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        return dict(row) if row else None


def get_tail_messages(conversation_id: int, count: int = 10) -> list[dict]:
    """Get the last N messages from a conversation.

    Used for carrying context across conversation boundaries.

    Args:
        conversation_id: The conversation to query.
        count: Number of messages to return (default 10).

    Returns:
        List of message dicts, in chronological order.
    """
    with db_connection() as db:
        rows = db.execute(
            "SELECT * FROM messages WHERE conversation_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (conversation_id, count),
        ).fetchall()
        # Reverse to get chronological order
        return [dict(row) for row in reversed(rows)]


def get_prior_context(current_conversation_id: int, count: int = 10) -> list[dict]:
    """Get tail messages from the previous conversation.

    Used when starting a new conversation after a 6-hour boundary
    to carry over context.

    Args:
        current_conversation_id: The current (new) conversation ID.
        count: Number of messages to carry over.

    Returns:
        List of message dicts from the previous conversation.
    """
    with db_connection() as db:
        # Find the conversation before the current one
        row = db.execute(
            "SELECT id FROM conversations WHERE id < ? "
            "ORDER BY id DESC LIMIT 1",
            (current_conversation_id,),
        ).fetchone()

        if row is None:
            return []

        return get_tail_messages(row["id"], count)


def update_token_count(conversation_id: int, tokens: int):
    """Update the token count for a conversation."""
    with db_transaction() as db:
        db.execute(
            "UPDATE conversations SET context_tokens = ? WHERE id = ?",
            (tokens, conversation_id),
        )


def create_conversation() -> int:
    """Create a new conversation. Always creates a new one (no reuse logic).

    Returns:
        Conversation ID.
    """
    with db_connection() as db:
        return _create_conversation(db)


def list_conversations_with_preview(
    include_archived: bool = False,
    archived_only: bool = False,
    limit: int = 20,
) -> list[dict]:
    """List conversations with a preview of the first user message.

    Args:
        include_archived: Include archived conversations (default False).
        archived_only: Show only archived conversations (default False).
        limit: Max results.

    Returns:
        List of dicts with id, title, started_at, last_message_at,
        archived, preview.
    """
    with db_connection() as db:
        if archived_only:
            archive_filter = "WHERE c.archived = TRUE"
        elif include_archived:
            archive_filter = ""
        else:
            archive_filter = "WHERE c.archived = FALSE"
        rows = db.execute(
            f"SELECT c.id, c.title, c.started_at, c.last_message_at, c.archived, "
            f"(SELECT SUBSTR(m.content, 1, 60) FROM messages m "
            f" WHERE m.conversation_id = c.id AND m.role = 'user' "
            f" ORDER BY m.id ASC LIMIT 1) AS preview "
            f"FROM conversations c {archive_filter} "
            f"ORDER BY c.last_message_at DESC NULLS LAST, c.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def archive_conversation(conversation_id: int):
    """Archive a conversation (hide from default listing)."""
    with db_transaction() as db:
        db.execute(
            "UPDATE conversations SET archived = TRUE WHERE id = ?",
            (conversation_id,),
        )


def unarchive_conversation(conversation_id: int):
    """Unarchive a conversation (restore to default listing)."""
    with db_transaction() as db:
        db.execute(
            "UPDATE conversations SET archived = FALSE WHERE id = ?",
            (conversation_id,),
        )


def archive_conversations_batch(conversation_ids: list[int]) -> int:
    """Archive multiple conversations in a single UPDATE.

    Args:
        conversation_ids: List of conversation IDs to archive.

    Returns:
        Number of conversations actually archived (skips already-archived).
    """
    if not conversation_ids:
        return 0
    with db_transaction() as db:
        placeholders = ",".join("?" for _ in conversation_ids)
        cursor = db.execute(
            f"UPDATE conversations SET archived = TRUE "
            f"WHERE id IN ({placeholders}) AND archived = FALSE",
            conversation_ids,
        )
        return cursor.rowcount


def archive_all_conversations(exclude_ids: list[int] | None = None) -> int:
    """Archive all non-archived conversations.

    Args:
        exclude_ids: Optional list of conversation IDs to keep unarchived.

    Returns:
        Number of conversations archived.
    """
    with db_transaction() as db:
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            cursor = db.execute(
                f"UPDATE conversations SET archived = TRUE "
                f"WHERE archived = FALSE AND id NOT IN ({placeholders})",
                exclude_ids,
            )
        else:
            cursor = db.execute(
                "UPDATE conversations SET archived = TRUE WHERE archived = FALSE"
            )
        return cursor.rowcount


def set_conversation_title(conversation_id: int, title: str):
    """Set or update a conversation's title."""
    with db_transaction() as db:
        db.execute(
            "UPDATE conversations SET title = ? WHERE id = ?",
            (title, conversation_id),
        )


def link_arc_to_conversation(conversation_id: int, arc_id: int):
    """Link an arc to a conversation. Idempotent (INSERT OR IGNORE)."""
    with db_transaction() as db:
        db.execute(
            "INSERT OR IGNORE INTO conversation_arcs (conversation_id, arc_id) "
            "VALUES (?, ?)",
            (conversation_id, arc_id),
        )


def get_conversation_arc_ids(conversation_id: int) -> list[int]:
    """Get arc IDs linked to a conversation."""
    with db_connection() as db:
        rows = db.execute(
            "SELECT arc_id FROM conversation_arcs WHERE conversation_id = ? "
            "ORDER BY created_at ASC",
            (conversation_id,),
        ).fetchall()
        return [r["arc_id"] for r in rows]


def generate_title(conversation_id: int):
    """Generate a title for a conversation using the cheapest available model.

    Reads the first 3 user messages, calls the AI to summarize in ~5 words,
    and stores the result. Safe to call in a background thread.
    """
    try:
        with db_connection() as db:
            rows = db.execute(
                "SELECT content FROM messages "
                "WHERE conversation_id = ? AND role = 'user' "
                "ORDER BY id ASC LIMIT 3",
                (conversation_id,),
            ).fetchall()

        if not rows:
            return

        user_text = "\n".join(r["content"][:200] for r in rows)
        prompt = (
            "Summarize this conversation topic in 5 words or fewer. "
            "Return ONLY the title, no quotes or punctuation.\n\n"
            f"{user_text}"
        )

        from . import model_resolver
        model_str = model_resolver.get_model_for_role("title")
        client_mod = model_resolver.create_client_for_model(model_str)
        _, bare_model = model_resolver.parse_model_string(model_str)

        resp = client_mod.call(
            "You generate short conversation titles.",
            [{"role": "user", "content": prompt}],
            model=bare_model,
            max_tokens=30,
            temperature=0.3,
        )
        title = client_mod.extract_text(resp).strip()

        if title:
            # Truncate to reasonable length
            title = title[:80]
            set_conversation_title(conversation_id, title)
            logger.info("Generated title for conversation %d: %s", conversation_id, title)
    except Exception:  # broad catch: AI call for title generation
        logger.exception("Failed to generate title for conversation %d", conversation_id)


def set_conversation_summary(conversation_id: int, summary: str):
    """Set or update a conversation's summary."""
    with db_transaction() as db:
        db.execute(
            "UPDATE conversations SET summary = ? WHERE id = ?",
            (summary, conversation_id),
        )


def get_conversation_summary(conversation_id: int) -> str | None:
    """Get a conversation's summary."""
    with db_connection() as db:
        row = db.execute(
            "SELECT summary FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        return row["summary"] if row else None


def generate_summary(conversation_id: int):
    """Generate a structured summary for a conversation using the cheapest available model.

    Reads all messages, truncates to ~6000 chars, calls the AI to produce a
    structured summary, and stores the result. Safe to call in a background thread.
    """
    try:
        with db_connection() as db:
            rows = db.execute(
                "SELECT role, content FROM messages "
                "WHERE conversation_id = ? ORDER BY id ASC",
                (conversation_id,),
            ).fetchall()

        if not rows:
            return

        # Build message text, truncating to ~6000 chars total
        parts = []
        total = 0
        for r in rows:
            line = f"{r['role']}: {r['content']}"
            summary_max = config.get_config("conversation_summary_max_length", 6000)
            if total + len(line) > summary_max:
                remaining = summary_max - total
                if remaining > config.get_config("conversation_summary_min_remaining", 50):
                    parts.append(line[:remaining] + "...")
                break
            parts.append(line)
            total += len(line)

        conversation_text = "\n".join(parts)
        prompt = (
            "Summarize this conversation. Include these sections:\n"
            "- Topics Discussed\n"
            "- Key Decisions\n"
            "- User Preferences Noted\n"
            "- Pending/Unfinished Items\n\n"
            "Be concise. Use bullet points.\n\n"
            f"{conversation_text}"
        )

        from . import model_resolver
        model_str = model_resolver.get_model_for_role("summary")
        client_mod = model_resolver.create_client_for_model(model_str)
        _, bare_model = model_resolver.parse_model_string(model_str)

        resp = client_mod.call(
            "You generate structured conversation summaries.",
            [{"role": "user", "content": prompt}],
            model=bare_model,
            max_tokens=500,
            temperature=0.3,
        )
        summary = client_mod.extract_text(resp).strip()

        if summary:
            summary = summary[:5000]
            set_conversation_summary(conversation_id, summary)
            logger.info("Generated summary for conversation %d", conversation_id)
            # Enqueue KB entry creation
            try:
                from ..core.engine import work_queue
                work_queue.enqueue(
                    "kb.conversation_summary",
                    {"conversation_id": conversation_id},
                    idempotency_key=f"conv-kb-{conversation_id}",
                )
            except (ImportError, sqlite3.Error) as _exc:
                logger.debug("Could not enqueue conversation KB entry", exc_info=True)
    except Exception:  # broad catch: AI call for summary generation
        logger.exception("Failed to generate summary for conversation %d", conversation_id)


def get_previous_conversation_id(current_id: int) -> int | None:
    """Get the ID of the conversation before the current one."""
    with db_connection() as db:
        row = db.execute(
            "SELECT id FROM conversations WHERE id < ? "
            "ORDER BY id DESC LIMIT 1",
            (current_id,),
        ).fetchone()
        return row["id"] if row else None


def get_recent_conversations(limit: int = 3) -> list[dict]:
    """Get recent conversations with titles and summaries for memory hints.

    Returns conversations that have either a title or summary, ordered by
    most recent first.
    """
    with db_connection() as db:
        rows = db.execute(
            "SELECT id, title, summary, last_message_at FROM conversations "
            "WHERE (title IS NOT NULL OR summary IS NOT NULL) "
            "AND archived = FALSE "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]



def format_messages_for_api(messages: list[dict]) -> list[dict]:
    """Format database message records for the AI provider API.

    Handles structured content (content_json) for tool_use exchanges.
    Maps tool_result role to user role for the API.
    Converts system messages to user-role messages with a prefix.
    Merges consecutive same-role messages (required by the messages API).

    Returns:
        List of dicts with 'role' and 'content' keys.
    """
    api_messages = []
    for msg in messages:
        role = msg["role"]
        if role == "system":
            api_messages.append({
                "role": "user",
                "content": f"[System notification: {msg['content']}]",
            })
            continue

        content_json = msg.get("content_json")
        if content_json:
            try:
                parsed = json.loads(content_json)
            except (json.JSONDecodeError, TypeError):
                parsed = None

            if parsed is not None:
                role_map = {"tool_result": "user", "tool_call": "assistant"}
                api_role = role_map.get(role, role)
                api_messages.append({
                    "role": api_role,
                    "content": parsed,
                })
                continue

        if role in ("user", "assistant"):
            api_messages.append({
                "role": role,
                "content": msg["content"],
            })
    return _merge_consecutive_roles(api_messages)


def _merge_consecutive_roles(api_messages: list[dict]) -> list[dict]:
    """Merge consecutive messages with the same role.

    The messages API requires strictly alternating user/assistant roles,
    and every tool_use block must be immediately followed by a user message
    containing the corresponding tool_result — no intervening messages allowed.

    Merges adjacent same-role messages using these strategies:
    - str + str: newline-join into a single string
    - list + str: append a text block to the existing list
    - str + list: prepend a text block to the incoming list (handles the case
      where a system notification appears between a tool_use and its tool_result)
    - list + list: kept separate (should not occur in practice)
    """
    if not api_messages:
        return api_messages

    merged = [api_messages[0]]
    for msg in api_messages[1:]:
        prev = merged[-1]
        if msg["role"] != prev["role"]:
            merged.append(msg)
            continue

        prev_content = prev["content"]
        msg_content = msg["content"]

        if isinstance(prev_content, str) and isinstance(msg_content, str):
            # str + str: newline join
            prev["content"] = prev_content + "\n" + msg_content
        elif isinstance(prev_content, list) and isinstance(msg_content, str):
            # list + str: append text block to existing list
            prev["content"] = list(prev_content) + [{"type": "text", "text": msg_content}]
        elif isinstance(prev_content, str) and isinstance(msg_content, list):
            # str + list: drop the preceding string and use only the list content.
            # This is critical when a system notification (user/str) appears between an
            # assistant tool_use and its tool_result (user/list): the API requires the
            # tool_result to be in the message immediately following the tool_use, and
            # mixing text + tool_result blocks in the same user message is not supported.
            # The system notification is advisory; the AI receives the essential outcome
            # via the tool_result block.
            merged[-1] = {
                "role": msg["role"],
                "content": list(msg_content),
            }
        else:
            # list + list: keep separate
            merged.append(msg)
    return merged
