"""Dispatch bridge -- connects RestrictedExecutor to tool backends.

Extracts the validation and dispatch logic from ``carpenter.api.callbacks``
into standalone functions callable without HTTP request context.  This module
bridges the RestrictedExecutor's queue-based dispatch to the existing tool
backend handlers.

The bridge reuses the same ``_DISPATCH`` table, permission checks, and trust
boundary enforcement from the callback module -- just without the Starlette
request/response wrapper.
"""

import logging
from typing import Any

from ..api.callbacks import (
    _DISPATCH,
    _get_caller_context,
    _get_session_conversation_id,
    _get_session_execution_context,
    _is_descendant_of,
    _is_session_conversation_tainted,
    get_external_access_tools,
    get_messaging_tools,
    get_session_exempt_tools,
    validate_execution_session,
    _link_arc_to_session_conversation,
    _get_allowed_tools,
)
from ..core.trust.types import AgentType
from ..core.trust.capabilities import get_arc_capabilities, resolve_capability_tools, SCOPE_BYPASS_CAPABILITIES
from ..core.trust.audit import log_trust_event
from ..db import get_db, db_connection

logger = logging.getLogger(__name__)


class DispatchError(Exception):
    """Raised when a dispatch request fails validation or execution."""

    def __init__(self, message: str, status_code: int = 403):
        super().__init__(message)
        self.status_code = status_code


