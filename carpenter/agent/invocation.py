"""Agent invocation loop for Carpenter.

Prompt → AI response → extract code → save file → execute → retry.

Two types of retry:
- Mechanical retry (transient failures): up to mechanical_retry_max attempts
- Agentic iteration (code fix loop): up to agentic_iteration_budget rounds

Chat mode supports tool_use: the agent can use platform tools (files, state,
arc management, coding-change arcs) during conversation.
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..core import code_manager
from ..db import get_db, db_connection, db_transaction
from . import templates, conversation, model_resolver, api_standard, error_classifier
from .providers import anthropic as claude_client, ollama as ollama_client, tinfoil as tinfoil_client, local as local_client

logger = logging.getLogger(__name__)

# Registry of tool handlers added by platform packages.
# Checked before the built-in if/elif chain in _execute_chat_tool().
_extra_tool_handlers: dict[str, object] = {}

# Tools that start async background work (results arrive via arc.chat_notify).
# When ALL tools in a turn are async AND the model already produced visible text
# alongside the tool call, skip the post-tool API call to avoid a redundant
# "I'm fetching that now..." acknowledgment message.
_ASYNC_TOOLS = frozenset({"fetch_web_content"})


def register_tool_handler(name: str, handler) -> None:
    """Register a tool handler from a platform package.

    Args:
        name: Tool name (must match a loaded chat tool).
        handler: Callable(tool_input, **kwargs) -> str. Called with the same
                 keyword arguments as _execute_chat_tool() (conversation_id,
                 executor_arc_id, executor_conv_id).

    Raises:
        ValueError: If the tool has a ``platform`` trust boundary.
    """
    from ..chat_tool_registry import PLATFORM_TOOLS
    if name in PLATFORM_TOOLS:
        raise ValueError(
            f"Cannot override platform tool '{name}' via register_tool_handler()"
        )
    logger.info("Registered tool handler: %s", name)
    _extra_tool_handlers[name] = handler


# ISO 639-1 language code → name mapping for chat_language config directive.
_ISO_639_1_LANGUAGES: dict[str, str] = {
    "af": "Afrikaans", "ar": "Arabic", "bg": "Bulgarian", "bn": "Bengali",
    "ca": "Catalan", "cs": "Czech", "cy": "Welsh", "da": "Danish",
    "de": "German", "el": "Greek", "en": "English", "es": "Spanish",
    "et": "Estonian", "fa": "Persian", "fi": "Finnish", "fr": "French",
    "ga": "Irish", "gl": "Galician", "gu": "Gujarati", "he": "Hebrew",
    "hi": "Hindi", "hr": "Croatian", "hu": "Hungarian", "hy": "Armenian",
    "id": "Indonesian", "is": "Icelandic", "it": "Italian", "ja": "Japanese",
    "ka": "Georgian", "kn": "Kannada", "ko": "Korean", "lt": "Lithuanian",
    "lv": "Latvian", "mk": "Macedonian", "ml": "Malayalam", "mr": "Marathi",
    "ms": "Malay", "mt": "Maltese", "nl": "Dutch", "no": "Norwegian",
    "pa": "Punjabi", "pl": "Polish", "pt": "Portuguese", "ro": "Romanian",
    "ru": "Russian", "sk": "Slovak", "sl": "Slovenian", "sq": "Albanian",
    "sr": "Serbian", "sv": "Swedish", "sw": "Swahili", "ta": "Tamil",
    "te": "Telugu", "th": "Thai", "tl": "Filipino", "tr": "Turkish",
    "uk": "Ukrainian", "ur": "Urdu", "vi": "Vietnamese", "zh": "Chinese",
}

_DEFAULT_CONTEXT_WINDOW = 200000


def _extract_last_user_text(messages: list[dict]) -> str:
    """Extract the text content of the last user message."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "").strip()
                        if text:
                            return text
    return ""


def _auto_search_for_prompt(messages: list[dict] | None = None) -> str:
    """Run KB search on user messages and return results for the system prompt.

    If there's only one user message, returns one section.
    If there are 2+ user messages, returns two sections: one for the latest
    message, one for the combination of all user messages.

    Heuristic: if any user message contains an http(s) URL, the
    ``web/trust-warning`` KB entry is force-included in the results so the
    agent sees the untrusted-arc-batch pattern even when the search backend
    doesn't rank it highly enough.
    """
    import re as _re

    if not messages:
        return ""

    kb_config = config.CONFIG.get("kb", {})
    if not kb_config.get("enabled", True):
        return ""

    try:
        from ..kb import get_store
        store = get_store()
    except (ImportError, OSError, ValueError) as _exc:
        return ""

    # Extract user messages
    user_texts = []
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                user_texts.append(content.strip())

    if not user_texts:
        return ""

    parts = []

    try:
        # Always search for the most recent user message
        latest_text = user_texts[-1]
        latest_results = store.search(latest_text, max_results=3)
        if latest_results:
            lines = ["## Results for kb.search() for the received chat message:"]
            for r in latest_results:
                lines.append(f"- [[{r['path']}]] — {r['description']}")
            parts.append("\n".join(lines))

        # If 2+ user messages, also search the combination
        if len(user_texts) >= 2:
            combined = " ".join(user_texts)
            combined_results = store.search(combined, max_results=3)
            if combined_results:
                lines = ["## Results for kb.search() for the combination of all user messages in this conversation:"]
                for r in combined_results:
                    lines.append(f"- [[{r['path']}]] — {r['description']}")
                parts.append("\n".join(lines))
    except Exception as _exc:
        logger.debug("KB search failed (search backend may be unavailable): %s", _exc)
        return ""

    # Heuristic: if any user message contains an http(s) URL, force-include
    # the web/trust-warning KB entry so the agent knows the untrusted arc
    # batch pattern before attempting web access.
    all_user_text = " ".join(user_texts)
    if _re.search(r"https?://", all_user_text):
        # Check whether web/trust-warning is already in the results
        all_paths = set()
        if latest_results:
            all_paths.update(r["path"] for r in latest_results)
        if len(user_texts) >= 2 and combined_results:
            all_paths.update(r["path"] for r in combined_results)

        if "web/trust-warning" not in all_paths:
            try:
                tw_entry = store.get_entry("web/trust-warning")
                if tw_entry:
                    parts.append(
                        "## Auto-included (URL detected in message):\n"
                        f"- [[web/trust-warning]] — {tw_entry['description']}"
                    )
            except (ImportError, KeyError, ValueError) as _exc:
                pass  # Non-critical; don't break the prompt

    return "\n\n".join(parts)


