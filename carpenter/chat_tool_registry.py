"""Chat tool registry — validation layer for trust boundaries and capabilities.

Tool definitions now live in user-configurable Python modules under
``config/chat_tools/``, loaded via ``chat_tool_loader.py``.  This module
provides the immutable allowlists and validation functions.

Trust boundaries:
  - "chat"     — Read-only, internal state queries.  Safe for direct agent access.
  - "platform" — Privileged platform operations.  Hardcoded allowlist only.

See docs/trust-invariants.md I10 for the formal invariant.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .chat_tool_loader import LoadedTool

logger = logging.getLogger(__name__)

# Immutable allowlist — changing this IS a platform-level change.
PLATFORM_TOOLS = frozenset({
    "submit_code",
    "escalate_current_arc",
    "escalate",
    "fetch_web_content",
    "set_conversation_model",
    # D24 stage 3a: capability-package install/uninstall.  These mutate
    # platform state (filesystem + DB) and use the standard chat-tool
    # human-confirmation pattern (requires_user_confirm=True).
    "install_package",
    "uninstall_package",
    # API budget circuit breaker control: clearing a tripped breaker or
    # raising a limit mutates safety-critical platform state.
    "budget_control",
})

_VALID_BOUNDARIES = frozenset({"chat", "platform"})

# ── Capability vocabulary ──────────────────────────────────────────

READ_CAPABILITIES = frozenset({
    "filesystem_read",
    "database_read",
    "kb_read",
    "config_read",
    "pure",
})

WRITE_CAPABILITIES = frozenset({
    "filesystem_write",
    "database_write",
    "arc_create",
    "external_effect",
})

VALID_CAPABILITIES = READ_CAPABILITIES | WRITE_CAPABILITIES


def validate_tool_defs(
    tools: list[LoadedTool],
    *,
    write_chat_tools_allowed: bool = False,
) -> list[str]:
    """Validate loaded tool definitions.  Returns list of error strings (empty = OK).

    Checks:
    - Every tool has a valid trust boundary
    - Platform-boundary tools not in PLATFORM_TOOLS are rejected
    - Chat-boundary tools with write capabilities are rejected
    - No duplicate names
    - No empty descriptions or schemas
    - Unknown capability strings are rejected
    - ``pure`` mixed with other capabilities is flagged

    Args:
        tools: Loaded tool definitions to validate.
        write_chat_tools_allowed: Operator-gated relaxation of the
            "chat-boundary tools may not declare write capabilities"
            rule (invariant I10).  Defaults to ``False`` (the chat agent
            is read-only by default).  When ``True`` — set only when an
            operator explicitly opted a capability package in at install
            time — a chat-boundary tool MAY declare write capabilities.
            This NEVER relaxes the platform-boundary refusal: a package
            tool declaring ``trust_boundary='platform'`` is always
            rejected.  Callers that validate a mix of packages (e.g. the
            cross-package consistency check) must validate each package's
            tools separately with that package's flag — passing ``True``
            here only makes sense for a single package's tools.
    """
    errors: list[str] = []

    seen_names: set[str] = set()

    for tool in tools:
        # Duplicate check
        if tool.name in seen_names:
            errors.append(f"Duplicate tool name: {tool.name!r}")
        seen_names.add(tool.name)

        # Boundary check
        if tool.trust_boundary not in _VALID_BOUNDARIES:
            errors.append(
                f"Tool {tool.name!r} has invalid trust_boundary "
                f"{tool.trust_boundary!r} (valid: {_VALID_BOUNDARIES})"
            )

        # Platform boundary enforcement: only PLATFORM_TOOLS may declare it
        if tool.trust_boundary == "platform" and tool.name not in PLATFORM_TOOLS:
            errors.append(
                f"Tool {tool.name!r} declares trust_boundary='platform' but "
                f"is not in PLATFORM_TOOLS allowlist. User-config tools cannot "
                f"have platform trust boundary."
            )

        # Capability validation
        for cap in tool.capabilities:
            if cap not in VALID_CAPABILITIES:
                errors.append(
                    f"Tool {tool.name!r} has unknown capability {cap!r} "
                    f"(valid: {sorted(VALID_CAPABILITIES)})"
                )

        # Pure mixed with others
        if "pure" in tool.capabilities and len(tool.capabilities) > 1:
            errors.append(
                f"Tool {tool.name!r} mixes 'pure' with other capabilities: "
                f"{tool.capabilities}"
            )

        # Chat-boundary tools cannot have write capabilities — UNLESS an
        # operator explicitly opted this package in at install time
        # (write_chat_tools_allowed=True).  The default is read-only.
        if tool.trust_boundary == "chat" and not write_chat_tools_allowed:
            write_caps = [c for c in tool.capabilities if c in WRITE_CAPABILITIES]
            if write_caps:
                errors.append(
                    f"Tool {tool.name!r} has chat trust_boundary but declares "
                    f"write capabilities: {write_caps}. Only platform tools "
                    f"may declare write capabilities (or a capability package "
                    f"the operator opted in via write_chat_tools_allowed)."
                )

        # Description / schema check
        if not tool.description:
            errors.append(f"Tool {tool.name!r} has empty description")
        if not tool.input_schema:
            errors.append(f"Tool {tool.name!r} has empty input_schema")

    return errors
