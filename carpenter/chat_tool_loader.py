"""Chat tool loader — imports user-configurable Python modules with @chat_tool decorators.

Provides:
  - ``@chat_tool`` decorator for declaring tool metadata at the function definition
  - ``LoadedTool`` dataclass for runtime tool state
  - Module importer that collects decorated functions from ``config/chat_tools/``
  - Hot-reload via heartbeat hook (mtime polling)
  - ``install_chat_tool_defaults()`` to seed user config from ``config_seed/chat_tools/``

Platform tools (submit_code, escalate, escalate_current_arc) are injected separately
and cannot be overridden by user config modules.
"""

import importlib
import importlib.util
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# Seed defaults ship in config_seed/chat_tools/ at repo root
_CHAT_TOOL_DEFAULTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config_seed", "chat_tools",
)

# ── Capability vocabulary (single source of truth in chat_tool_registry) ──
from .chat_tool_registry import (
    READ_CAPABILITIES,
    WRITE_CAPABILITIES,
    VALID_CAPABILITIES,
    _VALID_BOUNDARIES,
)

# Platform tool definitions — schemas for tools handled inline in invocation.py.
# These are injected into _loaded_tools so they appear in get_tool_defs_for_api().
_PLATFORM_TOOL_DEFS = [
    {
        "name": "submit_code",
        "description": (
            "Submit Python code for security review and execution. ALL actions "
            "(file writes, state changes, web requests, arc management, git "
            "operations) must go through this tool. IMPORTANT: Code runs in an "
            "isolated executor subprocess that can ONLY import from "
            "carpenter_tools.* (NOT from carpenter.* platform internals). Code is "
            "sanitized and reviewed before execution. If you resubmit identical "
            "previously-approved code, review is skipped."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "Python code to execute. ONLY import from carpenter_tools.* "
                        "(act/read modules). DO NOT import carpenter.*."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Brief description of what the code does (for audit log).",
                },
            },
            "required": ["code", "description"],
        },
        "trust_boundary": "platform",
        "capabilities": ["filesystem_write", "database_write", "external_effect"],
        "always_available": True,
    },
    {
        "name": "escalate_current_arc",
        "description": (
            "Request escalation to a more powerful AI model for the current task. "
            "Use RARELY and only when genuinely uncertain about ability to succeed "
            "(complex reasoning, large refactoring, nuanced writing). Requires user "
            "approval by default. NEVER suggest disabling confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Clear explanation why escalation needed (shown to user).",
                },
                "task_type": {
                    "type": "string",
                    "enum": ["coding", "writing", "general"],
                    "description": "Task type for escalation stack selection.",
                },
            },
            "required": ["reason", "task_type"],
        },
        "trust_boundary": "platform",
        "capabilities": ["database_write"],
        "always_available": False,
    },
    {
        "name": "escalate",
        "description": (
            "Self-escalate: freeze this arc and create a stronger sibling to take "
            "over. The new arc gets full read access to this arc's subtree. Use "
            "when you determine the current model cannot complete the task. No "
            "parameters needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "trust_boundary": "platform",
        "capabilities": ["arc_create", "database_write"],
        "always_available": False,
    },
    {
        "name": "fetch_web_content",
        "description": (
            "Fetch content from a URL. Creates an untrusted arc batch that "
            "fetches, reviews, and validates the content, then reports the "
            "result back to this conversation. Use whenever the user wants "
            "information from a website, API, or any external URL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch content from.",
                },
                "goal": {
                    "type": "string",
                    "description": (
                        "What to extract or summarize from the fetched content. "
                        "Be specific about what information the user needs."
                    ),
                },
            },
            "required": ["url", "goal"],
        },
        "trust_boundary": "platform",
        "capabilities": ["arc_create", "database_write", "external_effect"],
        "always_available": True,
    },
]


# ── Decorator ──────────────────────────────────────────────────────