def _select_chat_tools(context_budget: int | None = None) -> list[dict]:
    """Select chat tools based on context budget and usage frequency.

    Tool definitions come from user-configurable Python modules loaded
    by chat_tool_loader.

    Always-available tools are always present. Other tools are sorted by
    usage frequency and included until budget is exhausted.

    Returns the selected tool definitions.
    """
    if context_budget is None:
        context_budget = _DEFAULT_CONTEXT_WINDOW

    from ..chat_tool_loader import get_tool_defs_for_api, get_always_available_names

    tool_defs = get_tool_defs_for_api()
    registry_core = get_always_available_names()

    # For small context windows, use a minimal core set (5 tools) to leave
    # room for messages.  Larger contexts keep the full 10-tool core.
    # Both sets are configurable via tool_lists.ultra_core_tools_add/remove
    # and tool_lists.core_tools_add/remove in config.yaml.
    _DEFAULT_ULTRA_CORE = {"read_file", "list_files", "get_state", "kb_search", "submit_code"}
    _DEFAULT_CORE = {
        "read_file", "list_files", "get_state", "submit_code",
        "list_arcs", "get_arc_detail",
        "kb_describe", "kb_search", "kb_links_in",
    }
    tl = config.CONFIG.get("tool_lists", {})
    if isinstance(tl, dict):
        ultra_add = tl.get("ultra_core_tools_add", [])
        ultra_remove = tl.get("ultra_core_tools_remove", [])
        core_add = tl.get("core_tools_add", [])
        core_remove = tl.get("core_tools_remove", [])
    else:
        ultra_add = ultra_remove = core_add = core_remove = []
    _ULTRA_CORE = (set(_DEFAULT_ULTRA_CORE) | set(ultra_add)) - set(ultra_remove)
    _FULL_CORE = (set(_DEFAULT_CORE) | set(core_add)) - set(core_remove)

    if context_budget <= 16384:
        _CORE_TOOLS = registry_core.intersection(_ULTRA_CORE)
    else:
        _CORE_TOOLS = registry_core

    total_count = len(tool_defs)

    # Token budget for tool definitions: 10% of context, max 5000 tokens
    tool_budget_tokens = min(int(context_budget * 0.10), 5000)
    # Tool defs average ~120-150 tokens each; use a conservative estimate
    # for small contexts and a tighter one for large contexts.
    tokens_per_tool = 150 if context_budget <= 16384 else 80
    max_tools = max(len(_CORE_TOOLS), tool_budget_tokens // tokens_per_tool)

    if max_tools >= total_count:
        # All tools fit
        return tool_defs

    # Separate core and non-core
    core_tools = [t for t in tool_defs if t["name"] in _CORE_TOOLS]
    non_core_tools = [t for t in tool_defs if t["name"] not in _CORE_TOOLS]

    # Sort non-core by usage frequency (from tool_calls table, 30-day window)
    try:
        with db_connection() as db:
            rows = db.execute(
                "SELECT tool_name, COUNT(*) as cnt FROM tool_calls "
                "WHERE created_at > datetime('now', '-30 days') "
                "GROUP BY tool_name ORDER BY cnt DESC"
            ).fetchall()
            freq = {row["tool_name"]: row["cnt"] for row in rows}
        non_core_tools.sort(key=lambda t: freq.get(t["name"], 0), reverse=True)
    except (sqlite3.Error, KeyError, ValueError) as _exc:
        pass  # Use default order

    remaining_slots = max_tools - len(core_tools)
    selected_non_core = non_core_tools[:remaining_slots] if remaining_slots > 0 else []

    return core_tools + selected_non_core


def _load_prompt_parts_from_templates(
    compact: bool,
    model_name: str | None = None,
    is_arc_step: bool = False,
) -> list[str]:
    """Load prompt sections from user-editable template files in prompts_dir.

    Returns list of content strings.

    Args:
        compact: True for small-context models (filters to compact sections).
        model_name: The model ID being used for this invocation.
        is_arc_step: True when building prompt for an arc step agent.

    Raises:
        RuntimeError: If prompt templates directory is missing or empty.
            The coordinator must install config_seed/prompts/ at startup.
    """
    prompts_dir = config.CONFIG.get("prompts_dir", "")
    if not prompts_dir:
        base_dir = config.CONFIG.get("base_dir", "")
        if base_dir:
            prompts_dir = os.path.join(base_dir, "config", "prompts")
    if not prompts_dir or not os.path.isdir(prompts_dir):
        raise RuntimeError(
            f"Prompt templates directory not found: {prompts_dir!r}. "
            f"The coordinator must call install_prompt_defaults() at startup."
        )

    from ..prompts import load_prompt_sections, render_prompt_sections
    sections = load_prompt_sections(prompts_dir)
    if not sections:
        raise RuntimeError(
            f"No prompt sections found in {prompts_dir!r}. "
            f"The coordinator must call install_prompt_defaults() at startup."
        )

    # Build template context
    context = {
        "model_name": model_name or "",
    }
    sections = render_prompt_sections(sections, context)

    # Filter by compact flag
    if compact:
        filtered = [s for s in sections if s.compact]
    else:
        filtered = list(sections)

    # Override identity with model name if available
    if model_name:
        for i, s in enumerate(filtered):
            if s.name == "identity":
                filtered[i] = type(s)(
                    name=s.name,
                    content=(
                        f"You are Carpenter (model: {model_name}), "
                        f"an AI agent platform."
                    ),
                    compact=s.compact,
                    order=s.order,
                )
                break

    # Drop sections that rendered to empty content (e.g. conditional templates)
    parts = [s.content for s in filtered if s.content.strip()]
    if not parts:
        raise RuntimeError(
            f"Prompt templates in {prompts_dir!r} produced no content. "
            f"Check that template files exist and have content."
        )
    return parts


def _build_chat_system_prompt(
    context_budget: int | None = None,
    model_name: str | None = None,
    messages: list[dict] | None = None,
    is_arc_step: bool = False,
) -> str:
    """Build the chat system prompt from composable sections + KB.

    Includes:
    - Static prompt sections (identity, security, etc.)
    - KB navigation guide
    - KB root index (top-level themes)
    - Auto-search results for user messages
    - Recent conversation hints

    When context_budget < 16384 (small local models), uses a compact
    prompt with just identity, security, KB navigation, and tools.

    When is_arc_step is True (arc PLANNER/EXECUTOR/REVIEWER agents),
    skips sections not needed by ephemeral arc conversations: recent
    conversations, KB root index, and auto-search results.

    Args:
        context_budget: Total context window in tokens.
        model_name: The model ID being used for this invocation.
        messages: Conversation messages for auto-search.
        is_arc_step: True when building prompt for an arc step agent.
    """
    if context_budget is None:
        context_budget = _DEFAULT_CONTEXT_WINDOW
    compact = context_budget < 16384

    # Load from user-editable template files (installed by coordinator at startup).
    # Templates handle: identity, security, KB navigation, tools,
    # and KB search few-shot example (compact only).
    parts = _load_prompt_parts_from_templates(compact, model_name, is_arc_step)

    # Dynamic: KB prepopulation for compact mode.
    # Pre-inject search results so small models don't need to call kb_search.
    if compact and messages:
        user_query = _extract_last_user_text(messages)
        if user_query:
            try:
                from ..kb import get_store
                store = get_store()
                results = store.search(user_query, max_results=3)
                if results:
                    lines = ["## Relevant Knowledge"]
                    for r in results:
                        desc = f" — {r.get('title', '')}: {r.get('description', '')}"
                        lines.append(f"- [[{r['path']}]]{desc}")
                    parts.append("\n".join(lines))
            except (ImportError, KeyError, ValueError) as _exc:
                pass

    # Dynamic: KB root index (top-level themes from the KB)
    kb_config = config.CONFIG.get("kb", {})
    if kb_config.get("enabled", True) and not compact and not is_arc_step:
        try:
            from ..kb import get_store
            store = get_store()
            children = store.list_children("")
            if children:
                lines = ["## KB Topics"]
                for child in children:
                    desc = f" — {child['description']}" if child.get("description") else ""
                    lines.append(f"- [[{child['path']}]]{desc}")
                parts.append("\n".join(lines))
        except (ImportError, KeyError, ValueError) as _exc:
            pass

    # Dynamic: auto-search for user messages
    if not compact and not is_arc_step:
        try:
            search_section = _auto_search_for_prompt(messages)
            if search_section:
                parts.append(search_section)
        except (ImportError, KeyError, ValueError) as _exc:
            pass

    # Dynamic: recent conversation hints for memory (skip for arc steps)
    if not is_arc_step:
        try:
            hint_count = config.CONFIG.get("memory_recent_hints", 3)
            recent = conversation.get_recent_conversations(limit=hint_count)
            if recent:
                hint_lines = ["## Recent Conversations"]
                for c in recent:
                    title = c.get("title") or "(untitled)"
                    date = (c.get("last_message_at") or "")[:10]
                    has_summary = "summary available" if c.get("summary") else "no summary"
                    hint_lines.append(f"- conv#{c['id']} [{date}] {title} ({has_summary})")
                parts.append("\n".join(hint_lines))
        except (sqlite3.Error, KeyError, ValueError) as _exc:
            pass

    # Dynamic: tool count indicator
    selected_tools = _select_chat_tools(context_budget)
    from ..chat_tool_loader import get_total_count
    total_tools = get_total_count()
    selected_count = len(selected_tools)
    if selected_count < total_tools:
        parts.append(
            f"(showing {selected_count} of {total_tools} available tools — "
            f"use kb_search to find more capabilities)"
        )
    else:
        parts.append(f"(all {total_tools} tools shown)")

    # Dynamic: current date/time and timezone context
    # The agent needs this to construct correct timestamps for scheduling,
    # date-relative queries, and general time awareness.
    now_local = datetime.now().astimezone()  # local time with tzinfo
    tz_name = now_local.strftime("%Z")       # e.g. "BST", "GMT"
    tz_offset = now_local.strftime("%z")     # e.g. "+0100", "+0000"
    try:
        import zoneinfo
        tz_iana = str(now_local.tzinfo)      # e.g. "Europe/London"
    except ImportError:
        tz_iana = ""
    # Try to get IANA name from /etc/timezone as a more reliable source
    if not tz_iana or tz_iana.startswith("UTC"):
        try:
            with open("/etc/timezone") as f:
                tz_iana = f.read().strip()
        except OSError:
            pass
    tz_display = f"{tz_name} (UTC{tz_offset[:3]}:{tz_offset[3:]})"
    if tz_iana:
        tz_display += f" — IANA: {tz_iana}"
    parts.append(
        f"## Current Date & Time\n\n"
        f"Local time: {now_local.strftime('%Y-%m-%d %H:%M:%S')} {tz_name}\n"
        f"UTC time: {now_local.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        f"Timezone: {tz_display}\n\n"
        f"When scheduling, use local time as a naive ISO timestamp "
        f"(e.g. '{now_local.strftime('%Y-%m-%d')}T14:30:00') — "
        f"the platform converts local time to UTC automatically."
    )

    # Dynamic: language directive
    chat_language = config.CONFIG.get("chat_language", "")
    if chat_language:
        lang_code = chat_language.strip().lower()
        lang_name = _ISO_639_1_LANGUAGES.get(lang_code, lang_code)
        parts.append(
            f"## Language\n\n"
            f"Always respond in {lang_name} (ISO 639-1 code: {lang_code}), "
            f"regardless of the language the user writes in."
        )

    return "\n\n".join(parts)

def _truncate_tool_output(result_text: str, tool_name: str) -> str:
    """Truncate large tool output to avoid flooding the context window.

    If the result exceeds ``tool_output_max_bytes`` (default 32 KB) the full
    output is saved to a date-partitioned file under ``{code_dir}/../tool_output/``
    and a head + tail summary is returned to the agent instead.

    Small outputs are passed through unchanged.
    """
    max_bytes = config.CONFIG.get("tool_output_max_bytes", 32768)
    if len(result_text.encode("utf-8", errors="replace")) <= max_bytes:
        return result_text

    head_lines = config.CONFIG.get("tool_output_head_lines", 50)
    tail_lines = config.CONFIG.get("tool_output_tail_lines", 20)

    # Persist full output to disk
    now = datetime.now(timezone.utc)
    date_dir = now.strftime("%Y/%m/%d")
    # Derive output directory from code_dir's parent (both live under data/)
    code_dir = config.CONFIG.get("code_dir", "")
    base_data_dir = str(Path(code_dir).parent) if code_dir else os.path.expanduser("~/carpenter/data")
    out_dir = os.path.join(base_data_dir, "tool_output", date_dir)
    os.makedirs(out_dir, exist_ok=True)

    timestamp = now.strftime("%H%M%S")
    safe_tool_name = tool_name.replace("/", "_").replace("\\", "_")
    filename = f"{timestamp}_{safe_tool_name}_{os.getpid()}.txt"
    out_path = os.path.join(out_dir, filename)

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(result_text)
    except OSError as e:
        logger.warning("Failed to save truncated tool output to %s: %s", out_path, e)
        # Still truncate even if save fails — the whole point is context protection
        out_path = "(save failed)"

    lines = result_text.splitlines(keepends=True)
    total_lines = len(lines)
    total_bytes = len(result_text.encode("utf-8", errors="replace"))

    head_part = "".join(lines[:head_lines])
    tail_part = "".join(lines[-tail_lines:]) if tail_lines > 0 else ""

    notice = (
        f"\n\n[... truncated — full output saved to {out_path} "
        f"({total_bytes} bytes, {total_lines} lines) — "
        f"use read_file to access ...]\n\n"
    )

    return head_part + notice + tail_part


def _validate_tool_call(
    tool_name: str,
    tool_input: dict,
    available_tools: list[dict],
) -> str | None:
    """Validate a tool call before execution.

    Returns an error message string if the call is invalid, or None if valid.
    This gives small models actionable feedback they can use to retry.
    """
    # Handle malformed JSON parse errors from api_standard
    if "_parse_error" in tool_input:
        return (
            f"Error: Your tool call arguments were not valid JSON. "
            f"Raw text: {tool_input['_parse_error']}. "
            f"Please format as valid JSON, e.g.: "
            f'{tool_name}({{"query": "your search terms"}})'
        )

    # Check tool name exists
    tool_names = {t["name"] for t in available_tools}
    if tool_name not in tool_names:
        return (
            f"Error: tool '{tool_name}' not found. "
            f"Available tools: {', '.join(sorted(tool_names))}."
        )

    # Check required parameters
    tool_def = next((t for t in available_tools if t["name"] == tool_name), None)
    if tool_def:
        schema = tool_def.get("input_schema", {})
        required = schema.get("required", [])
        props = schema.get("properties", {})
        missing = [p for p in required if p not in tool_input]
        if missing:
            examples = []
            for p in missing:
                ptype = props.get(p, {}).get("type", "string")
                examples.append(f"{p} ({ptype})")
            return (
                f"Error: tool '{tool_name}' requires parameters: "
                f"{', '.join(examples)}. "
                f"You provided: {json.dumps(tool_input)}."
            )

    return None


def _check_tainted_trusted_arc_creation(conversation_id: int, code: str) -> None:
    """Check if tainted conversation attempts to create trusted arcs.

    Logs a warning if tainted code tries to set integrity_level='trusted'
    on arc.create() or arc.add_child() calls. The real enforcement happens
    at the callback handler level; this provides observability.

    Args:
        conversation_id: ID of the conversation to check.
        code: Python code to analyze.
    """
    try:
        from ..security.trust import is_conversation_tainted
        if not is_conversation_tainted(conversation_id):
            return

        # Conversation is tainted, check for trusted arc creation attempts
        _check_code_for_trusted_arc_calls(conversation_id, code)
    except (ImportError, sqlite3.Error, ValueError) as _exc:
        # Fail silently - this is just observability logging
        pass


def _check_code_for_trusted_arc_calls(conversation_id: int, code: str) -> None:
    """Check code for arc.create/add_child calls with integrity_level='trusted'.

    Helper for _check_tainted_trusted_arc_creation that does the AST analysis.
    """
    import ast as _ast

    try:
        tree = _ast.parse(code)
    except SyntaxError:
        return

    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue

        func = node.func
        if not (isinstance(func, _ast.Attribute) and func.attr in ("create", "add_child")):
            continue

        # Check if any keyword argument sets integrity_level='trusted'
        if _has_trusted_integrity_level(node.keywords):
            logger.warning(
                "Tainted conversation %d attempted to create trusted arc",
                conversation_id,
            )


def _has_trusted_integrity_level(keywords: list) -> bool:
    """Check if keyword arguments contain integrity_level='trusted'.

    Args:
        keywords: List of ast.keyword nodes from a function call.

    Returns:
        True if integrity_level='trusted' is found, False otherwise.
    """
    import ast as _ast

    for kw in keywords:
        if kw.arg == "integrity_level":
            if isinstance(kw.value, _ast.Constant) and kw.value.value == "trusted":
                return True
    return False


def _execute_chat_tool(
    tool_name: str,
    tool_input: dict,
    conversation_id: int | None = None,
    executor_arc_id: int | None = None,
    executor_conv_id: int | None = None,
) -> str:
    """Execute a chat tool and return the result as a string.

    Dispatch order:
    1. Platform-registered handlers (from platform packages via register_tool_handler)
    2. Platform tools (submit_code, escalate, escalate_current_arc) — inline
    3. Loaded handlers (from user-configurable config/chat_tools/ modules)
    """
    try:
        # 1. Check registered handlers first (from platform packages)
        if tool_name in _extra_tool_handlers:
            handler = _extra_tool_handlers[tool_name]
            return handler(
                tool_input,
                conversation_id=conversation_id,
                executor_arc_id=executor_arc_id,
                executor_conv_id=executor_conv_id,
            )

        # 2. Platform tools — security-critical, kept inline
        if tool_name == "submit_code":
            return _handle_submit_code(
                tool_input, conversation_id=conversation_id,
                executor_arc_id=executor_arc_id,
                executor_conv_id=executor_conv_id,
            )
        elif tool_name == "escalate_current_arc":
            return _handle_escalate_current_arc(
                tool_input, conversation_id=conversation_id,
            )
        elif tool_name == "escalate":
            return _handle_escalate(
                tool_input, executor_arc_id=executor_arc_id,
            )
        elif tool_name == "fetch_web_content":
            return _handle_fetch_web_content(
                tool_input, conversation_id=conversation_id,
            )

        # 3. Loaded handlers (from config/chat_tools/ modules)
        from ..chat_tool_loader import get_handler
        handler = get_handler(tool_name)
        if handler:
            return handler(
                tool_input,
                conversation_id=conversation_id,
                executor_arc_id=executor_arc_id,
                executor_conv_id=executor_conv_id,
            )

        return f"Unknown tool: {tool_name}"
    except Exception as e:  # broad catch: tool handlers may raise anything
        logger.error("Chat tool %s error: %s", tool_name, e)
        return f"Error: {e}"


def _handle_submit_code(
    tool_input: dict,
    conversation_id: int | None = None,
    executor_arc_id: int | None = None,
    executor_conv_id: int | None = None,
) -> str:
    """Handle submit_code — security-critical platform tool."""
    from ..tool_backends import state as state_backend

    code = tool_input["code"]
    desc = tool_input.get("description", "submitted_code")
    conv_id_for_review = conversation_id or 0
    # Determine review mode: trusted (intent-only) vs full security.
    _is_tainted = False
    if conversation_id:
        try:
            from ..security.trust import is_conversation_tainted as _ict
            _is_tainted = _ict(conversation_id)
        except (ImportError, sqlite3.Error, ValueError) as _exc:
            _is_tainted = True  # fail-closed
    from ..review.pipeline import run_review_pipeline
    from ..review.profiles import PROFILE_PLANNER, PROFILE_STEP
    _is_arc_step = executor_arc_id is not None
    _arc_is_trusted = False
    if _is_arc_step and not _is_tainted:
        try:
            from ..core.arcs import manager as _am
            _arc_info = _am.get_arc(executor_arc_id)
            _arc_is_trusted = (
                _arc_info is not None
                and _arc_info.get("integrity_level") == "trusted"
            )
        except (ImportError, sqlite3.Error, KeyError) as _exc:
            pass  # fail-closed: treat as untrusted
    _profile = PROFILE_PLANNER if _arc_is_trusted else (
        PROFILE_STEP if (_is_tainted or _is_arc_step) else PROFILE_PLANNER
    )
    pipeline_result = run_review_pipeline(
        code, conv_id_for_review, profile=_profile, arc_id=executor_arc_id,
    )
    if pipeline_result.status == "syntax_error":
        return f"Syntax error: {pipeline_result.reason}"
    if pipeline_result.status in ("major_alert", "rejected"):
        return (
            f"Code REJECTED ({pipeline_result.status}): "
            f"{pipeline_result.reason}\n"
            "Please revise and resubmit."
        )
    # Approved, minor_concern, or cached_approval — execute

    # Pre-execution taint check: block web/network tools from chat context
    if not _is_arc_step:
        try:
            from ..security.trust import check_code_for_taint as _pre_taint_check
            _pre_taint = _pre_taint_check(code)
            if _pre_taint:
                return (
                    f"submit_code: BLOCKED — code imports {_pre_taint} which "
                    "accesses external/untrusted data. Web tools cannot be "
                    "called from chat context.\n\n"
                    "To fetch web content, create an untrusted arc batch:\n"
                    "```\n"
                    "from carpenter_tools.act import arc\n"
                    "arc.create_batch(arcs=[\n"
                    '  {"name": Label("Fetch data"),\n'
                    '   "goal": UnstructuredText("Fetch content from <URL>"),\n'
                    '   "integrity_level": Label("untrusted"),\n'
                    '   "output_type": Label("json"),\n'
                    '   "agent_type": Label("EXECUTOR")},\n'
                    '  {"name": Label("Review data"),\n'
                    '   "agent_type": Label("REVIEWER"),\n'
                    '   "integrity_level": Label("trusted"),\n'
                    '   "reviewer_profile": Label("security-reviewer")},\n'
                    '  {"name": Label("Judge review"),\n'
                    '   "agent_type": Label("JUDGE"),\n'
                    '   "integrity_level": Label("trusted"),\n'
                    '   "reviewer_profile": Label("judge")},\n'
                    "])\n"
                    "```\n"
                    "IMPORTANT: The EXECUTOR arc MUST have "
                    '"integrity_level": Label("untrusted"). '
                    "See KB [[web/trust-warning]] for details."
                )
        except Exception as _exc:  # broad catch: fail-open pre-check
            pass

    prefix = ""
    if pipeline_result.status == "minor_concern":
        prefix = f"[Reviewer note: {pipeline_result.reason}]\n"

    save_result = code_manager.save_code(
        code, source="chat_agent", name=desc,
    )

    if pipeline_result.status in ("approved", "minor_concern", "cached_approval"):
        with db_transaction() as db:
            db.execute(
                "UPDATE code_files SET review_status = ? WHERE id = ?",
                ("approved", save_result["code_file_id"]),
            )

    exec_result = code_manager.execute(
        save_result["code_file_id"],
        conversation_id=executor_conv_id if executor_conv_id is not None else conversation_id,
        arc_id=executor_arc_id,
        execution_context="reviewed",
    )
    output = ""
    if exec_result.get("log_file"):
        try:
            with open(exec_result["log_file"]) as f:
                output = f.read()[-4000:]
        except OSError:
            pass
    status = exec_result["execution_status"]
    status_prefix = f"[{status}] " if status != "success" else ""
    flags_note = ""
    if pipeline_result.advisory_flags:
        flags_note = f"\nAdvisory flags: {pipeline_result.advisory_flags}"
    # Record taint — fail-closed
    taint_source = None
    taint_check_failed = False
    if conversation_id:
        try:
            from ..security.trust import check_code_for_taint, record_taint
            taint_source = check_code_for_taint(code)
            if taint_source:
                record_taint(conversation_id, taint_source)
        except Exception as _exc:  # broad catch: fail-closed taint check
            taint_check_failed = True
            taint_source = "(taint-check-error)"
            logger.warning(
                "Taint check failed for conversation %d; "
                "treating as tainted (fail-closed)",
                conversation_id,
                exc_info=True,
            )

    if conversation_id:
        _check_tainted_trusted_arc_creation(conversation_id, code)

    if taint_source:
        exec_id = exec_result.get("execution_id", 0)
        output_key = f"exec_{exec_id:06d}"
        output_bytes = len(output.encode("utf-8")) if output else 0
        exit_code = exec_result.get("exit_code", -1)

        try:
            state_backend.handle_set({
                "arc_id": 0,
                "key": output_key,
                "value": {
                    "_tainted": True,
                    "_taint_source": taint_source,
                    "output": output,
                    "execution_id": exec_id,
                    "log_file": exec_result.get("log_file", ""),
                },
            })
        except (sqlite3.Error, KeyError, ValueError) as _exc:
            logger.warning(
                "Failed to store tainted output in arc state for exec %s",
                exec_id, exc_info=True,
            )

        try:
            _tdb = get_db()
            try:
                _tdb.execute(
                    "UPDATE code_executions SET taint_source = ? WHERE id = ?",
                    (taint_source, exec_id),
                )
                _tdb.commit()
            finally:
                _tdb.close()
        except sqlite3.Error as _exc:
            logger.debug(
                "Failed to persist taint_source on execution %s", exec_id,
                exc_info=True,
            )

        metadata = {
            "status": "executed",
            "output_key": output_key,
            "output_bytes": output_bytes,
            "exit_code": exit_code,
            "guidance": (
                "Output withheld (tainted). To access this data, "
                "create an untrusted arc batch via arc.create_batch() "
                "with integrity_level='untrusted' for the fetcher arc, "
                "plus REVIEWER and JUDGE arcs. "
                "See kb entry [[web/trust-warning]] for the exact pattern."
            ),
        }
        if exit_code != 0:
            error_type = "RuntimeError"
            if output:
                for line in reversed(output.strip().splitlines()):
                    line = line.strip()
                    if ":" in line and not line.startswith(" "):
                        candidate = line.split(":")[0].strip()
                        if candidate and candidate[0].isupper() and " " not in candidate:
                            error_type = candidate
                            break
            metadata["error_type"] = error_type

        result_str = json.dumps(metadata)

        # Invariant I1: tainted output must not leak into return value
        stripped_output = output.strip() if output else ""
        if stripped_output and len(stripped_output) > 8 and stripped_output in result_str:
            raise RuntimeError(
                "Trust invariant violation (I1): tainted execution output "
                "leaked into submit_code return value"
            )
        return result_str

    return f"{prefix}{status_prefix}{output}{flags_note}"


def _handle_escalate_current_arc(
    tool_input: dict,
    conversation_id: int | None = None,
) -> str:
    """Handle escalate_current_arc — platform tool."""
    from . import model_resolver
    from ..tool_backends import state as state_backend

    reason = tool_input["reason"]
    task_type = tool_input.get("task_type", "general")

    with db_connection() as db:
        row = db.execute(
            "SELECT model FROM api_calls WHERE conversation_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (conversation_id,)
        ).fetchone()
        current_model = row["model"] if row else model_resolver.get_model_for_role("chat")

    next_model = model_resolver.get_next_model(current_model, task_type)
    if next_model is None:
        return "Already at highest available model tier."

    state_backend.handle_set({
        "arc_id": 0,
        "key": "pending_escalation",
        "value": {
            "target_model": next_model,
            "reason": reason,
            "task_type": task_type,
            "conversation_id": conversation_id,
        },
    })

    cost_msg = model_resolver.estimate_cost_multiplier(current_model, next_model)

    require_confirm = config.CONFIG.get("escalation", {}).get("require_confirmation", True)
    if not require_confirm:
        return f"Escalated to {next_model}. Continuing..."

    return (
        f"I'd like to escalate to {next_model} for this task. ({cost_msg} cost)\n"
        f"Reason: {reason}\n\n"
        f"Reply 'yes' to approve, or 'no' to continue with current model."
    )


def _handle_escalate(
    tool_input: dict,
    executor_arc_id: int | None = None,
) -> str:
    """Handle escalate — platform tool for self-escalation."""
    from ..core.arcs import manager as _am
    from . import model_resolver

    if executor_arc_id is None:
        return "Error: escalate can only be called from within an arc execution context."

    arc = _am.get_arc(executor_arc_id)
    if arc is None:
        return f"Error: Arc #{executor_arc_id} not found."

    if arc["status"] in _am.FROZEN_STATUSES:
        return f"Error: Arc #{executor_arc_id} is already frozen (status: {arc['status']})."

    current_model = None
    config_id = arc.get("agent_config_id")
    if config_id:
        cfg = _am.get_agent_config(config_id)
        if cfg:
            current_model = cfg["model"]
    if not current_model:
        current_model = model_resolver.get_model_for_role("default_step")

    next_model = model_resolver.get_next_model(current_model, "general")
    if next_model is None:
        return "Already at highest available model tier. Cannot escalate further."

    children = _am.get_children(executor_arc_id)
    child_summary = ""
    if children:
        child_lines = [f"  - Arc #{c['id']} [{c['status']}]: {c['name']}" for c in children]
        child_summary = "\nChild arcs:\n" + "\n".join(child_lines)

    enhanced_goal = (
        f"{arc['goal'] or arc['name']}\n\n"
        f"--- Escalation Context ---\n"
        f"This is an escalation of Arc #{executor_arc_id}.\n"
        f"Use get_arc_detail(arc_id={executor_arc_id}) to inspect the prior arc's state and history.\n"
        f"{child_summary}"
    )

    new_config_id = _am.get_or_create_agent_config(model=next_model)
    new_arc_id = _am.create_arc(
        name=f"{arc['name']} (escalated)",
        goal=enhanced_goal,
        parent_id=arc["parent_id"],
        step_order=arc["step_order"],
        agent_config_id=new_config_id,
        agent_type="PLANNER",
        integrity_level=arc["integrity_level"],
        output_type=arc["output_type"],
    )

    try:
        _am.update_status(executor_arc_id, "escalated")
    except ValueError:
        logger.warning("Could not transition arc %d to escalated", executor_arc_id)

    _am.grant_read_access(
        new_arc_id, executor_arc_id,
        depth="subtree",
        reason="Self-escalation",
        granted_by="platform",
    )

    with db_transaction() as db:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
            (new_arc_id, "_escalated_from", json.dumps(executor_arc_id)),
        )

    return f"Escalated to {next_model}. Arc #{new_arc_id} created. This arc is now frozen."


