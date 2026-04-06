"""Auto-action processing for reflection proposed actions.

After a reflection completes, examines proposed_actions and submits
workable changes through the existing review pipelines.

Actions are classified into types:
- kb: knowledge base creation/update via kb_add/kb_edit
- code: code changes via submit_code
- config: config suggestions (recorded but not auto-applied)
- other: anything else (recorded but not auto-applied)

Taint-aware: reflections from tainted conversations use stricter review.
Rate-limited: configurable per-reflection and per-day caps.
"""

import json
import logging
from datetime import datetime, timezone, timedelta

from .. import config
from ..db import get_db, db_connection, db_transaction
from ..prompts import load_prompt_template

logger = logging.getLogger(__name__)

# Action types that can be auto-submitted
_ACTIONABLE_TYPES = {"kb", "code"}

# Keywords used for classification heuristics
_KB_KEYWORDS = [
    "knowledge base", "kb entry", "update kb", "create kb entry",
    "modify knowledge", "new kb entry", "SKILL.md", "skill",
]
_CODE_KEYWORDS = [
    "code", "implement", "fix bug", "refactor", "write code",
    "modify code", "update code", "patch", "function", "module",
]
_CONFIG_KEYWORDS = [
    "config", "configuration", "setting", "parameter",
    "enable", "disable", "threshold", "limit",
]


def classify_action(description: str) -> str:
    """Classify an action description into a type.

    Args:
        description: The action description text.

    Returns:
        One of: 'kb', 'code', 'config', 'other'
    """
    lower = description.lower()

    for kw in _KB_KEYWORDS:
        if kw in lower:
            return "kb"

    for kw in _CODE_KEYWORDS:
        if kw in lower:
            return "code"

    for kw in _CONFIG_KEYWORDS:
        if kw in lower:
            return "config"

    return "other"


def _parse_proposed_actions(proposed_actions: str | None) -> list[str]:
    """Parse proposed actions from reflection output.

    Handles both JSON list format and plain text (line-separated) format.

    Args:
        proposed_actions: Raw proposed_actions text from the reflection.

    Returns:
        List of action description strings.
    """
    if not proposed_actions or not proposed_actions.strip():
        return []

    # Try JSON first
    try:
        parsed = json.loads(proposed_actions)
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item]
        if isinstance(parsed, str):
            return [parsed]
    except (json.JSONDecodeError, TypeError):
        pass

    # Fall back to line-separated text
    actions = []
    for line in proposed_actions.strip().split("\n"):
        line = line.strip()
        # Strip common list prefixes
        if line.startswith("- "):
            line = line[2:]
        elif line.startswith("* "):
            line = line[2:]
        elif len(line) > 2 and line[0].isdigit() and line[1] in (".", ")"):
            line = line[2:].strip()
        elif len(line) > 3 and line[:2].isdigit() and line[2] in (".", ")"):
            line = line[3:].strip()

        if line:
            actions.append(line)

    return actions


def _get_daily_action_count() -> int:
    """Count reflection_actions created today."""
    # Use strftime format matching SQLite's CURRENT_TIMESTAMP format
    # (YYYY-MM-DD HH:MM:SS without timezone or T separator)
    today_start = datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00:00")

    with db_connection() as db:
        row = db.execute(
            "SELECT COUNT(*) as cnt FROM reflection_actions "
            "WHERE created_at >= ?",
            (today_start,),
        ).fetchone()
        return row["cnt"] if row else 0


def _check_reflection_tainted(reflection_id: int) -> bool:
    """Check if the reflection's conversation was tainted.

    Looks up the conversation created for the reflection by matching
    the reflection's time window against conversation titles that contain
    '[Reflection]'. Then checks conversation_taint for that conversation.

    Args:
        reflection_id: The reflection to check.

    Returns:
        True if the reflection's conversation was tainted.
    """
    with db_connection() as db:
        # Get the reflection record
        refl = db.execute(
            "SELECT * FROM reflections WHERE id = ?",
            (reflection_id,),
        ).fetchone()
        if not refl:
            return False

        # Find the conversation created for this reflection.
        # Reflection conversations have titles like "[Daily Reflection] 2026-03-16"
        # and are created around the reflection's period_end time.
        rows = db.execute(
            "SELECT c.id FROM conversations c "
            "WHERE c.title LIKE '%Reflection%' "
            "ORDER BY c.id DESC LIMIT 10",
        ).fetchall()

        for row in rows:
            taint_row = db.execute(
                "SELECT 1 FROM conversation_taint WHERE conversation_id = ? LIMIT 1",
                (row["id"],),
            ).fetchone()
            if taint_row is not None:
                return True

        return False


