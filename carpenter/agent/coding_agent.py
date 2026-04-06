"""Built-in coding agent using tool_use loop.

A simple agent that runs in an isolated workspace using read/write/edit/list_files
tools. Uses the AI provider's messages API with tool_use for structured
tool invocations.

Coding agents do NOT have bash/shell access. They operate purely through
file read/write/edit operations and a list_files tool for workspace exploration.
This restricted execution model improves security and enables platform-agnostic
operation (e.g. Android where no shell is available).

System prompt and tool definitions are loaded from the config/prompt system
(config_seed/coding-prompts/ and config_seed/coding-tools/). Hardcoded fallbacks are
used if template files are unavailable.
"""

import logging
import os
import threading

from .. import config
from . import rate_limiter
from .providers import anthropic as claude_client

logger = logging.getLogger(__name__)

# Hardcoded fallback — used when template files are unavailable
_FALLBACK_SYSTEM_PROMPT = """\
You are a coding agent. Make the requested changes accurately and completely.

WORKSPACE: You are working in an isolated workspace — a temporary git-backed \
copy of a source directory. All file paths are RELATIVE to the workspace root.
- read_file("src/main.py") — correct
- read_file("/home/user/repos/project/src/main.py") — WRONG (will be rejected)
- Use relative paths for everything.

You do NOT have shell/bash access. Use the provided file tools to explore and \
modify the workspace.

Approach:
- For simple tasks (creating new files, writing documents): write the file \
immediately. Do NOT explore the codebase first.
- For code modifications to existing files: use list_files to explore the \
directory structure, read_file to examine files, match existing patterns, \
then make targeted edits.
- Verify by re-reading modified files, then summarize changes.
- Be efficient — make changes early, don't over-explore.

Tools:
- read_file(path): Read file relative to workspace root
- write_file(path, content): Write/create file (creates parent dirs)
- edit_file(path, old_text, new_text): Find-and-replace (old_text must match once)
- delete_file(path): Delete a file from the workspace
- list_files(path): List files and directories at the given path (defaults to root)

IMPORTANT: For write_file, always provide BOTH 'path' and 'content' parameters. \
The 'content' parameter must contain the complete file contents as a single string. \
For multi-line files, include newlines within the content string.

If modifying Carpenter platform code, read CLAUDE.md first for architecture. \
Key entry points: agent/invocation.py (chat tools), tool_backends/ (server-side \
handlers), core/ (work queue, arcs, main loop), api/ (HTTP + web UI).
"""

_FALLBACK_TOOL_DEFINITIONS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file. Path is relative to the workspace root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file to read.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates the file and parent directories if they don't exist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Find and replace text in a file. Fails if old_text is not found exactly once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file to edit.",
                },
                "old_text": {
                    "type": "string",
                    "description": "The exact text to find and replace.",
                },
                "new_text": {
                    "type": "string",
                    "description": "The replacement text.",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "delete_file",
        "description": "Delete a file from the workspace. Use this when you need to remove a file entirely (e.g. removing a tool or module).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file to delete.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": "List files and directories at the given path relative to workspace root. Returns a tree-like listing. Use '.' or omit path to list the workspace root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the directory to list. Defaults to workspace root.",
                    "default": ".",
                },
            },
            "required": [],
        },
    },
]


def _resolve_coding_prompts_dir() -> str:
    """Resolve the coding prompts directory from config."""
    coding_prompts_dir = config.CONFIG.get("coding_prompts_dir", "")
    if not coding_prompts_dir:
        base_dir = config.CONFIG.get("base_dir", "")
        if base_dir:
            coding_prompts_dir = os.path.join(base_dir, "config", "coding-prompts")
    return coding_prompts_dir


def _resolve_coding_tools_dir() -> str:
    """Resolve the coding tools directory from config."""
    coding_tools_dir = config.CONFIG.get("coding_tools_dir", "")
    if not coding_tools_dir:
        base_dir = config.CONFIG.get("base_dir", "")
        if base_dir:
            coding_tools_dir = os.path.join(base_dir, "config", "coding-tools")
    return coding_tools_dir


def _load_system_prompt() -> str:
    """Load the coding agent system prompt from template files.

    Falls back to _FALLBACK_SYSTEM_PROMPT if templates are unavailable.
    """
    coding_prompts_dir = _resolve_coding_prompts_dir()
    if coding_prompts_dir:
        try:
            from ..prompts import load_coding_prompt
            prompt = load_coding_prompt(coding_prompts_dir)
            if prompt:
                return prompt
        except Exception:
            logger.debug("Failed to load coding prompt templates, using fallback",
                        exc_info=True)
    return _FALLBACK_SYSTEM_PROMPT


def _load_tool_definitions() -> list[dict]:
    """Load coding agent tool definitions from YAML files.

    Falls back to _FALLBACK_TOOL_DEFINITIONS if YAML files are unavailable.
    """
    coding_tools_dir = _resolve_coding_tools_dir()
    if coding_tools_dir:
        try:
            from ..tool_loader import load_coding_tool_definitions
            tools = load_coding_tool_definitions(coding_tools_dir)
            if tools:
                return tools
        except Exception:
            logger.debug("Failed to load coding tool definitions, using fallback",
                        exc_info=True)
    return _FALLBACK_TOOL_DEFINITIONS