# Pre-verified fetch script.  The URL is read from arc state (set by the
# platform before dispatch) so the script body is identical for every fetch,
# keeping a single hash in verified_code_hashes.
_FETCH_SCRIPT = """\
from carpenter_tools.act import web
from carpenter_tools.act import state as write_state
from carpenter_tools.read import state as read_state
from carpenter_tools.declarations import Label

url = read_state.get(Label("fetch_url"))
result = web.fetch_webpage(url)
write_state.set(Label("fetched_content"), result)
"""


def _handle_fetch_web_content(
    tool_input: dict,
    conversation_id: int | None = None,
) -> str:
    """Handle fetch_web_content — create an untrusted arc batch to fetch a URL.

    Creates a parent PLANNER arc with three children:
      1. EXECUTOR (untrusted) — fetches the URL using a pre-verified script
      2. REVIEWER (trusted) — reviews the untrusted output
      3. JUDGE (trusted) — validates the review

    The parent arc completes when all children finish, triggering
    arc.chat_notify to deliver results back to the conversation.
    """
    from ..core.arcs import manager as _am
    from ..core.engine import work_queue as _wq
    from ..core.workflows._arc_state import set_arc_state
    from ..tool_backends import arc as arc_backend

    url = tool_input.get("url", "").strip()
    goal = tool_input.get("goal", "").strip()

    if not url:
        return "Error: url is required."
    if not goal:
        return "Error: goal is required."

    # Create parent arc
    parent_id = _am.create_arc(
        name=f"Fetch: {url[:60]}",
        goal=f"Fetch content from {url} and extract: {goal}",
        agent_type="PLANNER",
    )

    # Link parent to conversation
    if conversation_id:
        from . import conversation as _conv
        _conv.link_arc_to_conversation(conversation_id, parent_id)

    # Activate parent so freeze_arc() can transition it
    _am.update_status(parent_id, "active")

    # Create children via create_batch (handles Fernet keys, review_keys, etc.)
    batch_result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": "Fetch web content",
                "goal": (
                    "Submit this EXACT code via submit_code "
                    "(do not modify it):\n"
                    "```python\n" + _FETCH_SCRIPT + "```\n"
                    "The URL has been pre-set in arc state as 'fetch_url'."
                ),
                "parent_id": parent_id,
                "integrity_level": "untrusted",
                "output_type": "json",
                "agent_type": "EXECUTOR",
                "step_order": 0,
            },
            {
                "name": "Review fetched content",
                "goal": (
                    f"Read the untrusted output from the fetch arc. "
                    f"Extract the relevant information the user wanted: {goal}. "
                    f"Store a clean summary in arc state under key '_agent_response'."
                ),
                "parent_id": parent_id,
                "agent_type": "REVIEWER",
                "integrity_level": "trusted",
                "reviewer_profile": "security-reviewer",
                "step_order": 1,
            },
            {
                "name": "Validate review",
                "goal": (
                    "Validate that the reviewer's extraction is accurate and complete. "
                    "Copy the final answer to arc state key '_agent_response'."
                ),
                "parent_id": parent_id,
                "agent_type": "JUDGE",
                "integrity_level": "trusted",
                "reviewer_profile": "judge",
                "step_order": 2,
            },
        ],
    })

    if "error" in batch_result:
        # Clean up the parent
        try:
            _am.update_status(parent_id, "failed")
        except (ValueError, Exception):
            pass
        return f"Error creating web fetch arcs: {batch_result['error']}"

    child_ids = batch_result["arc_ids"]
    executor_arc_id = child_ids[0]

    # Pre-set the URL in the EXECUTOR arc's state so the script can read it.
    set_arc_state(executor_arc_id, "fetch_url", url)

    # Link children to conversation too
    if conversation_id:
        from . import conversation as _conv
        for child_id in child_ids:
            _conv.link_arc_to_conversation(conversation_id, child_id)

    # Enqueue the first child (EXECUTOR) for dispatch
    _wq.enqueue(
        "arc.dispatch",
        {"arc_id": executor_arc_id},
        idempotency_key=f"arc_dispatch:{executor_arc_id}",
    )

    return f"Web fetch started (arc #{parent_id}). Result will arrive automatically."