def _create_action_record(
    reflection_id: int,
    action_type: str,
    description: str,
    review_mode: str,
    status: str = "pending",
    outcome: str | None = None,
) -> int:
    """Create a reflection_actions record.

    Returns the action ID.
    """
    with db_transaction() as db:
        cursor = db.execute(
            "INSERT INTO reflection_actions "
            "(reflection_id, action_type, action_description, status, review_mode, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (reflection_id, action_type, description, status, review_mode, outcome),
        )
        action_id = cursor.lastrowid
        return action_id


def _update_action_status(
    action_id: int,
    status: str,
    outcome: str | None = None,
) -> None:
    """Update a reflection_actions record status and outcome."""
    with db_transaction() as db:
        completed_at = None
        if status in ("approved", "rejected", "skipped"):
            completed_at = datetime.now(timezone.utc).isoformat()

        db.execute(
            "UPDATE reflection_actions SET status = ?, outcome = ?, completed_at = ? "
            "WHERE id = ?",
            (status, outcome, completed_at, action_id),
        )


def _submit_kb_action(description: str, review_mode: str) -> dict:
    """Submit a KB action via invoke_for_chat.

    Args:
        description: What KB entry to create/update.
        review_mode: 'auto', 'human', or 'none'.

    Returns:
        Dict with 'success' bool and 'detail' string.
    """
    from . import invocation, conversation as conv_module

    conv_id = conv_module.create_conversation()
    conv_module.set_conversation_title(conv_id, f"[Reflection Action] KB: {description[:50]}")

    try:
        prompt = load_prompt_template(
            "action-kb",
            context={"description": description},
            subdirectory="reflections",
        )
    except FileNotFoundError:
        prompt = (
            f"You are processing a reflection-proposed action. "
            f"Create or update a knowledge base entry as described:\n\n"
            f"{description}\n\n"
            f"Use submit_code with carpenter_tools.act.kb.add() or "
            f"carpenter_tools.act.kb.edit() to add/update a KB entry under the "
            f"skills/ path. Be concise and focused on the specific action."
        )

    try:
        result = invocation.invoke_for_chat(
            prompt,
            conversation_id=conv_id,
            _system_triggered=True,
        )
        response = result.get("response_text", "")
        conv_module.archive_conversation(conv_id)
        return {"success": True, "detail": response[:200]}
    except Exception as e:  # broad catch: AI invocation may raise anything
        logger.exception("KB action failed: %s", description[:100])
        conv_module.archive_conversation(conv_id)
        return {"success": False, "detail": str(e)[:200]}


def _submit_code_action(description: str, review_mode: str) -> dict:
    """Submit a code action via invoke_for_chat.

    Args:
        description: What code change to implement.
        review_mode: 'auto', 'human', or 'none'.

    Returns:
        Dict with 'success' bool and 'detail' string.
    """
    from . import invocation, conversation as conv_module

    conv_id = conv_module.create_conversation()
    conv_module.set_conversation_title(conv_id, f"[Reflection Action] Code: {description[:50]}")

    try:
        prompt = load_prompt_template(
            "action-code",
            context={"description": description},
            subdirectory="reflections",
        )
    except FileNotFoundError:
        prompt = (
            f"You are processing a reflection-proposed action. "
            f"Implement the following code change:\n\n"
            f"{description}\n\n"
            f"Use submit_code to implement this. "
            f"Be concise and focused on the specific action."
        )

    try:
        result = invocation.invoke_for_chat(
            prompt,
            conversation_id=conv_id,
            _system_triggered=True,
        )
        response = result.get("response_text", "")
        conv_module.archive_conversation(conv_id)
        return {"success": True, "detail": response[:200]}
    except Exception as e:  # broad catch: AI invocation may raise anything
        logger.exception("Code action failed: %s", description[:100])
        conv_module.archive_conversation(conv_id)
        return {"success": False, "detail": str(e)[:200]}