def chat_tool(
    description: str,
    input_schema: dict,
    capabilities: list[str] | None = None,
    trust_boundary: str = "chat",
    always_available: bool = False,
    requires_user_confirm: bool = False,
) -> Callable:
    """Decorator that registers a function as a chat tool.

    The function name becomes the tool name.
    Metadata is attached as ``_chat_tool_meta`` attribute.

    Args:
        description: Human-readable tool description for the AI.
        input_schema: JSON Schema for the tool's input parameters.
        capabilities: List of capability strings (default: ["pure"]).
        trust_boundary: "chat" (default) or "platform".
        always_available: If True, tool is always offered to the agent.
        requires_user_confirm: If True, platform must confirm before execution.
    """
    if capabilities is None:
        capabilities = ["pure"]

    # Validate capability strings at decoration time (fail-fast)
    for cap in capabilities:
        if cap not in VALID_CAPABILITIES:
            raise ValueError(
                f"Unknown capability {cap!r} in @chat_tool decorator. "
                f"Valid: {sorted(VALID_CAPABILITIES)}"
            )

    # Validate "pure" not mixed with others
    if "pure" in capabilities and len(capabilities) > 1:
        raise ValueError(
            f"Capability 'pure' cannot be mixed with other capabilities: {capabilities}"
        )

    if trust_boundary not in _VALID_BOUNDARIES:
        raise ValueError(
            f"Invalid trust_boundary {trust_boundary!r}. Valid: {_VALID_BOUNDARIES}"
        )

    def decorator(func: Callable) -> Callable:
        func._chat_tool_meta = {
            "name": func.__name__,
            "description": description,
            "input_schema": input_schema,
            "capabilities": list(capabilities),
            "trust_boundary": trust_boundary,
            "always_available": always_available,
            "requires_user_confirm": requires_user_confirm,
        }
        return func
    return decorator


# ── LoadedTool dataclass ───────────────────────────────────────────

@dataclass
class LoadedTool:
    """Runtime representation of a loaded chat tool."""

    name: str
    description: str
    input_schema: dict
    trust_boundary: str
    capabilities: list[str]
    always_available: bool
    handler: Callable
    requires_user_confirm: bool = False

    @property
    def is_read_only(self) -> bool:
        return all(c in READ_CAPABILITIES for c in self.capabilities)


# ── Module-level state ─────────────────────────────────────────────

_loaded_tools: dict[str, LoadedTool] = {}
_mtimes: dict[str, float] = {}
_chat_tools_dir: str = ""

# Platform-provided confirmation handler for tools requiring user confirmation.
# Set via set_confirmation_handler() at platform startup.
_confirmation_handler: Callable[[str, dict], bool] | None = None


# ── Install defaults ───────────────────────────────────────────────

def install_chat_tool_defaults(chat_tools_dir: str) -> dict:
    """Copy config_seed/chat_tools/ to user dir on first install.

    Returns:
        {"status": "installed"|"exists"|"no_defaults", "copied": int}
    """
    if os.path.isdir(chat_tools_dir):
        return {"status": "exists", "copied": 0}

    if not os.path.isdir(_CHAT_TOOL_DEFAULTS_DIR):
        logger.warning("Chat tool defaults directory not found: %s", _CHAT_TOOL_DEFAULTS_DIR)
        return {"status": "no_defaults", "copied": 0}

    try:
        shutil.copytree(_CHAT_TOOL_DEFAULTS_DIR, chat_tools_dir)
        count = sum(1 for _ in Path(chat_tools_dir).glob("*.py") if _.name != "__init__.py")
        logger.info("Installed chat tool defaults: %d files to %s", count, chat_tools_dir)
        return {"status": "installed", "copied": count}
    except OSError as e:
        logger.error("Failed to install chat tool defaults: %s", e)
        return {"status": "error", "error": str(e), "copied": 0}


# ── Module loading ─────────────────────────────────────────────────