def _save_api_call(
    conv_id: int,
    model: str,
    usage: dict,
    stop_reason: str | None = None,
    latency_ms: int | None = None,
    arc_id: int | None = None,
):
    """Persist API call metrics (tokens, cache stats) to the api_calls table.

    Args:
        conv_id: Conversation ID.
        model: Model name used for this call.
        usage: The 'usage' dict from the API response.
        stop_reason: The stop_reason from the API response.
        latency_ms: Wall-clock latency of the API call in milliseconds.
        arc_id: Arc ID that triggered this call (if applicable).
    """
    with db_transaction() as db:
        db.execute(
            "INSERT INTO api_calls "
            "(conversation_id, model, input_tokens, output_tokens, "
            " cache_creation_input_tokens, cache_read_input_tokens, stop_reason, "
            " latency_ms, arc_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conv_id,
                model,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                usage.get("cache_creation_input_tokens", 0),
                usage.get("cache_read_input_tokens", 0),
                stop_reason,
                latency_ms,
                arc_id,
            ),
        )


def _save_tool_calls(
    conv_id: int,
    msg_id: int,
    tool_blocks: list[dict],
    tool_results: dict[str, str],
    timings: dict[str, int],
):
    """Persist tool call records to the tool_calls table.

    Args:
        conv_id: Conversation ID.
        msg_id: Message ID of the assistant message containing tool_use blocks.
        tool_blocks: List of tool_use content blocks from the API response.
        tool_results: Map of tool_use_id -> result text.
        timings: Map of tool_use_id -> duration in milliseconds.
    """
    with db_transaction() as db:
        for block in tool_blocks:
            if block.get("type") != "tool_use":
                continue
            tool_id = block["id"]
            # Sanitize strings to remove surrogate characters that some
            # backends (e.g. Ollama proxies) may introduce.  SQLite's
            # Python driver rejects surrogates during UTF-8 encoding.
            input_json = json.dumps(block["input"])
            result_text = tool_results.get(tool_id)
            if isinstance(input_json, str):
                input_json = input_json.encode("utf-8", errors="replace").decode("utf-8")
            if isinstance(result_text, str):
                result_text = result_text.encode("utf-8", errors="replace").decode("utf-8")
            db.execute(
                "INSERT INTO tool_calls "
                "(conversation_id, message_id, tool_use_id, tool_name, "
                " input_json, result_text, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    conv_id,
                    msg_id,
                    tool_id,
                    block["name"],
                    input_json,
                    result_text,
                    timings.get(tool_id),
                ),
            )