# Public aliases for backward compatibility and test access.
# These are lazily resolved; callers that need the latest value should
# call _load_system_prompt() / _load_tool_definitions() directly.
SYSTEM_PROMPT = _FALLBACK_SYSTEM_PROMPT
TOOL_DEFINITIONS = _FALLBACK_TOOL_DEFINITIONS


def _validate_path(workspace: str, rel_path: str) -> str:
    """Validate and resolve a relative path within the workspace.

    Prevents path traversal (../) outside the workspace directory.

    Returns the absolute path.

    Raises:
        ValueError: If path escapes workspace.
    """
    abs_path = os.path.realpath(os.path.join(workspace, rel_path))
    ws_real = os.path.realpath(workspace)
    if not abs_path.startswith(ws_real + os.sep) and abs_path != ws_real:
        raise ValueError(f"Path escapes workspace: {rel_path}")
    return abs_path


def _exec_read_file(workspace: str, params: dict) -> str:
    """Execute read_file tool."""
    path = _validate_path(workspace, params["path"])
    if not os.path.isfile(path):
        return f"Error: File not found: {params['path']}"
    with open(path) as f:
        return f.read()


def _exec_write_file(workspace: str, params: dict) -> str:
    """Execute write_file tool."""
    path = _validate_path(workspace, params["path"])
    content = params.get("content")
    if content is None:
        return "Error: 'content' parameter is required for write_file. Call write_file with both 'path' and 'content' parameters."
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return f"Written: {params['path']} ({len(content)} bytes)"


def _exec_edit_file(workspace: str, params: dict) -> str:
    """Execute edit_file tool."""
    path = _validate_path(workspace, params["path"])
    if not os.path.isfile(path):
        return f"Error: File not found: {params['path']}"
    with open(path) as f:
        content = f.read()
    old_text = params["old_text"]
    count = content.count(old_text)
    if count == 0:
        return f"Error: old_text not found in {params['path']}"
    if count > 1:
        return f"Error: old_text found {count} times in {params['path']} (must be unique)"
    new_content = content.replace(old_text, params["new_text"], 1)
    with open(path, "w") as f:
        f.write(new_content)
    return f"Edited: {params['path']}"


def _exec_delete_file(workspace: str, params: dict) -> str:
    """Execute delete_file tool."""
    path = _validate_path(workspace, params["path"])
    if not os.path.exists(path):
        return f"Error: File not found: {params['path']}"
    if os.path.isdir(path):
        return f"Error: Cannot delete a directory: {params['path']} (only files can be deleted)"
    os.remove(path)
    return f"Deleted: {params['path']}"


def _exec_list_files(workspace: str, params: dict) -> str:
    """Execute list_files tool — list directory contents without shell access."""
    rel_path = params.get("path", ".")
    try:
        abs_path = _validate_path(workspace, rel_path)
    except ValueError as e:
        return f"Error: {e}"
    if not os.path.isdir(abs_path):
        return f"Error: Not a directory: {rel_path}"
    try:
        entries = sorted(os.listdir(abs_path))
    except PermissionError:
        return f"Error: Permission denied: {rel_path}"
    if not entries:
        return "(empty directory)"
    lines = []
    for entry in entries:
        full = os.path.join(abs_path, entry)
        if os.path.isdir(full):
            lines.append(f"{entry}/")
        else:
            lines.append(entry)
    return "\n".join(lines)


_TOOL_HANDLERS = {
    "read_file": _exec_read_file,
    "write_file": _exec_write_file,
    "edit_file": _exec_edit_file,
    "delete_file": _exec_delete_file,
    "list_files": _exec_list_files,
}

# Shutdown flag — set to stop the coding agent between iterations
_shutdown = threading.Event()


def _execute_tool(workspace: str, tool_name: str, tool_input: dict) -> str:
    """Execute a tool call and return the result string."""
    handler = _TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return f"Error: Unknown tool: {tool_name}"
    try:
        return handler(workspace, tool_input)
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:  # broad catch: tool handler may raise anything
        return f"Error executing {tool_name}: {e}"


