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

from .. import config, constants
from ..core import code_manager
from ..db import get_db, db_connection, db_transaction
from . import templates, conversation, model_resolver, api_standard, error_classifier
from .providers import anthropic as claude_client, ollama as ollama_client, tinfoil as tinfoil_client

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
    except Exception:
        logger.warning("KB search failed (search backend may be unavailable)", exc_info=True)
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
    """Select chat tools based on context budget.

    Tool definitions come from user-configurable Python modules loaded
    by chat_tool_loader.

    Always-available (core) tools are always present. Other tools are
    selected in deterministic alphabetical order by name until the
    budget is exhausted, and the final ordering is also alphabetical.

    Deterministic ordering matters for prompt caching: the tool list is
    part of the cached prefix on the Anthropic API. Sorting tools by
    usage frequency caused the cached prefix to drift as 30-day stats
    rotated, invalidating the cache on every turn. Alphabetical order
    is stable across turns and across processes.

    Returns the selected tool definitions, sorted alphabetically by name.
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

    # Sort everything alphabetically up front for stable cache hits
    tool_defs_sorted = sorted(tool_defs, key=lambda t: t["name"])

    if max_tools >= total_count:
        # All tools fit
        return tool_defs_sorted

    # Separate core and non-core (both already alphabetical)
    core_tools = [t for t in tool_defs_sorted if t["name"] in _CORE_TOOLS]
    non_core_tools = [t for t in tool_defs_sorted if t["name"] not in _CORE_TOOLS]

    remaining_slots = max_tools - len(core_tools)
    # Take the alphabetically-first non-core tools as a deterministic subset.
    # Prior implementation used 30-day usage frequency, but that rotated the
    # tool list across turns and invalidated the prompt cache. If a different
    # selection policy is desired, it should be configured (allowlist) rather
    # than computed from runtime state.
    selected_non_core = non_core_tools[:remaining_slots] if remaining_slots > 0 else []

    return sorted(core_tools + selected_non_core, key=lambda t: t["name"])


def _maybe_add_reviewer_emit_tool(
    tools: list[dict], executor_arc_id: int | None,
) -> list[dict]:
    """Add the ``submit_extract`` tool def iff the executing arc is a REVIEWER.

    ``submit_extract`` lets a REVIEWER arc persist its typed extract by
    supplying field VALUES as a structured argument (no code, no
    dispatch()).  It is REVIEWER-arc-scoped, so it is NOT
    ``always_available`` and is intentionally absent from the normal
    chat agent's tool set (I10).  We resolve the caller arc's
    ``agent_type`` and inject the API def only for REVIEWER arcs, keeping
    the prompt cache stable for every other agent type.

    Returns ``tools`` unchanged for non-REVIEWER arcs or when the agent
    type cannot be resolved (fail-closed: do not offer the tool).
    """
    if executor_arc_id is None:
        return tools
    try:
        from ..core.arcs import manager as _am
        arc_info = _am.get_arc(executor_arc_id)
    except Exception:  # noqa: BLE001 — DB lookup; fail-closed (don't offer)
        logger.debug(
            "Could not resolve arc %s agent_type for submit_extract",
            executor_arc_id, exc_info=True,
        )
        return tools
    if not arc_info or arc_info.get("agent_type") != "REVIEWER":
        return tools
    if any(t.get("name") == "submit_extract" for t in tools):
        return tools

    from ..chat_tool_loader import get_loaded_tools
    emit = get_loaded_tools().get("submit_extract")
    if emit is None:
        logger.warning(
            "submit_extract tool def not loaded; REVIEWER arc %s cannot "
            "use the structured emit path", executor_arc_id,
        )
        return tools
    emit_def = {
        "name": emit.name,
        "description": emit.description,
        "input_schema": emit.input_schema,
    }
    return sorted([*tools, emit_def], key=lambda t: t["name"])


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
    """Build the **stable, cacheable** chat system prompt.

    This output is what occupies the ``cache_control: ephemeral`` slot on
    Anthropic's prompt cache. It MUST be invariant across turns within a
    conversation (and ideally across all chat conversations) — otherwise
    every turn pays full input cost.

    What goes here:
    - Static prompt sections (identity, security, communication style, ...)
    - KB navigation guide
    - KB root index (top-level themes — KB content changes rarely)
    - Tool count indicator (function of selected tools, alphabetically stable)
    - Language directive (config-driven)

    What does **not** go here (moved to ``_build_per_turn_context_block`` and
    appended to the live user message):

    - Current date/time. Even daily-quantized timestamps cost a cache miss
      at midnight; the per-turn slot is the right home. If the agent needs
      finer time, it can call a future ``get_current_time()`` read tool.
    - Auto-search results keyed off the latest user message.
    - "Recent Conversations" hints (rotates as new conversations land).
    - Active arcs summary (rotates as arcs change state).

    When context_budget < 16384 (small local models), uses a compact
    prompt with just identity, security, KB navigation, and tools.

    When is_arc_step is True (arc PLANNER/EXECUTOR/REVIEWER agents),
    skips sections not needed by ephemeral arc conversations: KB root
    index. (Auto-search and recent-conversations were already gated on
    is_arc_step before this refactor; they now live in the per-turn block
    which arc steps simply don't get.)

    Args:
        context_budget: Total context window in tokens.
        model_name: The model ID being used for this invocation.
        messages: Conversation messages — only used for compact-mode KB
            prepopulation on small local models, which are not on the
            Anthropic cache path. Ignored for non-compact (cached) paths
            so the cached prefix stays stable across turns.
        is_arc_step: True when building prompt for an arc step agent.
    """
    if context_budget is None:
        context_budget = _DEFAULT_CONTEXT_WINDOW
    compact = context_budget < 16384

    # Load from user-editable template files (installed by coordinator at startup).
    # Templates handle: identity, security, KB navigation, tools,
    # and KB search few-shot example (compact only).
    parts = _load_prompt_parts_from_templates(compact, model_name, is_arc_step)

    # Compact-mode KB prepopulation. This path runs on small local models
    # (e.g. Ollama) which do not benefit from Anthropic prompt caching, so
    # injecting per-turn results here does not cost cache hits. The
    # non-compact (Anthropic) path deliberately does NOT inject auto-search
    # into the cached system prompt — see _build_per_turn_context_block.
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

    # Stable: KB root index (top-level themes from the KB). KB structure
    # changes rarely (human-edited), so this stays in the cached prefix.
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

    # Stable: tool count indicator. With deterministic alphabetical tool
    # ordering (see _select_chat_tools), the count is stable across turns
    # for a given context budget.
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

    # Stable: language directive (config-driven)
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


def _build_per_turn_context_block(
    messages: list[dict] | None,
    arcs_summary: str = "",
    prior_context_tail: str = "",
) -> str:
    """Build the per-turn context block prepended to the live user message.

    This content varies turn-to-turn and so MUST NOT live in the cached
    system prompt slot — placing it there guarantees a cache miss on every
    turn, paying full input cost on Sonnet ($3/Mtok) when most of the
    prefix is identical.

    Wrapped in a clearly-delimited block so the model can identify the
    boundary between current-turn context and the user's actual message.

    Includes (when non-empty):
    - Current date and timezone (date-only — no second/minute precision).
    - Active arcs summary.
    - Prior conversation summary tail (mode-2 single-medium chats).
    - Auto-search results for the latest user message.
    - "Recent Conversations" hints for memory.

    Returns the rendered block, or an empty string if there is nothing
    to add (so callers can no-op cheaply).
    """
    sections: list[str] = []

    # Current date + timezone. Date-only is intentional: any sub-day
    # quantization pays a cache miss at the boundary (midnight, hour, etc.).
    # Day-level granularity is sufficient for chat scheduling. If finer
    # time is needed, the agent can use a `get_current_time()` read tool.
    now_local = datetime.now().astimezone()
    tz_name = now_local.strftime("%Z")
    tz_offset = now_local.strftime("%z")
    try:
        tz_iana = str(now_local.tzinfo)
    except Exception:
        tz_iana = ""
    if not tz_iana or tz_iana.startswith("UTC"):
        try:
            with open("/etc/timezone") as f:
                tz_iana = f.read().strip()
        except OSError:
            pass
    tz_display = f"{tz_name} (UTC{tz_offset[:3]}:{tz_offset[3:]})"
    if tz_iana:
        tz_display += f" — IANA: {tz_iana}"
    today_local = now_local.strftime("%Y-%m-%d")
    sections.append(
        f"## Current Date\n\n"
        f"Today (local): {today_local} ({tz_display})\n"
        f"When scheduling, use local time as a naive ISO timestamp "
        f"(e.g. '{today_local}T14:30:00') — the platform converts local "
        f"time to UTC automatically. If you need the current hour/minute, "
        f"call a clock-reading tool rather than relying on this block."
    )

    # Active arcs summary
    if arcs_summary and arcs_summary.strip():
        sections.append(f"## Active Work\n{arcs_summary}")

    # Prior conversation summary (mode-2 single-medium handoff)
    if prior_context_tail and prior_context_tail.strip():
        sections.append(f"## Prior Context (summary)\n{prior_context_tail}")

    # Auto-search keyed off the latest user message
    try:
        search_section = _auto_search_for_prompt(messages)
        if search_section:
            sections.append(search_section)
    except (ImportError, KeyError, ValueError):
        pass

    # Recent conversations memory hint
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
            sections.append("\n".join(hint_lines))
    except (sqlite3.Error, KeyError, ValueError):
        pass

    if not sections:
        return ""

    body = "\n\n".join(sections)
    return (
        "[per-turn context — not part of the user's message]\n"
        f"{body}\n"
        "[/per-turn context]"
    )


def _attach_per_turn_context(api_messages: list[dict], context_block: str) -> list[dict]:
    """Prepend ``context_block`` to the content of the last user message.

    The block is attached only to the **final** user-role message in the
    list; previous user messages remain identical to what is stored in the
    DB so the cached prefix (system + tools + history up to penultimate
    user) stays stable across turns.

    Mutates and returns ``api_messages``. If the list has no user message,
    or the context block is empty, the list is returned unchanged.
    """
    if not context_block or not api_messages:
        return api_messages

    # Find the index of the last user-role message
    last_user_idx: int | None = None
    for i in range(len(api_messages) - 1, -1, -1):
        if api_messages[i].get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        return api_messages

    target = api_messages[last_user_idx]
    content = target.get("content")
    block_dict = {"type": "text", "text": context_block}

    if isinstance(content, str):
        # Wrap into structured content so the per-turn block is a separate
        # text block prepended to the user's message text.
        target["content"] = [
            block_dict,
            {"type": "text", "text": content},
        ]
    elif isinstance(content, list):
        # Structured content (e.g. tool_result blocks). If any tool_result
        # is present the per-turn block must go AFTER existing blocks:
        # Anthropic requires the message that follows a tool_use to begin
        # with the matching tool_result, and prepending a text block here
        # triggers a 400 "tool_use ids were found without tool_result
        # blocks immediately after" error. When there's no tool_result we
        # prepend as before so the context appears before the user's text.
        has_tool_result = any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content
        )
        if has_tool_result:
            target["content"] = [*content, block_dict]
        else:
            target["content"] = [block_dict, *content]
    else:
        # Unknown shape — leave alone rather than corrupting it.
        pass

    return api_messages

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
        elif tool_name == "submit_extract":
            return _handle_submit_extract(
                tool_input, executor_arc_id=executor_arc_id,
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
        logger.exception("Chat tool %s error", tool_name)
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
        except Exception:  # broad catch: fail-open pre-check
            # Intentional swallow: pre-check is best-effort; the post-execution
            # taint check (below) is the fail-closed gate.
            logger.info("Pre-execution taint check failed; deferring to post-exec check", exc_info=True)

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
    policy_id = arc.get("model_policy_id")
    if policy_id:
        policy = _am.get_model_policy(policy_id)
        if policy:
            current_model = policy.get("model")
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

    new_policy_id = _am.get_or_create_model_policy(model=next_model)
    new_arc_id = _am.create_arc(
        name=f"{arc['name']} (escalated)",
        goal=enhanced_goal,
        parent_id=arc["parent_id"],
        step_order=arc["step_order"],
        model_policy_id=new_policy_id,
        agent_type="PLANNER",
        integrity_level=arc["integrity_level"],
        output_type=arc["output_type"],
        priority=arc["priority"],
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


# Pre-verified fetch script.  The URL and the raw output Resource's file
# path are read from arc state (set by the platform before dispatch) so
# the script body is identical for every fetch, keeping a single hash in
# verified_code_hashes.
#
# The script:
#   1. Reads fetch_url + raw_resource_path + raw_resource_id from state.
#   2. Calls web.fetch_webpage to retrieve the page HTML.
#   3. Writes the HTML to raw_resource_path (the EXECUTOR's output Resource blob).
#   4. Calls resource.finalize to populate byte_size/content_hash on the
#      Resource row.
#
# The REVIEWER then reads the raw blob by file_path, produces a cleaned
# summary, writes it to the derived Resource's file_path, and calls
# resource.finalize(..., deprecate_inputs=True).  JUDGE approval
# (resource.submit_verdict) is what promotes the derived Resource to
# trusted (via review_manager's _review_target_resource_id wiring).
_FETCH_SCRIPT = """\
from carpenter_tools.declarations import Label
url_result = dispatch(Label("state.get"), {"key": Label("fetch_url")})
url = url_result[Label("value")]
path_result = dispatch(Label("state.get"), {"key": Label("raw_resource_path")})
output_path = path_result[Label("value")]
rid_result = dispatch(Label("state.get"), {"key": Label("raw_resource_id")})
raw_resource_id = rid_result[Label("value")]
result = dispatch(Label("web.fetch_webpage"), {"url": url})
content = result[Label("content")]
dispatch(Label("files.write"), {"path": output_path, "content": content})
dispatch(Label("resource.finalize"), {"resource_id": raw_resource_id})
"""


def _handle_fetch_web_content(
    tool_input: dict,
    conversation_id: int | None = None,
) -> str:
    """Handle fetch_web_content — create an untrusted arc batch to fetch a URL.

    Creates a parent PLANNER arc with three children:
      1. EXECUTOR (untrusted) — fetches the URL using a pre-verified
         script and writes the HTML to a raw Resource blob on disk.
      2. REVIEWER (trusted) — reads the raw HTML Resource, extracts the
         user's goal-relevant info, writes a derived Resource summary
         file, and submits its verdict.
      3. JUDGE (trusted) — validates the review and (via resource.submit_verdict)
         flips the derived Resource's template_verdict, which is what
         makes the summary trusted under the Resource-provenance model.

    The parent arc completes when all children finish, triggering
    arc.chat_notify to deliver results back to the conversation.

    Resource wiring (PR3):
      - A raw html Resource is pre-created with file_path pointing at
        a deterministic location (``{storage_root}/<rid>/blob``) so the
        EXECUTOR script can write there.  produced_by_arc_id=EXECUTOR,
        produced_by_template=NULL (raw ingest is forever untrusted).
      - A derived text-summary Resource is pre-created with
        produced_by_template='html_to_summary', template_verdict='pending'.
        file_path again points at a deterministic location.
        produced_by_arc_id=REVIEWER.
      - arc_resources links: EXECUTOR -> raw (output); REVIEWER -> raw
        (input) + derived (output).
      - Parent PLANNER gets ``_primary_resource_id = <derived_id>`` in
        state so the chat notify path (PR4) can surface it.
      - Reviewer-as-JUDGE verdict wiring: the JUDGE arc's
        ``_review_target_resource_id`` is set to the derived Resource id
        so PR2's review_manager.submit_verdict side-effect flips the
        derived Resource's template_verdict on approve/reject.

    Decisions (documented here for the PR reviewer):
      - File layout: ``{resource_storage_dir}/<resource_id>/blob`` —
        one subdir per Resource so sweep-delete is ``rmtree`` of a
        single dir.  ``resource_storage_dir()`` anchors on
        ``database_path`` so Resources co-locate with the DB.
      - Resource row pre-allocation (decision (a) in the PR plan): we
        create the row BEFORE the file so the row id can be used in the
        path.  ``byte_size`` / ``content_hash`` start NULL and are
        populated by the producer via the new ``resource.finalize``
        dispatch tool after the blob is on disk.
      - Auto-deprecation: the REVIEWER calls
        ``resource.finalize(deprecate_inputs=True)`` after writing the
        derived summary, which marks the raw html Resource deprecated.
        This fires unconditionally on REVIEWER commit, BEFORE the JUDGE
        verdict — per the plan, auto-deprecation is triggered by
        "trusted arc successfully commits its output Resources", not by
        JUDGE outcome.
    """
    from ..core.arcs import manager as _am
    from ..core.engine import work_queue as _wq
    from ..core.resources import (
        create_resource as _create_resource,
        derive_resource as _derive_resource,
        link_arc_resource as _link_arc_resource,
        resource_storage_path as _resource_storage_path,
    )
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
                    "The URL and output path have been pre-set in arc state "
                    "as 'fetch_url', 'raw_resource_path', and 'raw_resource_id'."
                ),
                "parent_id": parent_id,
                "integrity_level": "untrusted",
                "output_type": "json",
                "agent_type": "EXECUTOR",
                "step_order": 0,
            },
            {
                "name": "Review fetched content",
                # Note: goal references the raw Resource's on-disk path
                # via arc state key raw_resource_path (set below) and
                # writes the derived summary to derived_resource_path.
                "goal": (
                    f"Read the untrusted html file at the path in arc state "
                    f"key 'raw_resource_path'. Extract the relevant "
                    f"information the user wanted: {goal}. Write a clean "
                    f"text summary to the path in arc state key "
                    f"'derived_resource_path', then call resource.finalize "
                    f"with the derived_resource_id from arc state and "
                    f"deprecate_inputs=True. Store the summary text in arc "
                    f"state under key '_agent_response' as well, for the "
                    f"chat notify path."
                ),
                "parent_id": parent_id,
                "agent_type": "REVIEWER",
                "integrity_level": "trusted",
                "reviewer_profile": "security-reviewer",
                "model_policy": "fast-chat",
                "step_order": 1,
            },
            {
                "name": "Validate review",
                "goal": (
                    "Validate that the reviewer's extraction is accurate "
                    "and complete. When approving, call resource.submit_verdict "
                    "with the derived_resource_id from arc state and "
                    "verdict='approved' (or 'rejected' if the content is "
                    "unsafe/incorrect). Copy the final answer to arc state "
                    "key '_agent_response'."
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
    reviewer_arc_id = child_ids[1]
    judge_arc_id = child_ids[2]

    # --- Resource wiring ---------------------------------------------------
    #
    # Raw html Resource: produced_by_arc_id=EXECUTOR, raw ingest (no
    # template).  Created first so its row id can be used to compute the
    # on-disk file_path.
    raw_resource_id = _create_resource(
        content_type="html",
        file_path=None,  # placeholder; updated after we know the id
        produced_by_arc_id=executor_arc_id,
        source_descriptor=url,
    )
    raw_path = _resource_storage_path(raw_resource_id, "blob")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    # Update the row with its final file_path.
    from ..db import db_transaction as _db_transaction
    with _db_transaction() as _db:
        _db.execute(
            "UPDATE resources SET file_path = ? WHERE id = ?",
            (str(raw_path), raw_resource_id),
        )

    # Link EXECUTOR -> raw (output role).
    _link_arc_resource(
        arc_id=executor_arc_id,
        resource_id=raw_resource_id,
        role="output",
    )

    # Derived text-summary Resource: pre-created pending so the REVIEWER
    # can file_path-write and finalize, and the JUDGE can submit verdict.
    derived_resource_id = _derive_resource(
        content_type="text-summary",
        file_path=None,  # placeholder; updated after we know the id
        produced_by_arc_id=reviewer_arc_id,
        produced_by_template="html_to_summary",
        template_verdict="pending",
        source_descriptor=url,
    )
    derived_path = _resource_storage_path(derived_resource_id, "blob")
    derived_path.parent.mkdir(parents=True, exist_ok=True)
    with _db_transaction() as _db:
        _db.execute(
            "UPDATE resources SET file_path = ? WHERE id = ?",
            (str(derived_path), derived_resource_id),
        )

    # REVIEWER links: reads raw (input), writes derived (output).
    _link_arc_resource(
        arc_id=reviewer_arc_id,
        resource_id=raw_resource_id,
        role="input",
    )
    _link_arc_resource(
        arc_id=reviewer_arc_id,
        resource_id=derived_resource_id,
        role="output",
    )

    # --- Arc state pre-seeding --------------------------------------------
    # EXECUTOR script inputs.
    set_arc_state(executor_arc_id, "fetch_url", url)
    set_arc_state(executor_arc_id, "raw_resource_path", str(raw_path))
    set_arc_state(executor_arc_id, "raw_resource_id", raw_resource_id)

    # REVIEWER reads raw + writes derived.
    set_arc_state(reviewer_arc_id, "raw_resource_path", str(raw_path))
    set_arc_state(reviewer_arc_id, "raw_resource_id", raw_resource_id)
    set_arc_state(reviewer_arc_id, "derived_resource_path", str(derived_path))
    set_arc_state(reviewer_arc_id, "derived_resource_id", derived_resource_id)

    # JUDGE arc: seed _review_target_resource_id so PR2's verdict wiring
    # flips the derived Resource's template_verdict on approve/reject.
    set_arc_state(judge_arc_id, "_review_target_resource_id", derived_resource_id)
    set_arc_state(judge_arc_id, "derived_resource_id", derived_resource_id)

    # Parent PLANNER knows its primary Resource — PR4's chat notify path
    # will check trust dynamically at delivery time.
    set_arc_state(parent_id, "_primary_resource_id", derived_resource_id)

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


def _handle_submit_extract(
    tool_input: dict,
    executor_arc_id: int | None = None,
) -> str:
    """Handle ``submit_extract`` — the REVIEWER's structured emit path.

    A REVIEWER arc-step agent calls this tool to persist its typed
    extract.  The LLM supplies the extracted DATA as a structured tool
    argument (``fields``, a JSON object of the dataclass field values);
    it never writes code and never calls ``dispatch()``.  Data crossing
    the tool boundary as an argument needs no code verification — only
    the values are untrusted, and the JUDGE is the gate that decides
    whether they graduate.

    Trust model (why this stays inside the boundary):

      * **Caller-scoped write only.**  The handler reads the REVIEWER
        arc's *own* pre-created pending extract Resource id from its arc
        state (``extract_resource_id``, seeded by the template builder)
        and persists via ``resource.handle_write`` with
        ``_caller_arc_id = executor_arc_id``.  ``handle_write`` enforces
        ``caller_arc_id == produced_by_arc_id`` — so a REVIEWER can only
        ever write the one Resource it produces.  It cannot target an
        arbitrary Resource id (the id is NOT a tool argument).
      * **No self-approval.**  ``handle_write`` writes the blob + stats
        only; it never touches ``produced_by_template`` or
        ``template_verdict``.  The Resource stays ``pending`` and the
        deterministic JUDGE remains the sole authority that flips it to
        approved.  This tool exposes no verdict surface.
      * **Arc-only.**  With no ``executor_arc_id`` (the normal chat
        agent has none) the tool refuses — it is not a general chat
        tool and respects I10.  It is offered only to REVIEWER arc-step
        agents (see ``invoke_for_chat``).

    Params:
        fields: required — a JSON object (dict) of the extract's typed
            field values.  Written verbatim as the Resource blob (JSON)
            via ``resource.write``; the JUDGE-dispatch deserialiser
            decodes it back into the dataclass named by the Resource's
            ``kind`` and validates it.
    """
    from ..core.workflows._arc_state import get_arc_state
    from ..tool_backends import resource as resource_backend

    if executor_arc_id is None:
        return (
            "Error: submit_extract is only callable by a REVIEWER arc-step "
            "agent (no arc context present)."
        )

    fields = tool_input.get("fields")
    if not isinstance(fields, dict):
        return (
            "Error: submit_extract requires a 'fields' object (a JSON "
            "dict of the extract's typed field values)."
        )

    extract_resource_id = get_arc_state(executor_arc_id, "extract_resource_id")
    if extract_resource_id is None:
        return (
            "Error: this arc has no pre-created extract Resource "
            "(arc state key 'extract_resource_id' is unset). submit_extract "
            "can only persist a template-created pending extract Resource."
        )

    try:
        result = resource_backend.handle_write({
            "resource_id": int(extract_resource_id),
            "content": fields,
            # Mirror the historical REVIEWER submit_code path: retire the
            # raw/briefing inputs the REVIEWER consumed once its derived
            # output is committed.
            "deprecate_inputs": True,
            "_caller_arc_id": executor_arc_id,
        })
    except PermissionError as exc:
        # caller != producer — the only Resource this arc may write is its
        # own pending extract; refuse anything else.
        logger.warning("submit_extract permission denied: %s", exc)
        return f"Error: submit_extract refused — {exc}"
    except (ValueError, OSError) as exc:
        logger.warning("submit_extract write failed: %s", exc)
        return f"Error: submit_extract failed to persist extract — {exc}"

    return (
        f"Extract persisted to Resource #{result['resource_id']} "
        f"({result['byte_size']} bytes). The JUDGE will validate it; you "
        "do not approve it yourself. Your work is done — exit."
    )


def _save_api_call(
    conv_id: int | None,
    model: str,
    usage: dict,
    stop_reason: str | None = None,
    latency_ms: int | None = None,
    arc_id: int | None = None,
):
    """Persist API call metrics (tokens, cache stats) to the api_calls table.

    Args:
        conv_id: Conversation ID, or None for calls made outside a
            conversation (e.g. arc-only coding-agent iterations).
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
    """Build a summary of active arcs, highlighting conversation-specific ones.

    Also surfaces recently finished arcs that belong to this conversation so
    the chat agent can answer "what have you been up to?" without forgetting
    earlier steps once the most recent system completion notification has
    rolled past in the message history.  The recap is bounded so the cached
    prefix stays small, and structurally lists each arc so the agent has a
    direct, enumerable handle for follow-up tool calls (list_arcs,
    get_arc_detail, read_arc_result).
    """
    from ..core.arcs import manager as arc_manager  # noqa: F401  (kept for parity)
    conv_set = set(conv_arc_ids)
    recap_limit = config.CONFIG.get("chat_arcs_summary_recent_limit", 10)

    with db_connection() as db:
        active_rows = db.execute(
            "SELECT id, name, status, goal FROM arcs "
            "WHERE status IN ('active', 'waiting', 'pending') "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()

        # Recently finished arcs from THIS conversation (and their children).
        # Children are pulled in by parent_id so a multi-step workflow created
        # from a single user request shows up even when only the parent arc
        # is registered in conversation_arcs.
        recent_finished_rows: list = []
        if conv_set:
            placeholders = ",".join("?" for _ in conv_set)
            params = list(conv_set) * 2 + [recap_limit]
            recent_finished_rows = db.execute(
                f"SELECT id, name, status, goal, parent_id FROM arcs "
                f"WHERE status IN ('completed', 'failed', 'cancelled') "
                f"AND (id IN ({placeholders}) "
                f"OR parent_id IN ({placeholders})) "
                f"ORDER BY id DESC LIMIT ?",
                tuple(params),
            ).fetchall()

    lines: list[str] = []
    if active_rows:
        for r in active_rows:
            goal = (r["goal"] or "")[:80]
            marker = " [this conversation]" if r["id"] in conv_set else ""
            lines.append(
                f"#{r['id']} [{r['status']}] {r['name']}: {goal}{marker}"
            )
    else:
        lines.append("No active arcs.")

    if recent_finished_rows:
        lines.append("")
        lines.append(
            "Recently finished arcs from this conversation "
            "(use get_arc_detail/read_arc_result for full content). "
            "When the user asks what you've been working on, an audit "
            "trail, or a recap, enumerate EVERY item below — do not "
            "summarise only the most recent one:"
        )
        for r in recent_finished_rows:
            goal = (r["goal"] or "")[:80]
            lines.append(f"#{r['id']} [{r['status']}] {r['name']}: {goal}")

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

    # Build stable system prompt (reuse chat template logic). Per-turn
    # context lives on the user message, not in the cached system slot.
    conv_arc_ids = conversation.get_conversation_arc_ids(conversation_id)
    arcs_summary = _build_arcs_summary(conv_arc_ids)
    system = templates.render(
        "chat_new",
        system_prompt=_build_chat_system_prompt(),
    )

    # Get messages
    messages = conversation.get_messages(conversation_id)
    api_messages = conversation.format_messages_for_api(messages)

    # Convert history to provider format (same fix as invoke_chat)
    _esc_standard = _get_api_standard_for_client(client)
    api_messages, _ = _convert_history_to_standard(
        api_messages, _esc_standard, [None] * len(api_messages)
    )

    # Attach per-turn context block to the last user message
    per_turn_block = _build_per_turn_context_block(
        messages=api_messages,
        arcs_summary=arcs_summary,
        prior_context_tail="",
    )
    if per_turn_block:
        api_messages = _attach_per_turn_context(api_messages, per_turn_block)

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
    1. Exact model string match in context_windows config (e.g. "ollama:qwen3.5:9b")
    2. Provider prefix match (e.g. "ollama")
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
                        f"{role}: [tool_result: {str(block.get('content', ''))[:constants.LOG_PREVIEW_TRUNCATION]}]"
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


def _dispatch_chat_tool_call(
    tool_name: str,
    tool_input: dict,
    tools: list[dict],
    *,
    conv_id: int,
    executor_arc_id: int | None,
    executor_conv_id: int | None,
) -> str:
    """Validate, confirm if required, and execute one chat-tool call.

    Returns the result string to feed back to the model. Centralizes
    the dispatch logic that was previously inlined six levels deep
    inside the chat-tool loop in :func:`invoke_for_chat`.

    Args:
        tool_name: Name of the tool to invoke.
        tool_input: Argument dict from the model's ``tool_use`` block.
        tools: Currently advertised tool defs (used for shape validation).
        conv_id: Conversation id (forwarded to ``_execute_chat_tool``).
        executor_arc_id: Optional executor arc id forwarded for arc-step
            tool calls.
        executor_conv_id: Optional executor conversation id forwarded
            for arc-step tool calls.
    """
    # Validate before executing — gives small models actionable feedback.
    validation_error = _validate_tool_call(tool_name, tool_input, tools)
    if validation_error:
        logger.warning("Tool validation failed: %s", validation_error)
        return validation_error

    # Check if tool requires user confirmation.
    from ..chat_tool_loader import get_loaded_tools, get_confirmation_handler
    loaded_tools = get_loaded_tools()
    tool_def = loaded_tools.get(tool_name)

    if not (tool_def and tool_def.requires_user_confirm):
        # No confirmation required, execute normally.
        return _execute_chat_tool(
            tool_name, tool_input, conversation_id=conv_id,
            executor_arc_id=executor_arc_id,
            executor_conv_id=executor_conv_id,
        )

    confirmation_handler = get_confirmation_handler()
    if confirmation_handler is None:
        logger.warning(
            "Tool %s requires confirmation but no handler registered",
            tool_name,
        )
        return (
            f"Error: Tool '{tool_name}' requires user confirmation, "
            "but no confirmation handler is registered. This tool "
            "cannot be executed on this platform."
        )

    try:
        confirmed = confirmation_handler(tool_name, tool_input)
    except Exception as e:
        logger.exception("Confirmation handler error for %s", tool_name)
        return f"Error during confirmation: {e}"

    if not confirmed:
        logger.info("Tool %s execution declined by user", tool_name)
        return "User declined to execute this tool."

    return _execute_chat_tool(
        tool_name, tool_input, conversation_id=conv_id,
        executor_arc_id=executor_arc_id,
        executor_conv_id=executor_conv_id,
    )


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

    # Resolve context window for the active model.
    # Priority: explicit _model_override kwarg → per-conversation pin →
    # global model_roles/ai_provider default.
    if _model_override is None:
        try:
            _conv_pin = conversation.get_conversation_model_override(conv_id)
        except Exception:  # broad catch: DB query, degrade to default
            logger.exception(
                "Failed to read conversation model override for conv %s", conv_id
            )
            _conv_pin = None
        if _conv_pin:
            _model_override = _conv_pin
            logger.info(
                "Using per-conversation model pin for conv %s: %s",
                conv_id, _conv_pin,
            )
    _chat_model = _model_override or model_resolver.get_model_for_role("chat")
    context_window = _get_context_window(_chat_model)

    # Build the **stable** system prompt from template. Per-turn dynamic
    # context (date, auto-search, recent conversations, active arcs, prior
    # summary) is intentionally NOT included here — it would invalidate the
    # Anthropic prompt cache on every turn. Instead we attach those bits to
    # the live user message via _build_per_turn_context_block below.
    system = templates.render(
        template_name,
        system_prompt=_build_chat_system_prompt(
            context_budget=context_window, model_name=_chat_model,
            messages=None,  # do not feed user messages into cached prefix
            is_arc_step=_is_arc,
        ),
    )

    client = _get_client(_model_override)

    # Convert history messages to provider-specific format.
    # For chain provider this is now a no-op (standard="anthropic"),
    # and chain_client converts per-backend as needed.
    _hist_standard = _get_api_standard_for_client(client)
    api_messages, db_message_ids = _convert_history_to_standard(
        api_messages, _hist_standard, db_message_ids
    )

    # Build and attach the per-turn context block to the last user message.
    # Arc step agents don't get this block — their conversations are
    # ephemeral and the platform supplies arc context separately.
    if not _is_arc:
        per_turn_block = _build_per_turn_context_block(
            messages=api_messages,
            arcs_summary=arcs_summary,
            prior_context_tail=prior_text,
        )
        if per_turn_block:
            api_messages = _attach_per_turn_context(api_messages, per_turn_block)

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

    # REVIEWER arc-step agents get the structured `submit_extract` emit
    # tool. It is NOT always_available (it must not be offered to the
    # normal chat agent — it is a scoped write of the caller arc's own
    # pending Resource, see I10), so `_select_chat_tools` may not include
    # it under budget pressure. Inject it explicitly for REVIEWER arcs so
    # the emit path is reliable-by-default rather than fragile
    # code-generation via submit_code.
    if _is_arc:
        tools = _maybe_add_reviewer_emit_tool(tools, _executor_arc_id)

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

            result_str = _dispatch_chat_tool_call(
                tool_name, tool_input, tools,
                conv_id=conv_id,
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
                providers.tinfoil for "tinfoil", providers.chain for "chain".
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
    return claude_client


def _get_provider_for_client(client) -> str:
    """Map a client module to its provider name."""
    if client is claude_client:
        return "anthropic"
    if client is ollama_client:
        return "ollama"
    if client is tinfoil_client:
        return "tinfoil"
    # Fallback: check module name
    name = getattr(client, "__name__", "")
    if "chain" in name:
        return "chain"
    if "ollama" in name:
        return "ollama"
    if "tinfoil" in name:
        return "tinfoil"
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
        operation_type: Type of operation (kept for model health classification).

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
                exc_info=True,
            )

            # Budget kill-switch is fatal — stop retrying immediately.
            if error_info.type == "BudgetExceededError":
                last_error_info = error_info
                break

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

    return {"_error": last_error_info} if last_error_info else None