def process_reflection_actions(reflection_id: int) -> dict:
    """Process proposed actions from a completed reflection.

    This is the main entry point. Called from the reflection template handler
    after a reflection is saved.

    Flow:
    1. Check if auto_action is enabled
    2. Load the reflection and parse proposed_actions
    3. Check daily rate limit
    4. Determine review mode (taint-aware)
    5. Process each action (up to per-reflection cap)
    6. Send batch notification

    Args:
        reflection_id: ID of the just-completed reflection.

    Returns:
        Dict summarizing results: {submitted, approved, rejected, skipped, errors}
    """
    reflection_config = config.CONFIG.get("reflection", {})

    # Check if auto_action is enabled
    if not reflection_config.get("auto_action", False):
        logger.debug("Reflection auto-action disabled, skipping")
        return {"submitted": 0, "approved": 0, "rejected": 0, "skipped": 0, "errors": 0}

    # Load the reflection
    with db_connection() as db:
        refl = db.execute(
            "SELECT * FROM reflections WHERE id = ?",
            (reflection_id,),
        ).fetchone()

    if not refl:
        logger.warning("Reflection %d not found", reflection_id)
        return {"submitted": 0, "approved": 0, "rejected": 0, "skipped": 0, "errors": 0}

    # Parse proposed actions
    actions = _parse_proposed_actions(refl["proposed_actions"])
    if not actions:
        logger.debug("No proposed actions in reflection %d", reflection_id)
        return {"submitted": 0, "approved": 0, "rejected": 0, "skipped": 0, "errors": 0}

    # Check daily rate limit
    max_per_day = reflection_config.get("max_actions_per_day", 50)
    daily_count = _get_daily_action_count()
    if daily_count >= max_per_day:
        logger.info(
            "Daily action limit reached (%d/%d), skipping reflection %d actions",
            daily_count, max_per_day, reflection_id,
        )
        return {"submitted": 0, "approved": 0, "rejected": 0, "skipped": 0, "errors": 0}

    # Determine review mode based on taint
    is_tainted = _check_reflection_tainted(reflection_id)
    if is_tainted:
        review_mode = reflection_config.get("tainted_review_mode", "human")
    else:
        review_mode = reflection_config.get("review_mode", "auto")

    # Process each action (up to per-reflection cap)
    max_per_reflection = reflection_config.get("max_actions_per_reflection", 10)
    remaining_daily = max_per_day - daily_count

    results = {"submitted": 0, "approved": 0, "rejected": 0, "skipped": 0, "errors": 0}
    action_summaries = []

    for i, action_desc in enumerate(actions):
        if i >= max_per_reflection:
            break
        if results["submitted"] + results["skipped"] >= remaining_daily:
            logger.info("Daily action limit reached mid-processing")
            break

        action_type = classify_action(action_desc)

        if action_type in _ACTIONABLE_TYPES:
            # Create a pending record
            action_id = _create_action_record(
                reflection_id, action_type, action_desc, review_mode,
            )
            results["submitted"] += 1

            # Execute the action
            try:
                if action_type == "kb":
                    result = _submit_kb_action(action_desc, review_mode)
                else:  # code
                    result = _submit_code_action(action_desc, review_mode)

                if result["success"]:
                    _update_action_status(action_id, "approved", result["detail"])
                    results["approved"] += 1
                    action_summaries.append(f"[{action_type}] DONE: {action_desc[:60]}")
                else:
                    _update_action_status(action_id, "rejected", result["detail"])
                    results["rejected"] += 1
                    action_summaries.append(f"[{action_type}] FAILED: {action_desc[:60]}")

            except Exception as e:  # broad catch: action execution may raise anything
                logger.exception("Error processing action: %s", action_desc[:100])
                _update_action_status(action_id, "rejected", str(e)[:200])
                results["errors"] += 1
                action_summaries.append(f"[{action_type}] ERROR: {action_desc[:60]}")
        else:
            # Config suggestions and other items are recorded as skipped
            _create_action_record(
                reflection_id, action_type, action_desc, review_mode,
                status="skipped",
                outcome=f"Suggestion: {action_desc}",
            )
            results["skipped"] += 1
            action_summaries.append(f"[{action_type}] SKIPPED: {action_desc[:60]}")

    # Send batch notification
    if action_summaries:
        _send_batch_notification(results, action_summaries, is_tainted)

    return results


def _send_batch_notification(
    results: dict,
    action_summaries: list[str],
    is_tainted: bool,
) -> None:
    """Send a single notification summarizing all processed actions.

    Args:
        results: Dict with submitted/approved/rejected/skipped/errors counts.
        action_summaries: List of one-line summaries for each action.
        is_tainted: Whether the reflection was tainted.
    """
    from ..core.notifications import notify

    parts = ["Reflection auto-actions processed:"]
    parts.append(
        f"  Submitted: {results['submitted']}, "
        f"Approved: {results['approved']}, "
        f"Rejected: {results['rejected']}, "
        f"Skipped: {results['skipped']}, "
        f"Errors: {results['errors']}"
    )

    if is_tainted:
        parts.append("  (from tainted reflection - stricter review applied)")

    parts.append("")
    for summary in action_summaries:
        parts.append(f"  {summary}")

    message = "\n".join(parts)
    notify(message, priority="low", category="reflection_actions")


def get_reflection_actions(
    reflection_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Retrieve reflection actions from the database.

    Args:
        reflection_id: Filter by reflection ID. None = all.
        status: Filter by status. None = all.
        limit: Maximum number of results.

    Returns:
        List of action dicts, most recent first.
    """
    with db_connection() as db:
        conditions = []
        params = []

        if reflection_id is not None:
            conditions.append("reflection_id = ?")
            params.append(reflection_id)
        if status is not None:
            conditions.append("status = ?")
            params.append(status)

        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)

        rows = db.execute(
            f"SELECT * FROM reflection_actions WHERE {where} "
            f"ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