def validate_and_dispatch(
    tool_name: str,
    params: dict,
    *,
    session_id: str | None = None,
    conversation_id: int | None = None,
    arc_id: int | None = None,
    execution_context: str = "reviewed",
) -> Any:
    """Validate and dispatch a tool call, applying the same security checks as
    the HTTP callback handler.

    This is the ``tool_handler`` passed to ``RestrictedExecutor``.

    Args:
        tool_name: Dotted tool name (e.g. ``"state.get"``).
        params: Tool parameters dict.
        session_id: Execution session ID for permission checks.
        conversation_id: Conversation context (injected into params).
        arc_id: Arc context (injected into params as both arc_id and
            _caller_arc_id).
        execution_context: ``"reviewed"`` or ``"arc-step"``.

    Returns:
        The tool handler's return value (must be JSON-serializable).

    Raises:
        DispatchError: If validation fails.
    """
    # Look up handler
    handler = _DISPATCH.get(tool_name)

    # Package-contributed TRUSTED capability verb?  These are registered
    # at load time from a package's ``platform_capabilities`` manifest
    # section.  They are NOT in ``_DISPATCH``; they route through the
    # capability registry, which builds the scoped CapabilityContext and
    # binds host/creds to the operator-confirmed grant.  Permission to
    # invoke is gated per-package below (the verb is allowed ONLY for arcs
    # belonging to the package that registered it).
    capability_verb = False
    if handler is None:
        from ..packages.capabilities import get_capability_registry
        cap_registry = get_capability_registry()
        if cap_registry.is_capability_verb(tool_name):
            capability_verb = True
            # Bind a per-call closure so the existing handler-invoke path
            # (which calls ``handler(params)``) works unchanged.
            def handler(p, _verb=tool_name, _reg=cap_registry):  # noqa: E731
                return _reg.dispatch(_verb, p)

    if handler is None:
        raise DispatchError(f"Unknown tool: {tool_name}", status_code=404)

    # Auto-inject context (mirrors _callback.py behavior)
    if conversation_id is not None and "conversation_id" not in params:
        params["conversation_id"] = conversation_id
    if arc_id is not None:
        # _caller_arc_id is the platform-injected CALLER identity and must not
        # be spoofable by untrusted executor params — override unconditionally.
        params["_caller_arc_id"] = arc_id
        if "arc_id" not in params:
            params["arc_id"] = arc_id

    # ── Package-capability gate (per-package, fail-closed) ──────────
    # A package's registered capability verb is permitted ONLY for that
    # package's own arcs.  An arc "belongs to" a package when it carries
    # the package's grant capability (``pkg.<name>``) in its
    # ``_capabilities`` arc_state.  This is enforced for EVERY caller
    # regardless of agent type (EXECUTOR has ``allowed_tools=None`` and
    # would otherwise be permitted any tool in the dispatch table) — the
    # capability registry is the authoritative gate, fail-closed: no
    # caller arc, or an arc lacking the owning package's grant, is denied.
    if capability_verb:
        from ..packages.capabilities import (
            capability_grant_for_package,
            get_capability_registry,
        )
        owner_pkg = get_capability_registry().package_for_verb(tool_name)
        caller_arc_id = params.get("_caller_arc_id")
        if caller_arc_id is None:
            raise DispatchError(
                f"Capability verb {tool_name!r} requires an arc context; "
                f"it cannot be invoked from chat context",
            )
        required_cap = capability_grant_for_package(owner_pkg)
        arc_caps = get_arc_capabilities(caller_arc_id)
        if required_cap not in arc_caps:
            log_trust_event(caller_arc_id, "access_denied", {
                "tool": tool_name,
                "reason": (
                    f"capability verb permitted only for package "
                    f"{owner_pkg!r} arcs (missing grant {required_cap!r})"
                ),
            })
            raise DispatchError(
                f"Capability verb {tool_name!r} is permitted only for "
                f"package {owner_pkg!r}'s own arcs"
            )

    # ── Session-gated tools (default-deny) ──────────────────────────
    if tool_name not in get_session_exempt_tools():
        if not validate_execution_session(session_id):
            logger.warning(
                "Action tool %s called without valid reviewed execution session",
                tool_name,
            )
            raise DispatchError(
                "Action tools require valid reviewed execution session"
            )

    # ── Messaging restriction ───────────────────────────────────────
    if tool_name in get_messaging_tools():
        exec_ctx = _get_session_execution_context(session_id)
        if exec_ctx == "arc-step":
            _arc = params.get("_caller_arc_id") or params.get("arc_id")
            log_trust_event(_arc, "access_denied", {
                "tool": tool_name,
                "reason": "arc executor cannot use messaging tools",
                "execution_context": exec_ctx,
            })
            raise DispatchError(
                "Arc executor code cannot send messages or ask questions. "
                "Write results to arc state instead; the platform handles "
                "user communication."
            )

    # ── Trust boundary enforcement ──────────────────────────────────
    caller_ctx = _get_caller_context(params)
    if caller_ctx:
        caller_agent = caller_ctx["agent_type"]

        # Enforce agent-type tool whitelists
        try:
            agent_enum = AgentType(caller_agent)
        except ValueError:
            agent_enum = None
        if agent_enum is not None:
            allowed = _get_allowed_tools(agent_enum)
            if allowed is not None and tool_name not in allowed:
                # Check capability-granted tools before rejecting
                caller_arc_id = params.get("_caller_arc_id")
                arc_caps = get_arc_capabilities(caller_arc_id) if caller_arc_id else set()
                granted_tools = resolve_capability_tools(arc_caps)
                if tool_name not in granted_tools:
                    _arc = caller_arc_id or params.get("arc_id")
                    log_trust_event(_arc, "access_denied", {
                        "tool": tool_name,
                        "reason": f"{caller_agent.lower()} restricted",
                    })
                    raise DispatchError(
                        f"{caller_agent} agents cannot use {tool_name}"
                    )

    # ── External access enforcement ─────────────────────────────────
    if tool_name in get_external_access_tools():
        if caller_ctx is None:
            raise DispatchError(
                f"External web access ({tool_name}) is not allowed from "
                f"chat context. Create an untrusted arc batch."
            )
        elif caller_ctx["integrity_level"] != "untrusted":
            _arc = params.get("_caller_arc_id") or params.get("arc_id")
            log_trust_event(_arc, "access_denied", {
                "tool": tool_name,
                "reason": "external access requires untrusted arc",
            })
            raise DispatchError(
                f"External web access ({tool_name}) requires an untrusted "
                f"arc (current: {caller_ctx['integrity_level']})"
            )

    # ── Tainted conversation rejects single-arc creation ────────────
    # Untrusted output must flow through a reviewer + judge chain. A bare
    # arc.create / arc.add_child cannot establish that chain, so silently
    # promoting it to untrusted produced an orphan tainted arc with no
    # review wiring. Force callers onto arc.create_batch instead.
    if tool_name in ("arc.create", "arc.add_child"):
        if _is_session_conversation_tainted(session_id):
            _arc = params.get("_caller_arc_id") or params.get("arc_id")
            log_trust_event(_arc, "access_denied", {
                "tool": tool_name,
                "reason": "tainted conversation requires arc.create_batch",
            })
            raise DispatchError(
                "Tainted conversations cannot create individual arcs. "
                "Untrusted output must be validated by a reviewer + "
                "judge chain. Use arc.create_batch with EXECUTOR + "
                "REVIEWER + JUDGE."
            )

    if tool_name == "arc.create_batch":
        if _is_session_conversation_tainted(session_id):
            arcs_list = params.get("arcs", [])
            for arc_spec in arcs_list:
                agent_type = arc_spec.get("agent_type", "EXECUTOR")
                if agent_type in ("REVIEWER", "JUDGE"):
                    continue
                if arc_spec.get("integrity_level", "trusted") == "trusted":
                    arc_spec["integrity_level"] = "untrusted"

    # ── Parent-child state reads ────────────────────────────────────
    if tool_name == "state.get" and "_target_arc_id" in params:
        target_arc_id = params["_target_arc_id"]
        caller_arc_id = params.get("arc_id")

        if caller_arc_id is None:
            raise DispatchError(
                "Cross-arc state read requires caller arc_id"
            )

        # Verify target is a descendant of caller OR reader has a read grant
        # OR caller has a scope-bypass capability (e.g. system.read)
        from ..core.arcs import manager as arc_manager
        if not _is_descendant_of(target_arc_id, caller_arc_id) and not arc_manager.has_read_grant(caller_arc_id, target_arc_id):
            arc_caps = get_arc_capabilities(caller_arc_id)
            if not (arc_caps & SCOPE_BYPASS_CAPABILITIES):
                log_trust_event(caller_arc_id, "access_denied", {
                    "tool": "state.get",
                    "reason": "target is not a descendant and no read grant",
                    "target_arc_id": target_arc_id,
                })
                raise DispatchError(
                    "Cross-arc state read: target arc is not a descendant "
                    "of caller and no read grant exists"
                )

        with db_connection() as db:
            target_row = db.execute(
                "SELECT integrity_level FROM arcs WHERE id = ?",
                (target_arc_id,),
            ).fetchone()

        if target_row is None:
            raise DispatchError(
                f"Target arc {target_arc_id} not found", status_code=404
            )

        if target_row["integrity_level"] != "trusted":
            log_trust_event(caller_arc_id, "access_denied", {
                "tool": "state.get",
                "reason": "target arc is not trusted",
                "target_arc_id": target_arc_id,
            })
            raise DispatchError(
                "Cannot read state from non-trusted child arc via this path"
            )

        params["arc_id"] = target_arc_id
        del params["_target_arc_id"]

    # ── Inject conversation_id for specific tools ───────────────────
    if tool_name == "arc.invoke_coding_change" and "conversation_id" not in params:
        if session_id:
            _conv_id = _get_session_conversation_id(session_id)
            if _conv_id is not None:
                params["conversation_id"] = _conv_id

    if tool_name in ("scheduling.add_cron", "scheduling.add_once") and "conversation_id" not in params:
        if session_id:
            _conv_id = _get_session_conversation_id(session_id)
            if _conv_id is not None:
                params["conversation_id"] = _conv_id

    if tool_name in ("kb.add", "kb.edit") and "conversation_id" not in params:
        if session_id:
            _conv_id = _get_session_conversation_id(session_id)
            if _conv_id is not None:
                params["conversation_id"] = _conv_id

    # ── Execute handler ─────────────────────────────────────────────
    try:
        result = handler(params)
    except Exception as exc:
        logger.exception("Error in tool handler: %s", tool_name)
        raise DispatchError(str(exc), status_code=500) from exc

    # Link newly created arcs to their source conversation
    if tool_name in ("arc.create", "arc.add_child") and isinstance(result, dict):
        _new_arc_id = result.get("arc_id")
        if _new_arc_id and session_id:
            _link_arc_to_session_conversation(session_id, _new_arc_id)

    if tool_name == "arc.create_batch" and isinstance(result, dict):
        for _batch_arc_id in result.get("arc_ids", []):
            if session_id:
                _link_arc_to_session_conversation(session_id, _batch_arc_id)

    return result


def make_tool_handler(
    *,
    session_id: str | None = None,
    conversation_id: int | None = None,
    arc_id: int | None = None,
    execution_context: str = "reviewed",
) -> callable:
    """Create a tool_handler closure pre-bound with execution context.

    This is the function you pass to ``RestrictedExecutor(tool_handler=...)``.
    It captures the session/arc/conversation context so that dispatch calls
    from executed code don't need to know about them.

    Args:
        session_id: Execution session ID for permission checks.
        conversation_id: Conversation context.
        arc_id: Arc context.
        execution_context: ``"reviewed"`` or ``"arc-step"``.

    Returns:
        A callable ``(tool_name, params) -> result``.
    """

    def handler(tool_name: str, params: dict) -> Any:
        return validate_and_dispatch(
            tool_name,
            params,
            session_id=session_id,
            conversation_id=conversation_id,
            arc_id=arc_id,
            execution_context=execution_context,
        )

    return handler