def _import_module_from_path(module_name: str, file_path: str):
    """Import a Python module from an absolute file path."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create module spec for {file_path}")
    module = importlib.util.module_from_spec(spec)
    # Don't add to sys.modules — we manage our own namespace
    spec.loader.exec_module(module)
    return module


def _collect_decorated_functions(module) -> list[LoadedTool]:
    """Collect all @chat_tool decorated functions from a module."""
    tools = []
    for attr_name in dir(module):
        obj = getattr(module, attr_name)
        if callable(obj) and hasattr(obj, "_chat_tool_meta"):
            meta = obj._chat_tool_meta
            tools.append(LoadedTool(
                name=meta["name"],
                description=meta["description"],
                input_schema=meta["input_schema"],
                trust_boundary=meta["trust_boundary"],
                capabilities=meta["capabilities"],
                always_available=meta["always_available"],
                requires_user_confirm=meta.get("requires_user_confirm", False),
                handler=obj,
            ))
    return tools


def _inject_platform_tools(tools: dict[str, LoadedTool]) -> None:
    """Inject platform tool definitions into the tools dict.

    Platform tools have handlers in invocation.py (dispatched before
    get_handler), but they need LoadedTool entries for get_tool_defs_for_api()
    and get_loaded_tools() to include them.
    """
    def _stub_handler(tool_input, **kwargs):
        return "Error: platform tool handler not registered"

    for defn in _PLATFORM_TOOL_DEFS:
        if defn["name"] not in tools:
            tools[defn["name"]] = LoadedTool(
                name=defn["name"],
                description=defn["description"],
                input_schema=defn["input_schema"],
                trust_boundary=defn["trust_boundary"],
                capabilities=defn["capabilities"],
                always_available=defn["always_available"],
                requires_user_confirm=defn.get("requires_user_confirm", False),
                handler=_stub_handler,
            )


def load_chat_tools(chat_tools_dir: str) -> dict[str, LoadedTool]:
    """Import all *.py modules from chat_tools_dir, collect @chat_tool functions.

    Returns dict mapping tool_name -> LoadedTool.
    Validates via chat_tool_registry.validate_tool_defs().

    Raises:
        RuntimeError: If validation fails on initial load.
    """
    global _loaded_tools, _mtimes, _chat_tools_dir
    _chat_tools_dir = chat_tools_dir

    tools: dict[str, LoadedTool] = {}
    mtimes: dict[str, float] = {}

    if not os.path.isdir(chat_tools_dir):
        logger.warning("Chat tools directory not found: %s", chat_tools_dir)
        _loaded_tools = tools
        _mtimes = mtimes
        return tools

    py_files = sorted(Path(chat_tools_dir).glob("*.py"))
    for py_file in py_files:
        if py_file.name.startswith("_"):
            continue
        module_name = f"_chat_tools_user_.{py_file.stem}"
        try:
            module = _import_module_from_path(module_name, str(py_file))
            module_tools = _collect_decorated_functions(module)
            for tool in module_tools:
                if tool.name in tools:
                    logger.warning(
                        "Duplicate chat tool name %r in %s (already loaded), skipping",
                        tool.name, py_file.name,
                    )
                    continue
                tools[tool.name] = tool
            mtimes[str(py_file)] = py_file.stat().st_mtime
        except Exception as e:
            logger.error("Failed to load chat tool module %s: %s", py_file.name, e)
            raise RuntimeError(
                f"Chat tool module {py_file.name} failed to load: {e}"
            ) from e

    # Validate
    from .chat_tool_registry import validate_tool_defs
    errors = validate_tool_defs(list(tools.values()))
    if errors:
        for err in errors:
            logger.error("Chat tool validation: %s", err)
        raise RuntimeError(f"Chat tool validation failed: {errors}")

    # Inject platform tool definitions (handlers live in invocation.py)
    _inject_platform_tools(tools)

    _loaded_tools = tools
    _mtimes = mtimes

    # Log summary
    chat_count = sum(1 for t in tools.values() if t.trust_boundary == "chat")
    platform_count = sum(1 for t in tools.values() if t.trust_boundary == "platform")

    # Capability breakdown
    cap_counts: dict[str, int] = {}
    for t in tools.values():
        for cap in t.capabilities:
            cap_counts[cap] = cap_counts.get(cap, 0) + 1
    cap_summary = ", ".join(f"{k}={v}" for k, v in sorted(cap_counts.items()))

    logger.info(
        "Loaded %d chat tools: %d chat-boundary (read-only), %d platform-boundary",
        len(tools), chat_count, platform_count,
    )
    if cap_summary:
        logger.info("Chat tools by capability: %s", cap_summary)

    return tools


# ── Public API ─────────────────────────────────────────────────────

def get_handler(tool_name: str) -> Callable | None:
    """Get handler for a tool name from the loaded set."""
    tool = _loaded_tools.get(tool_name)
    return tool.handler if tool else None


def get_tool_defs_for_api() -> list[dict]:
    """Return tool definitions in Claude API format."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in _loaded_tools.values()
    ]


def get_always_available_names() -> set[str]:
    """Return names of always-available tools."""
    return {t.name for t in _loaded_tools.values() if t.always_available}


def get_total_count() -> int:
    """Total number of loaded tools."""
    return len(_loaded_tools)


def get_loaded_tools() -> dict[str, LoadedTool]:
    """Return the current loaded tools dict (read-only access)."""
    return _loaded_tools


# ── Confirmation handler for tools requiring user approval ─────────

def set_confirmation_handler(handler: Callable[[str, dict], bool]) -> None:
    """Register platform-specific confirmation handler for tools.

    The handler is called when a tool with ``requires_user_confirm=True``
    is invoked. It receives the tool name and input parameters, and must
    return True (user confirmed) or False (user declined).

    Args:
        handler: Callable(tool_name: str, tool_input: dict) -> bool.
                 Returns True if user confirmed, False if declined.
    """
    global _confirmation_handler
    _confirmation_handler = handler
    logger.info("Registered confirmation handler for chat tools")


