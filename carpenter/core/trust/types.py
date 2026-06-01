"""Kernel-level trust boundary types and constants.

Enums (OutputType, AgentType) define the type system and stay as code.
The AGENT_CAPABILITIES mapping is policy — configurable via config.yaml
under the ``agent_capabilities`` key.  Hardcoded defaults live here as
``_DEFAULT_AGENT_CAPABILITIES``; ``get_agent_capabilities()`` merges
config overrides on top.
"""

from enum import Enum

from .integrity import IntegrityLevel, validate_integrity_level  # noqa: F401


class OutputType(str, Enum):
    PYTHON = "python"
    TEXT = "text"
    JSON = "json"
    UNKNOWN = "unknown"


class AgentType(str, Enum):
    PLANNER = "PLANNER"
    EXECUTOR = "EXECUTOR"
    REVIEWER = "REVIEWER"
    JUDGE = "JUDGE"
    CHAT = "CHAT"


# Hardcoded fallback defaults — overridable via config["agent_capabilities"].
#
# JUDGE: In the IFC model, JUDGE arcs run deterministic platform code
# (not LLM agents). Their allowed_tools is empty — they don't call tools.
# The dispatch handler runs policy checks directly.
_DEFAULT_AGENT_CAPABILITIES = {
    AgentType.PLANNER: {
        "can_create_untrusted_arcs": True,
        "allowed_tools": frozenset({
            "arc.create", "arc.add_child", "arc.cancel", "arc.update_status",
            "arc.get", "arc.get_children", "arc.get_history",
            "arc.get_plan", "arc.get_children_plan",
            "state.get", "state.list",
            "messaging.send", "messaging.ask",
            # Phase B PR B1: PLANNER may register raw Resources it produces.
            # The Resource is untrusted at creation (produced_by_template=
            # NULL); a later template-arc pipeline can derive a trusted
            # successor from it.
            "resource.create",
            # Phase B PR B3: PLANNER may fetch a URL into a raw Resource in
            # one step (dispatch-only; chat agents route through the
            # reviewed fetch_web_content pipeline instead).
            "web.fetch_webpage_to_resource",
        }),
    },
    AgentType.EXECUTOR: {
        "can_create_untrusted_arcs": False,
        "allowed_tools": None,  # all tools via normal session checks
    },
    AgentType.REVIEWER: {
        "can_create_untrusted_arcs": False,
        "allowed_tools": frozenset({
            "arc.get", "arc.get_children", "arc.get_history",
            "arc.get_plan", "arc.get_children_plan",
            "state.get", "state.set", "state.list",
            "files.read", "files.write", "files.list",
            "messaging.send", "messaging.ask",
            "review.submit_verdict",
            # PR3: template reviewers commit derived Resources by writing
            # the blob and then calling resource.finalize(..., deprecate_inputs=True).
            "resource.finalize",
            # Phase B PR B1: REVIEWER may also register raw Resources when
            # its review produces new untrusted byte streams that need a
            # follow-up template pipeline (rather than just a derived
            # Resource of a declared template).
            "resource.create",
            # Phase B PR B3: REVIEWER may pull additional pages into raw
            # Resources mid-review (e.g. fetch a linked page referenced by
            # a Resource it's currently reviewing).
            "web.fetch_webpage_to_resource",
        }),
    },
    AgentType.JUDGE: {
        "can_create_untrusted_arcs": False,
        # JUDGE runs platform code and normally makes no tool calls.  The
        # one exception is ``resource.submit_verdict``: when a JUDGE's
        # decision also gates a Resource's template_verdict (see PR3's
        # fetch_web_content pipeline), the dispatch handler is the
        # authoritative write path for that flip.
        "allowed_tools": frozenset({"resource.submit_verdict"}),
    },
    AgentType.CHAT: {
        "can_create_untrusted_arcs": True,
        "allowed_tools": None,  # all tools via normal chat dispatch
    },
}


def get_agent_capabilities() -> dict:
    """Return agent capabilities, merging config overrides with defaults.

    Config format (in config.yaml ``agent_capabilities`` key) uses plain
    strings and lists so it round-trips through YAML::

        agent_capabilities:
          PLANNER:
            can_create_untrusted_arcs: true
            allowed_tools:
              - arc.create
              - arc.add_child
              ...

    The getter converts string agent-type keys to ``AgentType`` enums and
    ``allowed_tools`` lists to ``frozenset`` (or ``None`` when the YAML
    value is ``null``).  Boolean flags are taken as-is.
    """
    from ...config import get_config

    config_caps = get_config("agent_capabilities", {})
    if not config_caps:
        return dict(_DEFAULT_AGENT_CAPABILITIES)

    merged: dict = {}
    for agent_type in AgentType:
        default_entry = _DEFAULT_AGENT_CAPABILITIES.get(agent_type, {})
        # Config uses string keys (e.g. "PLANNER"), not AgentType enums
        config_entry = config_caps.get(agent_type.value, {})
        if not config_entry:
            merged[agent_type] = dict(default_entry)
            continue

        entry: dict = {}
        # can_create_untrusted_arcs: bool
        if "can_create_untrusted_arcs" in config_entry:
            entry["can_create_untrusted_arcs"] = config_entry["can_create_untrusted_arcs"]
        else:
            entry["can_create_untrusted_arcs"] = default_entry.get("can_create_untrusted_arcs", False)

        # allowed_tools: frozenset, None, or list from config
        if "allowed_tools" in config_entry:
            raw = config_entry["allowed_tools"]
            if raw is None:
                entry["allowed_tools"] = None
            elif isinstance(raw, (list, set, frozenset)):
                entry["allowed_tools"] = frozenset(raw)
            else:
                entry["allowed_tools"] = default_entry.get("allowed_tools")
        else:
            entry["allowed_tools"] = default_entry.get("allowed_tools")

        merged[agent_type] = entry

    return merged


def validate_output_type(value: str) -> str:
    """Validate and return an output type string. Raises ValueError if invalid."""
    try:
        return OutputType(value).value
    except ValueError:
        valid = ", ".join(t.value for t in OutputType)
        raise ValueError(f"Invalid output_type '{value}'. Must be one of: {valid}")


def validate_agent_type(value: str) -> str:
    """Validate and return an agent type string. Raises ValueError if invalid."""
    try:
        return AgentType(value).value
    except ValueError:
        valid = ", ".join(t.value for t in AgentType)
        raise ValueError(f"Invalid agent_type '{value}'. Must be one of: {valid}")