def _build_arcs_summary(conv_arc_ids: list[int]) -> str:
    """Build a summary of active arcs, highlighting conversation-specific ones."""
    from ..core.arcs import manager as arc_manager
    with db_connection() as db:
        # Get all active/waiting arcs
        rows = db.execute(
            "SELECT id, name, status, goal FROM arcs "
            "WHERE status IN ('active', 'waiting', 'pending') "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()

    if not rows:
        return "No active arcs."

    lines = []
    conv_set = set(conv_arc_ids)
    for r in rows:
        goal = (r["goal"] or "")[:80]
        marker = " [this conversation]" if r["id"] in conv_set else ""
        lines.append(f"#{r['id']} [{r['status']}] {r['name']}: {goal}{marker}")
    return "\n".join(lines)


def _invoke_with_escalated_model(
    user_message: str,
    conversation_id: int,
    target_model: str,
    reason: str,
    api_key: str | None = None,
) -> dict:
    """Continue conversation with escalated model (single turn).

    Switches client based on target model provider, calls AI once,
    returns response. Subsequent turns revert to base model unless
    escalation is triggered again.

    Args:
        user_message: The user's message (already added to conversation).
        conversation_id: The conversation ID.
        target_model: The escalated model to use.
        reason: Reason for escalation (logged in system message).
        api_key: API key override.

    Returns:
        Dict with 'conversation_id', 'response_text', 'code', 'message_id'.
    """
    from . import model_resolver

    provider, model_name = model_resolver.parse_model_string(target_model)
    client = model_resolver.create_client_for_model(target_model)

    # Add system note about escalation
    conversation.add_message(
        conversation_id, "system",
        f"[Escalated to {target_model}: {reason}]"
    )

    # Build system prompt (reuse chat template logic)
    conv_arc_ids = conversation.get_conversation_arc_ids(conversation_id)
    arcs_summary = _build_arcs_summary(conv_arc_ids)
    system = templates.render(
        "chat_new",
        system_prompt=_build_chat_system_prompt(),
        active_arcs_summary=arcs_summary,
        prior_context_tail="",
    )

    # Get messages
    messages = conversation.get_messages(conversation_id)
    api_messages = conversation.format_messages_for_api(messages)

    # Convert history to provider format (same fix as invoke_chat)
    _esc_standard = _get_api_standard_for_client(client)
    api_messages, _ = _convert_history_to_standard(
        api_messages, _esc_standard, [None] * len(api_messages)
    )

    # Call AI with escalated model
    tools = _select_chat_tools()
    response = _call_with_retries(
        system, api_messages,
        client=client,
        model=target_model,
        api_key=api_key,
        max_retries=config.CONFIG.get("mechanical_retry_max", 4),
        tools=tools,
        operation_type="chat",
    )

    if response is None:
        return {
            "conversation_id": conversation_id,
            "response_text": "Escalation failed - couldn't reach target model.",
            "code": None,
            "message_id": None,
        }

    # Extract response — response is normalized to canonical format
    text = ""
    for block in response.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
    code = api_standard.extract_code_from_text(text)

    # Save API call with escalated model
    usage = response.get("usage", {})
    _save_api_call(conversation_id, target_model, usage, response.get("stop_reason"))

    # Save assistant message
    msg_id = conversation.add_message(conversation_id, "assistant", text)

    total_tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    conversation.update_token_count(conversation_id, total_tokens)

    return {
        "conversation_id": conversation_id,
        "response_text": text,
        "code": code,
        "message_id": msg_id,
    }


# ---------------------------------------------------------------------------
# Context window compaction
# ---------------------------------------------------------------------------


def _get_context_window(model_str: str | None = None) -> int:
    """Resolve the context window size for a model string.

    Resolution order:
    1. Exact model string match in context_windows config (e.g. "local:qwen2.5-1.5b-q4")
    2. Provider prefix match (e.g. "local")
    3. _DEFAULT_CONTEXT_WINDOW (200000)

    Args:
        model_str: Model string in "provider:model" format, or None.

    Returns:
        Context window size in tokens.
    """
    context_windows = config.CONFIG.get("context_windows", {})

    if model_str:
        # 1. Exact match
        if model_str in context_windows:
            return context_windows[model_str]

        # 2. Provider prefix match
        if ":" in model_str:
            provider = model_str.split(":", 1)[0]
            if provider in context_windows:
                return context_windows[provider]

    return _DEFAULT_CONTEXT_WINDOW


# Summarization prompt for compaction
_COMPACTION_PROMPT = (
    "Summarize the following conversation segment concisely. Preserve:\n"
    "- Key decisions made\n"
    "- State mutations (files written, arcs created, config changes)\n"
    "- Important results and outcomes\n"
    "- Any unresolved questions or pending work\n"
    "- Error conditions encountered\n\n"
    "Discard tool call details, intermediate reasoning, and verbose output.\n"
    "Produce a compact summary a future agent can use to continue the work."
)


def _estimate_tokens(messages: list[dict], system: str = "") -> int:
    """Estimate token count for a list of API messages.

    Uses a simple heuristic: character count / 4. This is intentionally
    approximate -- we only need to know when we're getting close to the
    context window, not an exact count.

    Args:
        messages: API-format messages (role + content).
        system: System prompt text.

    Returns:
        Estimated token count.
    """
    total_chars = len(system)
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            # Structured content (tool_use blocks, tool_result blocks)
            total_chars += len(json.dumps(content))
    return total_chars // 4


def _should_compact(
    estimated_tokens: int,
    context_window: int,
) -> bool:
    """Check whether compaction should be triggered.

    Returns True if either the fractional threshold or absolute token
    threshold is exceeded.

    Args:
        estimated_tokens: Current estimated token count.
        context_window: Model's context window size.

    Returns:
        True if compaction should occur.
    """
    frac_threshold = config.CONFIG.get("compaction_threshold", 0.8)
    abs_threshold = config.CONFIG.get("compaction_threshold_tokens", 0)

    if estimated_tokens >= context_window * frac_threshold:
        return True
    if abs_threshold > 0 and estimated_tokens >= abs_threshold:
        return True
    return False


def _compact_messages(
    api_messages: list[dict],
    conversation_id: int,
    db_message_ids: list[int | None],
    system: str,
    *,
    client=None,
    api_key: str | None = None,
) -> tuple[list[dict], list[int | None], int]:
    """Perform context window compaction on in-memory api_messages.

    Identifies the compactable segment (everything except the most recent
    ``compaction_preserve_recent`` messages), summarizes it via an AI call,
    records the compaction event in the database, and replaces the compacted
    portion in-memory.

    Args:
        api_messages: Current API-format messages (mutated in place is avoided;
            a new list is returned).
        conversation_id: Active conversation ID.
        db_message_ids: Parallel list mapping each api_messages entry to its
            database message ID (or None for synthetic entries).
        system: System prompt (for token estimation).
        client: AI client module.
        api_key: API key override.

    Returns:
        Tuple of (new_api_messages, new_db_message_ids, tokens_reclaimed).
        If compaction was skipped (too few messages), returns the inputs
        unchanged with tokens_reclaimed=0.
    """
    preserve_n = config.CONFIG.get("compaction_preserve_recent", 8)

    # Need more messages than we preserve to have something to compact
    if len(api_messages) <= preserve_n:
        return api_messages, db_message_ids, 0

    # Split: compactable segment vs preserved tail
    compact_end = len(api_messages) - preserve_n
    if compact_end <= 0:
        return api_messages, db_message_ids, 0

    compactable = api_messages[:compact_end]
    preserved = api_messages[compact_end:]
    compact_ids = db_message_ids[:compact_end]
    preserved_ids = db_message_ids[compact_end:]

    # Estimate tokens before compaction
    tokens_before = _estimate_tokens(api_messages, system)

    # Build the text for summarization
    text_parts = []
    for msg in compactable:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, str):
            text_parts.append(f"{role}: {content}")
        elif isinstance(content, list):
            # Structured content -- serialize to a readable form
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(f"{role}: {block.get('text', '')}")
                elif block.get("type") == "tool_use":
                    text_parts.append(
                        f"{role}: [tool_use: {block.get('name', '?')}({json.dumps(block.get('input', {}))})]"
                    )
                elif block.get("type") == "tool_result":
                    text_parts.append(
                        f"{role}: [tool_result: {str(block.get('content', ''))[:200]}]"
                    )
                else:
                    text_parts.append(f"{role}: {json.dumps(block)}")

    segment_text = "\n".join(text_parts)

    # Call the AI to summarize
    if client is None:
        client = _get_client()

    summary_messages = [
        {"role": "user", "content": f"{_COMPACTION_PROMPT}\n\n---\n\n{segment_text}"},
    ]

    try:
        summary_response = _call_with_retries(
            "You are a conversation summarizer. Be concise and preserve key information.",
            summary_messages,
            client=client,
            api_key=api_key,
            max_retries=2,
            operation_type="summarization",
        )
    except Exception:  # broad catch: AI provider call may raise anything
        logger.exception("Compaction summarization call failed")
        return api_messages, db_message_ids, 0

    if summary_response is None:
        logger.warning("Compaction summarization returned None, skipping compaction")
        return api_messages, db_message_ids, 0

    # Extract summary text — response is normalized to canonical format
    summary_text = ""
    for block in summary_response.get("content", []):
        if block.get("type") == "text":
            summary_text += block.get("text", "")

    if not summary_text.strip():
        logger.warning("Compaction produced empty summary, skipping")
        return api_messages, db_message_ids, 0

    # Determine message ID range for the compaction event
    valid_ids = [mid for mid in compact_ids if mid is not None]
    if valid_ids:
        msg_id_start = min(valid_ids)
        msg_id_end = max(valid_ids)
    else:
        # All synthetic messages (unlikely but handle gracefully)
        msg_id_start = 0
        msg_id_end = 0

    # Record compaction event in DB
    with db_transaction() as db:
        # Estimate tokens reclaimed
        tokens_after_estimate = _estimate_tokens(
            [{"role": "user", "content": f"[Compacted context]\n{summary_text}"}] + preserved,
            system,
        )
        tokens_reclaimed = tokens_before - tokens_after_estimate

        # Determine model used
        call_model = (
            summary_response.get("model", "")
            or model_resolver.get_model_for_role("compaction")
        )

        cursor = db.execute(
            "INSERT INTO compaction_events "
            "(conversation_id, message_id_start, message_id_end, model, tokens_reclaimed) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversation_id, msg_id_start, msg_id_end, call_model, tokens_reclaimed),
        )
        compaction_event_id = cursor.lastrowid

        # Insert synthetic summary message in the messages table
        cursor2 = db.execute(
            "INSERT INTO messages "
            "(conversation_id, role, content, compaction_event_id) "
            "VALUES (?, 'system', ?, ?)",
            (conversation_id, f"[Compacted context]\n{summary_text}", compaction_event_id),
        )
        synthetic_msg_id = cursor2.lastrowid

        # Update last_message_at
        db.execute(
            "UPDATE conversations SET last_message_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conversation_id,),
        )

        # Mark original messages with the compaction_event_id
        if valid_ids:
            placeholders = ",".join("?" for _ in valid_ids)
            db.execute(
                f"UPDATE messages SET compaction_event_id = ? "
                f"WHERE id IN ({placeholders})",
                [compaction_event_id] + valid_ids,
            )


    logger.info(
        "Compacted conversation %d: messages %d-%d, tokens reclaimed ~%d",
        conversation_id, msg_id_start, msg_id_end, tokens_reclaimed,
    )

    # Build new api_messages: summary message + preserved tail
    summary_msg = {
        "role": "user",
        "content": f"[System notification: Compacted context]\n{summary_text}",
    }
    new_api_messages = [summary_msg] + preserved
    new_db_ids = [synthetic_msg_id] + preserved_ids

    return new_api_messages, new_db_ids, tokens_reclaimed


