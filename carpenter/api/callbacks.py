"""Tool dispatch table and validation logic for executor tool calls.

This module defines the dispatch table (_DISPATCH), tool classification
lists (session-exempt, messaging, external-access, untrusted-data), and
validation helpers used by the dispatch bridge.

The HTTP callback endpoint (handle_callback) has been removed.  All tool
dispatch now goes through ``carpenter.executor.dispatch_bridge``.
"""
import logging
import sqlite3
from datetime import datetime, timezone

from ..tool_backends import messaging as msg_backend
from ..tool_backends import state as state_backend
from ..tool_backends import files as files_backend
from ..tool_backends import arc as arc_backend
from ..tool_backends import scheduling as sched_backend
from ..tool_backends import web as web_backend
from ..tool_backends import git as git_backend
from ..tool_backends import forgejo_api as forgejo_api_backend
from ..tool_backends import plugin as plugin_backend
from ..tool_backends import review as review_backend
from ..tool_backends import policy as policy_backend
from ..tool_backends import lm as lm_backend
from ..tool_backends import config_tool as config_tool_backend
from ..tool_backends import webhook as webhook_backend
from ..tool_backends import kb as kb_backend
from ..tool_backends import conversation as conversation_backend
from ..tool_backends import credentials as credentials_backend
from ..tool_backends import platform as platform_backend
from ..core.trust.types import AgentType, get_agent_capabilities
from ..core.trust.capabilities import get_arc_capabilities, resolve_capability_tools, SCOPE_BYPASS_CAPABILITIES
from ..core.trust.audit import log_trust_event
from ..db import get_db, db_connection
from .. import config

logger = logging.getLogger(__name__)


