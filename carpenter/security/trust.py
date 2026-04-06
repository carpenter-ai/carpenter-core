"""Conversation trust taint tracking.

Tracks which conversations have been exposed to untrusted tool output
(e.g., web content). Tainted conversations trigger additional security
review for sensitive operations (KB modifications, code execution, etc.).
"""

import ast
import logging
import re

from ..db import get_db

logger = logging.getLogger(__name__)

# Whitelist of carpenter_tools modules whose output is trusted.
# Any carpenter_tools import NOT in this set taints the conversation.
# This is safer than a blacklist because unknown/new modules are
# blocked by default rather than allowed.
_TRUSTED_IMPORTS: frozenset[str] = frozenset({
    # act modules (side-effecting tools)
    "carpenter_tools.act.arc",
    "carpenter_tools.act.config",
    "carpenter_tools.act.conversation",
    "carpenter_tools.act.credentials",
    "carpenter_tools.act.files",
    "carpenter_tools.act.git",
    "carpenter_tools.act.kb",
    "carpenter_tools.act.lm",
    "carpenter_tools.act.messaging",
    "carpenter_tools.act.platform",
    "carpenter_tools.act.plugin",
    "carpenter_tools.act.review",
    "carpenter_tools.act.scheduling",
    "carpenter_tools.act.state",
    "carpenter_tools.act.webhook",
    # read modules (read-only tools)
    "carpenter_tools.read.arc",
    "carpenter_tools.read.config",
    "carpenter_tools.read.files",
    "carpenter_tools.read.git",
    "carpenter_tools.read.messaging",
    "carpenter_tools.read.platform_time",
    "carpenter_tools.read.plugin",
    "carpenter_tools.read.state",
    "carpenter_tools.read.system_info",
    "carpenter_tools.read.webhook",
})


def _get_trusted_imports() -> frozenset[str]:
    """Return the effective trusted-imports whitelist.

    Uses config override (``security.trusted_imports``) when non-empty,
    otherwise falls back to the built-in ``_TRUSTED_IMPORTS`` default.
    """
    try:
        from ..config import CONFIG
        override = CONFIG.get("security", {}).get("trusted_imports", [])
        if override:
            return frozenset(override)
    except Exception:
        pass
    return _TRUSTED_IMPORTS


def _is_untrusted_carpenter_import(module_path: str) -> bool:
    """Return True if *module_path* is a carpenter_tools module not on the whitelist.

    Non-carpenter_tools modules are NOT checked here (they have their own
    _NETWORK_MODULES check).  Only leaf carpenter_tools modules (three or
    more dotted components like ``carpenter_tools.act.web``) are evaluated
    against the trusted whitelist.  Parent packages like ``carpenter_tools``
    or ``carpenter_tools.act`` are not flagged — their sub-imports are
    checked individually via the ``from carpenter_tools.act import X`` path.
    """
    if not module_path.startswith("carpenter_tools."):
        return False
    # Parent packages (e.g. "carpenter_tools", "carpenter_tools.act",
    # "carpenter_tools.read") are not leaf modules — don't flag them.
    # Only leaf modules with 3+ dotted components are checked.
    parts = module_path.split(".")
    if len(parts) < 3:
        return False
    return module_path not in _get_trusted_imports()

# Built-in default for network modules that cause taint.
# Used when config ``security.network_modules`` is empty (the default).
_DEFAULT_NETWORK_MODULES = frozenset({
    # stdlib
    "socket",
    "http",
    "http.client",
    "http.cookiejar",
    "urllib",
    "urllib.request",
    "urllib.parse",
    "xmlrpc",
    "xmlrpc.client",
    "ftplib",
    "imaplib",
    "poplib",
    "smtplib",
    "telnetlib",
    # common third-party
    "httpx",
    "requests",
    "aiohttp",
    "urllib3",
    "pycurl",
    "grpc",
    "websocket",
    "websockets",
})


def _get_network_modules() -> frozenset[str]:
    """Return the effective network-modules set.

    Uses config override (``security.network_modules``) when non-empty,
    otherwise falls back to the built-in ``_DEFAULT_NETWORK_MODULES`` default.
    """
    try:
        from ..config import CONFIG
        override = CONFIG.get("security", {}).get("network_modules", [])
        if override:
            return frozenset(override)
    except Exception:
        pass
    return _DEFAULT_NETWORK_MODULES