def _build_message_id_map(
    db_messages: list[dict],
    api_messages: list[dict],
) -> list[int | None]:
    """Build a parallel list of DB message IDs for each api_messages entry.

    Because ``format_messages_for_api`` may merge consecutive same-role
    messages, this is a best-effort mapping. Each api_messages entry gets
    the ID of the first DB message that contributed to it.

    Args:
        db_messages: Raw messages from the database.
        api_messages: Formatted API messages (after merging).

    Returns:
        List of message IDs (or None) with the same length as api_messages.
    """
    # Extract IDs from DB messages in order
    raw_ids = [m.get("id") for m in db_messages]

    if len(api_messages) == len(db_messages):
        # No merging happened -- 1:1 mapping
        return raw_ids

    # Merging happened. Walk through raw messages and assign IDs to API
    # messages. format_messages_for_api processes messages in order and
    # merges consecutive same-role string messages. We replicate that
    # logic to map IDs.
    result = []
    raw_idx = 0
    for _api_msg in api_messages:
        if raw_idx < len(raw_ids):
            result.append(raw_ids[raw_idx])
        else:
            result.append(None)
        # Skip past any DB messages that were merged into this API message
        raw_idx += 1
        while raw_idx < len(db_messages):
            # Check if the next raw message was merged (same role, string content)
            # We can't perfectly detect this, so we use a conservative heuristic:
            # if the api_messages list is shorter, some messages were merged.
            if len(result) < len(api_messages):
                break
            raw_idx += 1

    # Pad or trim to match api_messages length
    while len(result) < len(api_messages):
        result.append(None)

    return result[:len(api_messages)]