def _link_arc_to_session_conversation(session_id: str, arc_id: int) -> None:
    """Link a newly created arc to its source conversation (idempotent)."""
    try:
        with db_connection() as db:
            row = db.execute(
                "SELECT conversation_id FROM execution_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row and row["conversation_id"]:
                from ..agent.conversation import link_arc_to_conversation
                link_arc_to_conversation(row["conversation_id"], arc_id)
    except (sqlite3.Error, ImportError, KeyError) as _exc:
        pass  # Non-critical

# ── Default tool classification lists ────────────────────────────────
# These define the built-in defaults. Users can extend or reduce these
# lists via config.yaml tool_lists.{name}_add / tool_lists.{name}_remove
# without modifying code. The getter functions below merge config overrides.
#
# SECURITY: Default is DENY — tools not listed in session_exempt are gated.
# Adding a new tool to _DISPATCH without listing it here means it's gated
# by default (fail-safe).

_DEFAULT_SESSION_EXEMPT_TOOLS = frozenset({
    # Read-only data access
    "messaging.ask",
    "state.get", "state.list",
    "files.read", "files.list",
    "arc.get", "arc.get_children", "arc.get_history",
    "arc.get_plan", "arc.get_children_plan",
    "config.get_value", "config.list_keys", "config.models",
    "plugin.check_task", "plugin.check_health", "plugin.read_workspace_file",
    "plugin.list_plugins", "plugin.get_task_status",
    "policy.validate",
    # Trust boundary tools (have their own enforcement via _UNTRUSTED_DATA_TOOLS)
    "arc.read_output_UNTRUSTED", "arc.read_state_UNTRUSTED",
    # Read-only git operations
    "git.get_pr", "git.get_pr_diff",
    # Read-only webhook operations
    "webhook.list",
})

_DEFAULT_UNTRUSTED_DATA_TOOLS = frozenset({
    "arc.read_output_UNTRUSTED",
    "arc.read_state_UNTRUSTED",
})

_DEFAULT_EXTERNAL_ACCESS_TOOLS = frozenset({
    "web.get",
    "web.post",
    "web.fetch_webpage",
})

_DEFAULT_MESSAGING_TOOLS = frozenset({
    "messaging.send",
    "messaging.ask",
})

def _get_allowed_tools(agent_type: AgentType) -> frozenset | None:
    """Look up allowed_tools for an agent type from the live config."""
    caps = get_agent_capabilities()
    return caps.get(agent_type, {}).get("allowed_tools")


def _get_tool_list_config() -> dict:
    """Return the tool_lists config section, with defaults for missing keys."""
    from .. import config as cfg
    raw = cfg.CONFIG.get("tool_lists", {})
    return raw if isinstance(raw, dict) else {}


def _apply_overrides(defaults: frozenset, add_key: str, remove_key: str) -> frozenset:
    """Merge config add/remove overrides into a default frozenset."""
    tl = _get_tool_list_config()
    add = tl.get(add_key, [])
    remove = tl.get(remove_key, [])
    if not add and not remove:
        return defaults
    result = set(defaults)
    if add:
        result |= set(add)
    if remove:
        result -= set(remove)
    return frozenset(result)


def get_session_exempt_tools() -> frozenset:
    """Return the effective session-exempt tool set (defaults + config overrides)."""
    return _apply_overrides(
        _DEFAULT_SESSION_EXEMPT_TOOLS,
        "session_exempt_tools_add", "session_exempt_tools_remove",
    )


def get_untrusted_data_tools() -> frozenset:
    """Return the effective untrusted-data tool set (defaults + config overrides)."""
    return _apply_overrides(
        _DEFAULT_UNTRUSTED_DATA_TOOLS,
        "untrusted_data_tools_add", "untrusted_data_tools_remove",
    )


def get_external_access_tools() -> frozenset:
    """Return the effective external-access tool set (defaults + config overrides)."""
    return _apply_overrides(
        _DEFAULT_EXTERNAL_ACCESS_TOOLS,
        "external_access_tools_add", "external_access_tools_remove",
    )


def get_messaging_tools() -> frozenset:
    """Return the effective messaging tool set (defaults + config overrides)."""
    return _apply_overrides(
        _DEFAULT_MESSAGING_TOOLS,
        "messaging_tools_add", "messaging_tools_remove",
    )


# Whitelist for PLANNER agent type — only structural/messaging tools.
# NOTE: Agent capabilities are kernel-level security policy (trust_types.py)
# and are NOT user-configurable. They require a code change + human review.
_PLANNER_ALLOWED_TOOLS = get_agent_capabilities()[AgentType.PLANNER]["allowed_tools"]

# Whitelist for REVIEWER agent type.
_REVIEWER_ALLOWED_TOOLS = get_agent_capabilities()[AgentType.REVIEWER]["allowed_tools"]

# Whitelist for JUDGE agent type (same as REVIEWER).
_JUDGE_ALLOWED_TOOLS = get_agent_capabilities()[AgentType.JUDGE]["allowed_tools"]

# Tool dispatch table
_DISPATCH = {
    "messaging.send": msg_backend.handle_send,
    "messaging.ask": msg_backend.handle_ask,
    "state.get": state_backend.handle_get,
    "state.set": state_backend.handle_set,
    "state.delete": state_backend.handle_delete,
    "state.list": state_backend.handle_list,
    "files.read": files_backend.handle_read,
    "files.write": files_backend.handle_write,
    "files.list": files_backend.handle_list,
    "arc.create": arc_backend.handle_create,
    "arc.add_child": arc_backend.handle_add_child,
    "arc.create_batch": arc_backend.handle_create_batch,
    "arc.invoke_coding_change": arc_backend.handle_invoke_coding_change,
    "arc.request_ai_review": arc_backend.handle_request_ai_review,
    "arc.get": arc_backend.handle_get,
    "arc.get_children": arc_backend.handle_get_children,
    "arc.get_history": arc_backend.handle_get_history,
    "arc.cancel": arc_backend.handle_cancel,
    "arc.update_status": arc_backend.handle_update_status,
    "scheduling.add_cron": sched_backend.handle_add_cron,
    "scheduling.add_once": sched_backend.handle_add_once,
    "scheduling.remove_cron": sched_backend.handle_remove_cron,
    "scheduling.list_cron": sched_backend.handle_list_cron,
    "scheduling.enable_cron": sched_backend.handle_enable_cron,
    "web.get": web_backend.handle_get,
    "web.post": web_backend.handle_post,
    "web.fetch_webpage": web_backend.handle_fetch_webpage,
    "git.setup_repo": git_backend.handle_setup_repo,
    "git.create_branch": git_backend.handle_create_branch,
    "git.commit_and_push": git_backend.handle_commit_and_push,
    "git.create_pr": forgejo_api_backend.handle_create_pr,
    "git.list_prs": forgejo_api_backend.handle_list_prs,
    "git.merge_pr": forgejo_api_backend.handle_merge_pr,
    "git.close_pr": forgejo_api_backend.handle_close_pr,
    "git.get_pr": forgejo_api_backend.handle_get_pr,
    "git.get_pr_diff": forgejo_api_backend.handle_get_pr_diff,
    "git.post_pr_review": forgejo_api_backend.handle_post_pr_review,
    "git.create_repo_webhook": forgejo_api_backend.handle_create_repo_webhook,
    "git.delete_repo_webhook": forgejo_api_backend.handle_delete_repo_webhook,
    "config.reload": config_tool_backend.handle_reload,
    "config.set_value": config_tool_backend.handle_set_value,
    "config.get_value": config_tool_backend.handle_get_value,
    "config.list_keys": config_tool_backend.handle_list_keys,
    "config.models": config_tool_backend.handle_models,
    "plugin.submit_task": plugin_backend.handle_submit_task,
    "plugin.check_task": plugin_backend.handle_check_task,
    "plugin.check_health": plugin_backend.handle_check_health,
    "plugin.read_workspace_file": plugin_backend.handle_read_workspace_file,
    "plugin.list_plugins": plugin_backend.handle_list_plugins,
    "plugin.get_task_status": plugin_backend.handle_get_task_status,
    # Trust boundary tools (Phase B)
    "arc.get_plan": arc_backend.handle_get_plan,
    "arc.get_children_plan": arc_backend.handle_get_children_plan,
    "arc.read_output_UNTRUSTED": arc_backend.handle_read_output_UNTRUSTED,
    "arc.read_state_UNTRUSTED": arc_backend.handle_read_state_UNTRUSTED,
    # Review tools (Phase C)
    "review.submit_verdict": review_backend.handle_submit_verdict,
    # Language model call tool
    "lm.call": lm_backend.handle_call,
    # Policy validation (read-only, no session required)
    "policy.validate": policy_backend.handle_validate,
    # Webhook subscription management
    "webhook.subscribe": webhook_backend.handle_subscribe,
    "webhook.list": webhook_backend.handle_list,
    "webhook.delete": webhook_backend.handle_delete,
    # Arc read grants
    "arc.grant_read_access": arc_backend.handle_grant_read_access,
    # Conversation management
    "conversation.rename": conversation_backend.handle_rename,
    "conversation.archive": conversation_backend.handle_archive,
    "conversation.archive_batch": conversation_backend.handle_archive_batch,
    "conversation.archive_all": conversation_backend.handle_archive_all,
    # Credential management
    "credentials.request": credentials_backend.handle_request,
    "credentials.verify": credentials_backend.handle_verify,
    "credentials.import_file": credentials_backend.handle_import_file,
    # Platform management
    "platform.request_restart": platform_backend.handle_request_restart,
    # Knowledge Base modification
    "kb.edit": kb_backend.handle_edit,
    "kb.add": kb_backend.handle_add,
    "kb.delete": kb_backend.handle_delete,
}


def validate_tool_classification():
    """Validate that session-exempt tool entries all exist in _DISPATCH.

    Checks both the built-in defaults and the effective set (after config
    overrides). Catches stale entries left behind after a tool is removed
    from the dispatch table.  Called once at startup.
    """
    effective = get_session_exempt_tools()
    stale = effective - set(_DISPATCH.keys())
    if stale:
        raise RuntimeError(
            f"Session-exempt tools not in dispatch table (stale?): {stale}"
        )


def validate_execution_session(session_id: str | None) -> bool:
    """Check if a session ID represents reviewed, non-expired execution.

    Args:
        session_id: The execution session ID from the X-Execution-Session-ID header.

    Returns:
        True if session is valid and reviewed, False otherwise.
    """
    if not session_id:
        return False

    with db_connection() as db:
        now = datetime.now(timezone.utc).isoformat()
        row = db.execute(
            "SELECT reviewed FROM execution_sessions "
            "WHERE session_id = ? AND expires_at > ?",
            (session_id, now)
        ).fetchone()

        return row is not None and bool(row["reviewed"])


def _get_session_conversation_id(session_id: str | None) -> int | None:
    """Look up the conversation_id for an execution session.

    Returns the conversation_id if found, None otherwise.
    """
    if not session_id:
        return None
    with db_connection() as db:
        row = db.execute(
            "SELECT conversation_id FROM execution_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row["conversation_id"] if row and row["conversation_id"] else None


def _is_session_conversation_tainted(session_id: str | None) -> bool:
    """Check if the execution session's conversation is tainted."""
    conv_id = _get_session_conversation_id(session_id)
    if conv_id is None:
        return False
    from ..security.trust import is_conversation_tainted
    return is_conversation_tainted(conv_id)


def _get_session_execution_context(session_id: str | None) -> str | None:
    """Look up the execution_context for an execution session.

    Returns ``"arc-step"`` for arc dispatch, ``"reviewed"`` for chat
    submit_code, or ``None`` if the session is not found.
    """
    if not session_id:
        return None
    with db_connection() as db:
        row = db.execute(
            "SELECT execution_context FROM execution_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return row["execution_context"] or "reviewed"


def _get_caller_context(params: dict) -> dict | None:
    """Look up the calling arc's integrity_level and agent_type.

    Uses only ``_caller_arc_id`` (injected by the executor environment),
    never ``arc_id`` which is the *target* of the tool call.  When code
    runs from the chat agent (no arc context), ``_caller_arc_id`` is
    absent and this returns None — meaning no agent-type restrictions
    apply (the chat agent has ``allowed_tools=None``).

    Returns dict with 'integrity_level' and 'agent_type', or None if
    the caller arc is unknown.
    """
    arc_id = params.get("_caller_arc_id")
    if arc_id is None:
        return None
    with db_connection() as db:
        row = db.execute(
            "SELECT integrity_level, agent_type FROM arcs WHERE id = ?",
            (arc_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "integrity_level": row["integrity_level"] or "trusted",
            "agent_type": row["agent_type"] or "EXECUTOR",
        }


def _is_descendant_of(target_arc_id: int, ancestor_arc_id: int) -> bool:
    """Check if target_arc_id is a descendant of ancestor_arc_id.

    Walks up the parent chain from target to see if ancestor is reached.
    """
    with db_connection() as db:
        current_id = target_arc_id
        # Walk up parent chain (max depth guard to prevent infinite loops)
        for _ in range(config.get_config("arc_parent_chain_max_depth", 100)):
            row = db.execute(
                "SELECT parent_id FROM arcs WHERE id = ?", (current_id,)
            ).fetchone()
            if row is None:
                return False
            parent_id = row["parent_id"]
            if parent_id is None:
                return False
            if parent_id == ancestor_arc_id:
                return True
            current_id = parent_id
        return False