def _get_network_top_level() -> set[str]:
    """Top-level names for matching 'from http import client' style imports."""
    return {m.split(".")[0] for m in _get_network_modules()}

# Regex patterns to detect carpenter_tools imports for whitelist checking.
# Patterns cover both act and read sub-packages.
_IMPORT_PATTERNS = [
    # "from carpenter_tools.act import web" / "from carpenter_tools.read import files"
    re.compile(r"from\s+(carpenter_tools\.\w+)\s+import\s+(\w+(?:\s*,\s*\w+)*)"),
    # "from carpenter_tools.act.web import get"
    re.compile(r"from\s+(carpenter_tools\.\w+\.\w+)\s+import\s+"),
    # "import carpenter_tools.act.web"
    re.compile(r"import\s+(carpenter_tools\.\w+\.\w+)"),
]


def check_code_for_taint(code: str) -> str | None:
    """Check if submitted code imports networking or non-whitelisted tool modules.

    Detects carpenter_tools modules not on the trusted whitelist and
    direct use of networking modules (httpx, requests, socket, urllib, etc.)
    that bypass the taint-tracked tool path.

    Uses both regex and AST parsing for robustness.

    Args:
        code: Python source code to analyze.

    Returns:
        The untrusted module path if found, None if clean.
    """
    # Resolve effective sets once per call
    network_modules = _get_network_modules()
    network_top_level = _get_network_top_level()

    # Try AST parsing first (more reliable)
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                # "from carpenter_tools.act.web import get"
                if _is_untrusted_carpenter_import(node.module):
                    return node.module
                # "from carpenter_tools.act import web" /
                # "from carpenter_tools.read import files"
                if (node.module.startswith("carpenter_tools.")
                        and node.module.count(".") == 1
                        and node.names):
                    for alias in node.names:
                        full = f"{node.module}.{alias.name}"
                        if _is_untrusted_carpenter_import(full):
                            return full
                # "from http import client" — check submodule name first
                # (more specific than the top-level match below)
                if node.module in network_top_level and node.names:
                    for alias in node.names:
                        full = f"{node.module}.{alias.name}"
                        if full in network_modules:
                            return full
                # "from httpx import Client" / "from socket import ..."
                if node.module in network_modules:
                    return node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_untrusted_carpenter_import(alias.name):
                        return alias.name
                    # "import httpx" / "import http.client"
                    if alias.name in network_modules:
                        return alias.name
    except SyntaxError:
        pass

    # Fallback: regex-based detection for carpenter_tools imports
    for pattern in _IMPORT_PATTERNS:
        match = pattern.search(code)
        if match:
            groups = match.groups()
            if len(groups) == 2:
                # Pattern: "from carpenter_tools.act import web, files"
                parent, names = groups
                for name in [n.strip() for n in names.split(",")]:
                    full = f"{parent}.{name}"
                    if _is_untrusted_carpenter_import(full):
                        return full
            else:
                # Pattern: full module path (e.g. "carpenter_tools.act.web")
                captured = groups[0]
                if _is_untrusted_carpenter_import(captured):
                    return captured

    return None


def record_taint(conversation_id: int, source_tool: str) -> None:
    """Record that a conversation has been exposed to untrusted content.

    Args:
        conversation_id: The conversation to taint.
        source_tool: The tool/module that produced untrusted output.
    """
    db = get_db()
    db.execute(
        "INSERT INTO conversation_taint (conversation_id, source_tool) VALUES (?, ?)",
        (conversation_id, source_tool),
    )
    db.commit()
    logger.info(
        "Conversation %d tainted by %s", conversation_id, source_tool,
    )


def is_conversation_tainted(conversation_id: int) -> bool:
    """Check if a conversation has any taint records.

    Args:
        conversation_id: The conversation to check.

    Returns:
        True if the conversation has been tainted.
    """
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM conversation_taint WHERE conversation_id = ? LIMIT 1",
        (conversation_id,),
    ).fetchone()
    return row is not None


def get_taint_sources(conversation_id: int) -> list[str]:
    """Get all taint sources for a conversation.

    Args:
        conversation_id: The conversation to query.

    Returns:
        List of source tool names.
    """
    db = get_db()
    rows = db.execute(
        "SELECT DISTINCT source_tool FROM conversation_taint "
        "WHERE conversation_id = ? ORDER BY source_tool",
        (conversation_id,),
    ).fetchall()
    return [row["source_tool"] for row in rows]