def invoke_for_chat(
    user_message: str,
    *,
    conversation_id: int | None = None,
    api_key: str | None = None,
    _message_already_saved: bool = False,
    _system_triggered: bool = False,
    _executor_arc_id: int | None = None,
    _executor_conv_id: int | None = None,
    _model_override: str | None = None,
) -> dict:
    """Handle a chat message — get or create conversation, call the AI model with tools.

    Supports tool_use: if the model requests tool calls, they are executed
    and results fed back in a loop until the model produces a final text response.

    Args:
        user_message: The user's chat message.
        conversation_id: Explicit conversation ID (skips prior context if set).
        api_key: API key override.
        _message_already_saved: If True, skip conversation resolution and
            message saving (caller already did it). conversation_id is required.
        _system_triggered: If True, this invocation was triggered by a system
            notification (e.g., arc completion). Skips adding user message,
            escalation check, and title generation. The system message is
            already in the DB and will appear as a user-role message via
            format_messages_for_api().

    Returns:
        Dict with 'conversation_id', 'response_text', 'code' (if any),
        and 'message_id'.
    """
    if _system_triggered:
        # System-triggered: conversation_id is required, message already in DB
        conv_id = conversation_id
        has_prior_messages = True  # Skip title generation
    elif _message_already_saved:
        # Caller (chat.py) already resolved conv_id and saved the user message.
        conv_id = conversation_id
        existing_messages = conversation.get_messages(conv_id)
        # The user message we just saved counts, so check for prior ones
        user_msgs = [m for m in existing_messages if m["role"] == "user"]
        has_prior_messages = len(user_msgs) > 1
    else:
        # Two context modes:
        #
        # 1. Conversation-specific UI (web UI with tabs/dropdown):
        #    conversation_id is provided. Full history is loaded — no time-based
        #    truncation or compaction. Prior context from other conversations is
        #    not injected. The agent sees the entire conversation.
        #
        # 2. Single-conversation medium (Signal, WhatsApp, Telegram bots):
        #    conversation_id is None. get_or_create_conversation() applies a
        #    6-hour time boundary, creating a new conversation and carrying
        #    over ~10 messages as prior context when the gap is too large.
        #
        if conversation_id is not None:
            # Mode 1: conversation-specific — verify it exists, use full history
            conv = conversation.get_conversation(conversation_id)
            if conv is None:
                return {
                    "conversation_id": None,
                    "response_text": f"Error: conversation #{conversation_id} not found.",
                    "code": None,
                    "message_id": None,
                }
            conv_id = conversation_id
        else:
            # Mode 2: single-medium — apply time-based context boundary
            conv_id = conversation.get_or_create_conversation()

        # Check if this is the first user message (for title generation later)
        existing_messages = conversation.get_messages(conv_id)
        has_prior_messages = any(m["role"] == "user" for m in existing_messages)

        # Add user message
        conversation.add_message(conv_id, "user", user_message)

    # Check for pending escalation approval (skip for system-triggered invocations)
    if not _system_triggered:
        from ..tool_backends import state as state_backend
        pending = state_backend.handle_get({"arc_id": 0, "key": "pending_escalation"})
    else:
        pending = {"value": None}

    if pending.get("value") is not None:
        escalation_data = pending["value"]
        user_lower = user_message.lower().strip()

        if user_lower in ("yes", "y", "approve", "escalate", "ok"):
            # Clear pending state
            state_backend.handle_set({"arc_id": 0, "key": "pending_escalation", "value": None})

            # Log escalation
            escalation_log = state_backend.handle_get({
                "arc_id": 0, "key": "escalation_history"
            }).get("value") or []
            escalation_log.append({
                "target_model": escalation_data["target_model"],
                "reason": escalation_data["reason"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            state_backend.handle_set({
                "arc_id": 0, "key": "escalation_history", "value": escalation_log
            })

            # Switch to escalated model for this turn
            return _invoke_with_escalated_model(
                user_message, conv_id, escalation_data["target_model"],
                escalation_data["reason"], api_key
            )

        elif user_lower in ("no", "n", "cancel", "skip", "decline"):
            # Rejection - clear and continue normally
            state_backend.handle_set({"arc_id": 0, "key": "pending_escalation", "value": None})
            conversation.add_message(conv_id, "system", "Escalation declined.")
            # Fall through to normal invocation

        else:
            # Ambiguous — clear stale escalation and continue normally.
            # Previous behaviour re-prompted, but stale escalation state
            # (e.g. surviving a restart) would silently block all chat messages
            # because the caller never sees the returned dict.
            state_backend.handle_set({"arc_id": 0, "key": "pending_escalation", "value": None})
            logger.info("Cleared stale escalation prompt (ambiguous response: %r)", user_message[:80])
            # Fall through to normal invocation

    # Get full conversation history
    messages = conversation.get_messages(conv_id)
    api_messages = conversation.format_messages_for_api(messages)

    # Build parallel list of DB message IDs for compaction tracking.
    # format_messages_for_api may merge consecutive same-role messages,
    # so we track the *first* message ID for each merged entry.
    db_message_ids = _build_message_id_map(messages, api_messages)

    # Prior context: only relevant for single-medium mode (mode 2).
    # In conversation-specific mode, the full history is already loaded.
    if conversation_id is not None:
        template_name = "chat_new"
        prior_text = ""
    else:
        # Prefer previous conversation's summary over raw tail messages
        prev_id = conversation.get_previous_conversation_id(conv_id)
        prev_summary = None
        if prev_id is not None:
            prev_summary = conversation.get_conversation_summary(prev_id)

        if prev_summary:
            template_name = "chat_compacted"
            prior_text = f"[Summary of previous conversation]\n{prev_summary}"
        else:
            prior = conversation.get_prior_context(conv_id)
            if prior:
                template_name = "chat_compacted"
                prior_text = "\n".join(
                    f"{m['role']}: {m['content']}" for m in prior
                )
            else:
                template_name = "chat_new"
                prior_text = ""

    # Detect arc step mode — arc agents don't need active arcs summary
    # or the dynamic prompt sections meant for interactive conversations.
    _is_arc = _executor_arc_id is not None

    if _is_arc:
        arcs_summary = ""
    else:
        conv_arc_ids = conversation.get_conversation_arc_ids(conv_id)
        arcs_summary = _build_arcs_summary(conv_arc_ids)

    # Resolve context window for the active model
    _chat_model = _model_override or model_resolver.get_model_for_role("chat")
    context_window = _get_context_window(_chat_model)

    # Build system prompt from template
    # Auto-search results are included and change per message, but the cost
    # is small (~50-100 tokens) and the benefit to model navigation is large.
    # Arc step agents skip auto-search and other interactive-only sections.
    system = templates.render(
        template_name,
        system_prompt=_build_chat_system_prompt(
            context_budget=context_window, model_name=_chat_model,
            messages=api_messages if not _is_arc else None,
            is_arc_step=_is_arc,
        ),
        active_arcs_summary=arcs_summary,
        prior_context_tail=prior_text,
    )

    client = _get_client(_model_override)

    # Convert history messages to provider-specific format.
    # For chain provider this is now a no-op (standard="anthropic"),
    # and chain_client converts per-backend as needed.
    _hist_standard = _get_api_standard_for_client(client)
    api_messages, db_message_ids = _convert_history_to_standard(
        api_messages, _hist_standard, db_message_ids
    )

    mechanical_max = config.CONFIG.get("mechanical_retry_max", 4)
    max_tool_iterations = config.CONFIG.get("chat_tool_iterations", 10)

    # Tools: select based on context budget
    try:
        tools = _select_chat_tools(context_window)
    except (ValueError, RuntimeError) as exc:
        err_msg = str(exc)
        conversation.add_message(conv_id, "system", err_msg)
        logger.error("Tool loading failed: %s", err_msg)
        return {"conversation_id": conv_id, "response_text": err_msg, "code": None, "message_id": None}

    collected_text = []
    total_tokens = 0
    last_msg_id = None
    last_stop_reason = None  # Track last stop_reason to detect tool_use loop exit

    # Per-tool token estimate (mirrors _select_chat_tools logic)
    _tpt = 150 if context_window <= 16384 else 80

    for iteration in range(max_tool_iterations):
        # --- Context window compaction check ---
        estimated = _estimate_tokens(api_messages, system)

        if iteration == 0:
            sys_tokens = _estimate_tokens([], system)
            msg_tokens = _estimate_tokens(api_messages, "")
            tool_tokens = len(tools) * _tpt
            logger.info(
                "Token estimate: system=%d, messages=%d, tools=~%d, "
                "total=%d/%d (%d tools)",
                sys_tokens, msg_tokens, tool_tokens,
                estimated + tool_tokens, context_window, len(tools),
            )

        if _should_compact(estimated, context_window):
            try:
                api_messages, db_message_ids, reclaimed = _compact_messages(
                    api_messages, conv_id, db_message_ids, system,
                    client=client, api_key=api_key,
                )
                if reclaimed > 0:
                    logger.info(
                        "Compaction reclaimed ~%d tokens (iteration %d)",
                        reclaimed, iteration,
                    )
            except Exception:  # broad catch: compaction involves AI calls
                logger.exception("Compaction failed, continuing without compaction")

        # Use low temperature for tool calls with small context windows
        _temp = 0.1 if tools and context_window <= 32768 else 0.7
        _call_t0 = time.monotonic()
        response = _call_with_retries(
            system, api_messages,
            client=client,
            model=_model_override,
            api_key=api_key,
            max_retries=mechanical_max,
            tools=tools,
            temperature=_temp,
            operation_type="chat",
        )
        _call_latency_ms = int((time.monotonic() - _call_t0) * 1000)

        if response is None or "_error" in response:
            if not collected_text:
                # Extract error info if available
                if response and "_error" in response:
                    error_info = response["_error"]
                    error_text = error_info.message
                    error_json = json.dumps(error_info.to_json())
                else:
                    # Fallback for backward compatibility
                    error_text = "I'm sorry, I couldn't process your message right now."
                    error_json = None

                # Save as system message with metadata
                error_msg_id = conversation.add_message(
                    conv_id, "system", error_text, content_json=error_json
                )
                return {
                    "conversation_id": conv_id,
                    "response_text": error_text,
                    "code": None,
                    "message_id": error_msg_id,
                }
            break

        # Track tokens — normalized to input_tokens/output_tokens
        usage = response.get("usage", {})
        total_tokens += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

        content = response.get("content", [])
        stop_reason = response.get("stop_reason", "end_turn")
        last_stop_reason = stop_reason  # Track for post-loop check

        # Persist API call metrics (model from response or config)
        call_model = response.get("model", "") or model_resolver.get_model_for_role("chat")
        try:
            _save_api_call(
                conv_id, call_model, usage, stop_reason,
                latency_ms=_call_latency_ms,
                arc_id=_executor_arc_id,
            )
        except (sqlite3.Error, ValueError, TypeError) as e:
            logger.warning("Failed to save API call metrics: %s", e)

        # Collect text blocks from this turn
        turn_text_parts = []
        for block in content:
            if block.get("type") == "text" and block.get("text"):
                turn_text_parts.append(block["text"])

        # If no tool_use, save final assistant message and break
        if stop_reason != "tool_use":
            final_text = "\n".join(turn_text_parts)
            collected_text.append(final_text)
            # Save final message with content_json only if structured
            has_structured = any(b.get("type") != "text" for b in content)
            cj = json.dumps(content) if has_structured else None
            last_msg_id = conversation.add_message(
                conv_id, "assistant", final_text, content_json=cj,
            )
            break

        # --- tool_use turn: persist assistant message with tool_use blocks ---
        tool_names_used = [
            b["name"] for b in content if b.get("type") == "tool_use"
        ]
        # Tool details are tracked in content_json; don't clutter chat with annotations
        summary_text = "\n".join(turn_text_parts) if turn_text_parts else ""
        assistant_content = summary_text
        if assistant_content:  # Only add to collected_text if agent said something
            collected_text.append(assistant_content)

        # Use "tool_call" role for tool-use-only turns (no user-visible text)
        # so they don't appear as empty chat bubbles.  "assistant" is reserved
        # for messages that have text the user should see.
        msg_role = "assistant" if assistant_content else "tool_call"
        assistant_msg_id = conversation.add_message(
            conv_id, msg_role, assistant_content,
            content_json=json.dumps(content),
        )

        # Execute tools with timing
        tool_result_blocks = []
        tool_result_map = {}  # tool_use_id -> result text
        tool_timing_map = {}  # tool_use_id -> duration_ms
        for block in content:
            if block.get("type") != "tool_use":
                continue
            tool_name = block["name"]
            tool_input = block["input"]
            tool_id = block["id"]

            logger.info("Chat tool call: %s(%s)", tool_name, list(tool_input.keys()))
            t_start = time.monotonic()

            # Validate before executing — gives small models actionable feedback
            validation_error = _validate_tool_call(tool_name, tool_input, tools)
            if validation_error:
                result_str = validation_error
                logger.warning("Tool validation failed: %s", validation_error)
            else:
                # Check if tool requires user confirmation
                from ..chat_tool_loader import get_loaded_tools, get_confirmation_handler
                loaded_tools = get_loaded_tools()
                tool_def = loaded_tools.get(tool_name)

                if tool_def and tool_def.requires_user_confirm:
                    confirmation_handler = get_confirmation_handler()
                    if confirmation_handler is None:
                        result_str = (
                            f"Error: Tool '{tool_name}' requires user confirmation, "
                            "but no confirmation handler is registered. This tool "
                            "cannot be executed on this platform."
                        )
                        logger.warning(
                            "Tool %s requires confirmation but no handler registered",
                            tool_name
                        )
                    else:
                        # Call confirmation handler
                        try:
                            confirmed = confirmation_handler(tool_name, tool_input)
                            if not confirmed:
                                result_str = "User declined to execute this tool."
                                logger.info("Tool %s execution declined by user", tool_name)
                            else:
                                result_str = _execute_chat_tool(
                                    tool_name, tool_input, conversation_id=conv_id,
                                    executor_arc_id=_executor_arc_id,
                                    executor_conv_id=_executor_conv_id,
                                )
                        except Exception as e:
                            result_str = f"Error during confirmation: {e}"
                            logger.error("Confirmation handler error for %s: %s", tool_name, e)
                else:
                    # No confirmation required, execute normally
                    result_str = _execute_chat_tool(
                        tool_name, tool_input, conversation_id=conv_id,
                        executor_arc_id=_executor_arc_id,
                        executor_conv_id=_executor_conv_id,
                    )
            t_end = time.monotonic()
            tool_timing_map[tool_id] = int((t_end - t_start) * 1000)

            # Truncate large tool outputs to protect the context window
            result_str = _truncate_tool_output(result_str, tool_name)

            tool_result_map[tool_id] = result_str
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": result_str,
            })

        # Persist tool_result message
        result_summary = "; ".join(
            f"{n}: {tool_result_map.get(b['id'], '')[:80]}"
            for b, n in zip(
                [bl for bl in content if bl.get("type") == "tool_use"],
                tool_names_used,
            )
        )
        conversation.add_message(
            conv_id, "tool_result", result_summary,
            content_json=json.dumps(tool_result_blocks),
        )

        # Persist to tool_calls table (non-fatal — audit record only)
        try:
            _save_tool_calls(
                conv_id, assistant_msg_id, content,
                tool_result_map, tool_timing_map,
            )
        except (sqlite3.Error, ValueError, TypeError) as _exc:
            logger.warning("Failed to save tool_calls audit record", exc_info=True)

        # Feed back into API messages for next iteration
        standard = _get_api_standard_for_client(client)
        api_messages.append(
            api_standard.format_assistant_tool_message(content, standard)
        )
        if standard == "openai":
            for result in api_standard.format_tool_results_for_api(
                tool_result_blocks, standard
            ):
                api_messages.append(result)
        else:
            api_messages.append({"role": "user", "content": tool_result_blocks})
        # Keep db_message_ids in sync (tool-loop messages have assistant_msg_id
        # for the assistant turn and None for the synthetic tool_result user turn)
        db_message_ids.append(assistant_msg_id)
        db_message_ids.append(None)

        last_msg_id = assistant_msg_id

        # Async tool short-circuit: if every tool in this turn is async
        # (results arrive later via arc.chat_notify) AND the model already
        # produced visible text alongside the tool call, skip the next API
        # call — it would only generate a redundant "fetching now..." message.
        if (
            tool_names_used
            and all(n in _ASYNC_TOOLS for n in tool_names_used)
            and msg_role == "assistant"
        ):
            logger.info(
                "Skipping post-tool API call: async tools %s with visible ack",
                tool_names_used,
            )
            # Mark as non-tool-use so the force-final-response logic below
            # doesn't make another API call.
            last_stop_reason = "end_turn"
            break

    # --- Force final response if needed ---
    # If the loop exited while still in tool_use mode, or if we collected no text,
    # make one final API call to get a summary. This ensures the user always gets
    # a response even if we hit the iteration limit.
    need_final_response = (
        last_stop_reason == "tool_use" or  # Loop exited mid-tool-use
        not collected_text  # No text was ever collected
    )

    if need_final_response:
        logger.info("Forcing final response after tool loop exit (stop_reason=%s, collected=%d)",
                    last_stop_reason, len(collected_text))

        # Add a user message requesting summary
        api_messages.append({
            "role": "user",
            "content": "Please summarize what you found and what should happen next."
        })

        try:
            final_response = _call_with_retries(
                system, api_messages,
                client=client,
                api_key=api_key,
                max_retries=mechanical_max,
                tools=None,  # Disable tools to prevent infinite loop
                operation_type="summarization",
            )

            if final_response:
                final_content = final_response.get("content", [])
                final_text_parts = [
                    b["text"] for b in final_content
                    if b.get("type") == "text" and b.get("text")
                ]
                final_text = "\n".join(final_text_parts)

                if final_text.strip():
                    collected_text.append(final_text)
                    last_msg_id = conversation.add_message(
                        conv_id, "assistant", final_text,
                    )

                    # Track tokens from final call
                    final_usage = final_response.get("usage", {})
                    total_tokens += final_usage.get("input_tokens", 0) + final_usage.get("output_tokens", 0)

                    # Log this API call too
                    final_model = final_response.get("model", "") or model_resolver.get_model_for_role("chat")
                    try:
                        _save_api_call(conv_id, final_model, final_usage, final_response.get("stop_reason"))
                    except (sqlite3.Error, ValueError, TypeError) as e:
                        logger.warning("Failed to save final API call metrics: %s", e)

        except Exception:  # broad catch: AI provider call may raise anything
            logger.exception("Failed to force final response, continuing with collected text")

    # Combine all text responses
    text = "\n".join(collected_text)
    code = api_standard.extract_code_from_text(text)

    conversation.update_token_count(conv_id, total_tokens)

    # Trigger title generation if this is the first exchange with no title
    if not has_prior_messages:
        conv_record = conversation.get_conversation(conv_id)
        if conv_record and not conv_record.get("title"):
            import threading
            threading.Thread(
                target=conversation.generate_title,
                args=(conv_id,),
                daemon=True,
            ).start()

    return {
        "conversation_id": conv_id,
        "response_text": text,
        "code": code,
        "message_id": last_msg_id,
    }


def _get_client(model_override: str | None = None):
    """Return the appropriate AI client module.

    If model_override is provided, uses its provider prefix.
    Otherwise uses the "chat" model role.

    Returns:
        Module: providers.anthropic for "anthropic", providers.ollama for "ollama",
                providers.tinfoil for "tinfoil", providers.local for "local",
                providers.chain for "chain".
    """
    if model_override and ":" in model_override:
        return model_resolver.create_client_for_model(model_override)

    provider = config.CONFIG.get("ai_provider", "anthropic")
    if provider == "chain":
        from .providers import chain as chain_client
        return chain_client
    if provider == "ollama":
        return ollama_client
    if provider == "tinfoil":
        return tinfoil_client
    if provider == "local":
        return local_client
    return claude_client


def _get_provider_for_client(client) -> str:
    """Map a client module to its provider name."""
    if client is claude_client:
        return "anthropic"
    if client is ollama_client:
        return "ollama"
    if client is tinfoil_client:
        return "tinfoil"
    if client is local_client:
        return "local"
    # Fallback: check module name
    name = getattr(client, "__name__", "")
    if "chain" in name:
        return "chain"
    if "ollama" in name:
        return "ollama"
    if "tinfoil" in name:
        return "tinfoil"
    if "local" in name:
        return "local"
    return "anthropic"


def _get_api_standard_for_client(client) -> str:
    """Resolve the API standard for a client module."""
    return api_standard.get_api_standard(_get_provider_for_client(client))


def _convert_history_to_standard(
    api_messages: list[dict],
    standard: str,
    db_message_ids: list[int | None],
) -> tuple[list[dict], list[int | None]]:
    """Convert history messages from canonical to provider-specific format.

    Messages are stored in the DB in canonical (Anthropic) format. When
    replaying history to an OpenAI-standard provider (Ollama, etc.), tool-use
    messages must be converted before the next API call.

    Handles expansion: a user message whose content is a list of tool_result
    blocks becomes multiple separate ``role: "tool"`` messages. The
    ``db_message_ids`` list is expanded in sync to keep compaction tracking
    correct (expanded slots get ``None`` IDs).

    Args:
        api_messages: Messages in canonical (Anthropic) format.
        standard: Target API standard; only ``"openai"`` triggers conversion.
        db_message_ids: Parallel DB ID list (same length as api_messages).

    Returns:
        Tuple of (converted_messages, adjusted_db_message_ids).
    """
    if standard == "anthropic":
        return api_messages, db_message_ids

    converted: list[dict] = []
    adjusted_ids: list[int | None] = []
    for msg, msg_id in zip(api_messages, db_message_ids):
        role = msg["role"]
        content = msg["content"]

        if role == "assistant" and isinstance(content, list):
            if any(b.get("type") == "tool_use" for b in content):
                converted.append(
                    api_standard.format_assistant_tool_message(content, standard)
                )
                adjusted_ids.append(msg_id)
                continue

        if role == "user" and isinstance(content, list):
            if any(b.get("type") == "tool_result" for b in content):
                tool_msgs = api_standard.format_tool_results_for_api(content, standard)
                for i, tool_msg in enumerate(tool_msgs):
                    converted.append(tool_msg)
                    adjusted_ids.append(msg_id if i == 0 else None)
                continue

        converted.append(msg)
        adjusted_ids.append(msg_id)

    return converted, adjusted_ids


def _try_local_fallback(
    system: str,
    messages: list[dict],
    *,
    operation_type: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    arc_id: int | None = None,
) -> dict | None:
    """Attempt a call to a local model as a last-resort fallback.

    Called after the main retry loop is exhausted. Uses direct httpx to
    avoid interference from client-level circuit breakers.

    Args:
        system: System prompt text.
        messages: Conversation messages in canonical (Anthropic) format.
        operation_type: The type of operation (e.g. "chat", "summarization").
        max_tokens: Maximum tokens in the response.
        temperature: Sampling temperature.
        arc_id: Optional arc ID for per-arc fallback override check.

    Returns:
        Normalized response dict on success, None on failure.
    """
    import httpx

    fb_config = config.CONFIG.get("local_fallback", {})
    if not fb_config.get("enabled", False):
        return None

    # Per-arc fallback override: check arc_state for _fallback_allowed
    if arc_id is not None:
        try:
            db = get_db()
            row = db.execute(
                "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = '_fallback_allowed'",
                (arc_id,),
            ).fetchone()
            db.close()
            if row is not None:
                import json as _json
                allowed = _json.loads(row["value_json"])
                if allowed is False:
                    logger.debug("Local fallback blocked by arc_state for arc_id=%d", arc_id)
                    return None
        except (sqlite3.Error, json.JSONDecodeError, KeyError) as _exc:
            pass  # If we can't check, fall through to config-level filtering

    fb_url = fb_config.get("url", "")
    if not fb_url:
        return None

    # Check operation type against allowed/blocked lists
    if operation_type:
        blocked = fb_config.get("blocked_operations", [])
        if operation_type in blocked:
            logger.debug("Local fallback blocked for operation_type=%s", operation_type)
            return None
        allowed = fb_config.get("allowed_operations", [])
        if allowed and operation_type not in allowed:
            logger.debug("Local fallback not allowed for operation_type=%s", operation_type)
            return None

    fb_model = fb_config.get("model", "qwen3.5:9b")
    fb_timeout = fb_config.get("timeout", 300)
    fb_max_tokens = max_tokens or fb_config.get("max_tokens", 4096)
    fb_context_window = fb_config.get("context_window", 16384)
    fallback_id = f"fallback:{fb_model}"

    logger.info("Attempting local fallback to %s at %s (operation=%s)",
                fb_model, fb_url, operation_type)

    try:
        # Convert messages from Anthropic to OpenAI format
        dummy_ids = [None] * len(messages)
        converted, _ = _convert_history_to_standard(messages, "openai", dummy_ids)

        # Build OpenAI-format messages with system prompt
        api_messages = [{"role": "system", "content": system}]
        for msg in converted:
            role = msg["role"]
            content = msg.get("content")
            entry = {"role": role}
            if role == "assistant" and "tool_calls" in msg:
                entry["content"] = content
                entry["tool_calls"] = msg["tool_calls"]
            elif role == "tool":
                entry["tool_call_id"] = msg.get("tool_call_id", "")
                entry["content"] = content if isinstance(content, str) else str(content)
            else:
                entry["content"] = content
            api_messages.append(entry)

        # Truncate to fit context window (rough estimate: 4 chars per token)
        total_chars = sum(len(str(m.get("content", ""))) for m in api_messages)
        char_limit = fb_context_window * 4
        while total_chars > char_limit and len(api_messages) > 2:
            removed = api_messages.pop(1)  # Remove oldest non-system message
            total_chars -= len(str(removed.get("content", "")))

        body = {
            "model": fb_model,
            "messages": api_messages,
            "max_tokens": fb_max_tokens,
            "temperature": temperature or 0.7,
        }

        url = f"{fb_url.rstrip('/')}/v1/chat/completions"
        body_json = json.dumps(body, ensure_ascii=False)
        body_bytes = body_json.encode("utf-8", errors="replace")
        response = httpx.post(
            url, content=body_bytes,
            headers={"Content-Type": "application/json"},
            timeout=fb_timeout,
        )
        response.raise_for_status()
        result = response.json()

        # Record success in model health
        from ..core.models import health as model_health
        model_health.record_model_call(fallback_id, success=True)

        # Normalize from OpenAI format
        normalized = api_standard.normalize_response(result, "openai")
        logger.info("Local fallback succeeded via %s", fb_model)
        return normalized

    except Exception as e:  # broad catch: local inference may raise anything
        logger.warning("Local fallback failed: %s", e)
        try:
            from ..core.models import health as model_health
            model_health.record_model_call(
                fallback_id, success=False,
                error_type=type(e).__name__,
            )
        except (ImportError, KeyError, ValueError) as _exc:
            pass
        return None


def _call_with_retries(
    system: str,
    messages: list[dict],
    *,
    client=None,
    model: str | None = None,
    api_key: str | None = None,
    max_retries: int = 4,
    max_tokens: int | None = None,
    tools: list[dict] | None = None,
    temperature: float | None = None,
    operation_type: str | None = None,
) -> dict | None:
    """Call AI API with mechanical retries for transient failures.

    Normalizes responses to canonical (Anthropic-like) format via
    ``api_standard.normalize_response`` so callers always see:
    ``content``, ``stop_reason``, ``usage.input_tokens/output_tokens``.

    Args:
        system: System prompt text.
        messages: Conversation messages.
        client: AI client module to use (defaults to _get_client()).
        model: Model string to use (defaults to config chat_model).
        api_key: API key (only used for anthropic provider).
        max_retries: Maximum number of retry attempts.
        max_tokens: Maximum tokens in the response (None = use client default).
        tools: Optional tool definitions in canonical (Anthropic) format.
        temperature: Sampling temperature (None = use client default).
        operation_type: Type of operation for local fallback routing.

    Returns the normalized API response dict, or a dict with '_error' key containing
    ErrorInfo if all retries exhausted.
    """
    if client is None:
        client = _get_client()

    provider = _get_provider_for_client(client)
    standard = api_standard.get_api_standard(provider)

    # Convert tools to provider format (chain handles conversion per-backend)
    if provider == "chain":
        provider_tools = tools
    else:
        provider_tools = api_standard.convert_tools_for_provider(tools, standard)

    # Extract bare model name from provider:model string.
    # Chain manages its own per-backend models — don't override.
    chat_model = model or model_resolver.get_model_for_role("chat")
    if chat_model and ":" in chat_model:
        _, chat_model = chat_model.split(":", 1)
    if provider == "chain":
        chat_model = None

    # Fast path: if all cloud models have open circuit breakers and we
    # have no tools, skip retries and go straight to local fallback
    if tools is None:
        try:
            from ..core.models.health import all_cloud_models_circuit_open
            if all_cloud_models_circuit_open():
                logger.info("All cloud models circuit-open, attempting fast fallback")
                fallback_result = _try_local_fallback(
                    system, messages,
                    operation_type=operation_type,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                if fallback_result is not None:
                    return fallback_result
                # If fallback fails, fall through to normal retry loop
        except Exception:  # broad catch: fallback may raise anything
            pass

    last_error_info = None
    for attempt in range(max_retries):
        try:
            kwargs = {"model": chat_model}
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens
            if provider_tools is not None:
                kwargs["tools"] = provider_tools
            if provider == "anthropic":
                kwargs["api_key"] = api_key
            if temperature is not None:
                kwargs["temperature"] = temperature
            raw = client.call(system, messages, **kwargs)
            # Chain client injects _api_standard per-backend
            resp_standard = raw.pop("_api_standard", None) or standard
            return api_standard.normalize_response(raw, resp_standard)
        except Exception as e:  # broad catch: AI provider may raise anything
            # Classify error for structured logging and user messaging
            error_info = error_classifier.classify_error(
                e,
                retry_count=attempt + 1,
                model=chat_model,
                provider=provider,
            )

            # Preserve existing 429 handling behavior
            if error_info.type == "RateLimitError":
                from . import rate_limiter as _rl
                retry_after = error_info.retry_after or 5.0
                _model = model_resolver.get_model_for_role("chat") if provider == "anthropic" else None
                _rl.record_429(retry_after, model=_model)

            # Structured logging with error type
            logger.warning(
                "AI API call failed (attempt %d/%d) [%s]: %s",
                attempt + 1, max_retries, error_info.type, e,
            )

            # Store for return on final attempt
            if attempt == max_retries - 1:
                last_error_info = error_info

            if attempt < max_retries - 1:
                wait = max(5, 2 ** attempt) if error_info.type == "RateLimitError" else 2 ** attempt
                time.sleep(wait)

    logger.error(
        "All %d retry attempts exhausted [%s]",
        max_retries,
        last_error_info.type if last_error_info else "Unknown",
    )

    # Try local fallback before giving up (tools not supported in fallback)
    if tools is None:
        fallback_result = _try_local_fallback(
            system, messages,
            operation_type=operation_type,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if fallback_result is not None:
            return fallback_result

    return {"_error": last_error_info} if last_error_info else None