def get_confirmation_handler() -> Callable[[str, dict], bool] | None:
    """Return the registered confirmation handler, or None if not set."""
    return _confirmation_handler


# ── Extension tool registration ───────────────────────────────────


def register_extension_tool(
    name: str,
    description: str,
    input_schema: dict,
    handler: Callable,
    capabilities: list[str] | None = None,
    always_available: bool = False,
    requires_user_confirm: bool = False,
) -> None:
    """Register a platform extension tool at runtime.

    Used by platform packages (e.g., carpenter-android) to add tools
    that are only available on that platform.  Extension tools are
    always ``trust_boundary="chat"`` — platform-boundary tools must
    be in the hardcoded PLATFORM_TOOLS allowlist.

    Skips silently if a tool with the same name is already registered.
    """
    from .chat_tool_registry import PLATFORM_TOOLS

    if name in _loaded_tools:
        return

    if capabilities is None:
        capabilities = ["pure"]

    tool = LoadedTool(
        name=name,
        description=description,
        input_schema=input_schema,
        trust_boundary="chat",
        capabilities=capabilities,
        always_available=always_available,
        requires_user_confirm=requires_user_confirm,
        handler=handler,
    )

    # Validate the single tool
    from .chat_tool_registry import validate_tool_defs
    errors = validate_tool_defs([tool])
    if errors:
        raise ValueError(
            f"Extension tool {name!r} failed validation: {errors}"
        )

    _loaded_tools[name] = tool
    logger.info("Registered extension tool: %s", name)


# ── Hot-reload ─────────────────────────────────────────────────────

def _check_and_reload():
    """Check for mtime changes and reload if needed.

    Called from heartbeat hook. On validation failure, keeps previous set.
    """
    global _loaded_tools, _mtimes

    if not _chat_tools_dir or not os.path.isdir(_chat_tools_dir):
        return

    py_files = sorted(Path(_chat_tools_dir).glob("*.py"))
    current_files = {str(f) for f in py_files if not f.name.startswith("_")}
    tracked_files = set(_mtimes.keys())

    # Check for changes
    changed = False

    # New or removed files
    if current_files != tracked_files:
        changed = True
    else:
        # Check mtimes
        for fpath in current_files:
            try:
                mtime = os.path.getmtime(fpath)
                if mtime != _mtimes.get(fpath, 0):
                    changed = True
                    break
            except OSError:
                changed = True
                break

    if not changed:
        return

    logger.info("Chat tool file changes detected, reloading...")

    # Try to reload
    new_tools: dict[str, LoadedTool] = {}
    new_mtimes: dict[str, float] = {}

    for py_file in py_files:
        if py_file.name.startswith("_"):
            continue
        module_name = f"_chat_tools_user_.{py_file.stem}"
        try:
            module = _import_module_from_path(module_name, str(py_file))
            module_tools = _collect_decorated_functions(module)
            for tool in module_tools:
                if tool.name in new_tools:
                    logger.warning(
                        "Duplicate chat tool %r in %s during reload, skipping",
                        tool.name, py_file.name,
                    )
                    continue
                new_tools[tool.name] = tool
            new_mtimes[str(py_file)] = py_file.stat().st_mtime
        except Exception as e:
            logger.error(
                "Chat tool reload failed for %s: %s — keeping previous set",
                py_file.name, e,
            )
            return  # Keep previous valid set

    # Inject platform tools
    _inject_platform_tools(new_tools)

    # Validate
    from .chat_tool_registry import validate_tool_defs
    errors = validate_tool_defs(list(new_tools.values()))
    if errors:
        for err in errors:
            logger.error("Chat tool reload validation: %s", err)
        logger.warning("Chat tool reload validation failed, keeping previous set")
        return

    # Diff for logging
    old_names = set(_loaded_tools.keys())
    new_names = set(new_tools.keys())
    added = new_names - old_names
    removed = old_names - new_names
    if added:
        logger.info("Chat tools added: %s", ", ".join(sorted(added)))
    if removed:
        logger.info("Chat tools removed: %s", ", ".join(sorted(removed)))

    _loaded_tools = new_tools
    _mtimes = new_mtimes
    logger.info("Chat tools reloaded: %d tools", len(new_tools))


def register_reload_hook(chat_tools_dir: str) -> None:
    """Register heartbeat hook for mtime-based hot-reload."""
    global _chat_tools_dir
    _chat_tools_dir = chat_tools_dir
    from .core.engine import main_loop
    main_loop.register_heartbeat_hook(_check_and_reload)
    logger.info("Chat tool hot-reload hook registered")