def run(workspace: str, prompt: str, profile: dict) -> dict:
    """Run the built-in coding agent in a workspace.

    Args:
        workspace: Absolute path to the workspace directory.
        prompt: The user's coding instruction.
        profile: Agent profile dict from config (type, model, max_tokens, etc.)

    Returns:
        dict with keys: stdout (str), exit_code (int), iterations (int)
    """
    model = profile.get("model") or None
    max_tokens = profile.get("max_tokens", claude_client.DEFAULT_MAX_TOKENS)
    max_iterations = profile.get("max_iterations", 20)

    from . import invocation

    # Resolve client from model string (or auto-detect from ai_provider)
    client = invocation._get_client(model)

    messages = [{"role": "user", "content": prompt}]
    iterations = 0
    collected_text = []
    consecutive_text_only = 0  # Track iterations with no tool use
    read_only_iterations = 0  # Track iterations with only read/list tools
    files_modified = False  # Track whether any write/edit was performed
    nudge_count = 0  # How many times we've nudged the agent to write
    MAX_NUDGES = 2
    EXPLORATION_NUDGE_THRESHOLD = 8  # Nudge after this many read-only iters
    import time as _time

    start_time = _time.monotonic()
    overall_timeout = profile.get("timeout", 300)  # 5 min default

    while iterations < max_iterations:
        if _shutdown.is_set():
            return {
                "stdout": "Interrupted by shutdown\n" + "\n".join(collected_text),
                "exit_code": 1,
                "iterations": iterations,
            }

        # Overall timeout check
        elapsed = _time.monotonic() - start_time
        if elapsed > overall_timeout:
            logger.warning("Coding agent timed out after %.0fs", elapsed)
            return {
                "stdout": "Overall timeout exceeded\n" + "\n".join(collected_text),
                "exit_code": 1,
                "iterations": iterations,
            }

        iterations += 1
        logger.info("Coding agent iteration %d/%d (%.0fs elapsed)",
                     iterations, max_iterations, elapsed)

        # Proactive rate limiting — block until a slot is available
        if not rate_limiter.acquire(model=model):
            return {
                "stdout": "Rate limiter timed out\n" + "\n".join(collected_text),
                "exit_code": 1,
                "iterations": iterations,
            }

        if _shutdown.is_set():
            return {
                "stdout": "Interrupted by shutdown\n" + "\n".join(collected_text),
                "exit_code": 1,
                "iterations": iterations,
            }

        # Resolve system prompt: profile override > template files > fallback
        system_prompt = profile.get("system_prompt") or _load_system_prompt()
        # Resolve tool definitions: template files > fallback
        tool_defs = _load_tool_definitions()
        result = invocation._call_with_retries(
            system_prompt, messages,
            client=client,
            model=model,
            max_tokens=max_tokens,
            max_retries=4,
            tools=tool_defs,
            temperature=0.3,
        )

        if result is None:
            return {
                "stdout": "All retries exhausted\n" + "\n".join(collected_text),
                "exit_code": 1,
                "iterations": iterations,
            }

        # Log token usage and feed back to rate limiter
        usage = result.get("usage", {})
        input_tokens = usage.get("input_tokens", 0)
        logger.info(
            "Coding agent API: input_tokens=%d, output_tokens=%d",
            input_tokens, usage.get("output_tokens", 0),
        )
        rate_limiter.record(input_tokens, model=model)

        # Process response content blocks
        content = result.get("content", [])
        stop_reason = result.get("stop_reason", "end_turn")

        # Collect text blocks
        for block in content:
            if block.get("type") == "text":
                collected_text.append(block["text"])

        # If no tool use, check whether anything was written
        if stop_reason == "end_turn":
            if not files_modified and nudge_count < MAX_NUDGES:
                # Agent stopped without writing — nudge it to act
                nudge_count += 1
                logger.info(
                    "Coding agent ended without writing (nudge %d/%d)",
                    nudge_count, MAX_NUDGES,
                )
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": (
                    "You have not written or edited any files yet. "
                    "Do NOT just describe what to do — you MUST use "
                    "write_file, edit_file, or delete_file to implement "
                    "the requested changes now."
                )})
                continue
            break

        # Handle tool_use blocks
        tool_results = []
        for block in content:
            if block.get("type") == "tool_use":
                tool_name = block["name"]
                tool_input = block["input"]
                tool_id = block["id"]

                logger.info(
                    "Coding agent tool call [iter=%d]: %s(%s)",
                    iterations, tool_name, list(tool_input.keys()),
                )

                if tool_name in ("write_file", "edit_file", "delete_file"):
                    files_modified = True

                tool_result = _execute_tool(workspace, tool_name, tool_input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": tool_result,
                })

        if tool_results:
            # Add assistant response and tool results to messages
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": tool_results})
            consecutive_text_only = 0

            # Track read-only iterations and nudge if exploring too long
            if not files_modified:
                read_only_iterations += 1
                if (read_only_iterations == EXPLORATION_NUDGE_THRESHOLD
                        and nudge_count < MAX_NUDGES):
                    nudge_count += 1
                    logger.info(
                        "Coding agent has %d read-only iterations, nudging to act",
                        read_only_iterations,
                    )
                    messages.append({"role": "user", "content": (
                        "You have spent many iterations reading files without "
                        "making changes. Stop exploring and ACT NOW. Use "
                        "write_file to create files, edit_file to modify them, "
                        "or delete_file to remove them. Make the requested "
                        "changes immediately."
                    )})
        else:
            consecutive_text_only += 1
            if consecutive_text_only >= 2:
                logger.info("Coding agent stopped: %d text-only iterations",
                            consecutive_text_only)
                break

    elapsed = _time.monotonic() - start_time
    final_text = "\n".join(collected_text)
    logger.info("Built-in coding agent completed in %d iterations (%.0fs)",
                iterations, elapsed)
    return {
        "stdout": final_text,
        "exit_code": 0,
        "iterations": iterations,
    }
