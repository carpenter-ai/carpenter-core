"""Whitelist tests for ``resource.create`` (Phase B PR B1).

Guards the agent-type allowed_tools policy decision:
  - EXECUTOR: allowed_tools=None (unrestricted — tool is implicitly allowed).
  - PLANNER: allowed_tools contains "resource.create".
  - REVIEWER: allowed_tools contains "resource.create".
  - JUDGE: allowed_tools does NOT contain "resource.create" (kept narrow).
  - CHAT: allowed_tools=None (chat is a boundary; in practice it spawns
    arcs rather than creating Resources directly).

The intent is documented in ``core/trust/types.py`` near the
_DEFAULT_AGENT_CAPABILITIES mapping.
"""

from carpenter.core.trust.types import (
    AgentType,
    _DEFAULT_AGENT_CAPABILITIES,
    get_agent_capabilities,
)


TOOL = "resource.create"


class TestDefaultWhitelist:
    def test_planner_has_tool(self):
        caps = _DEFAULT_AGENT_CAPABILITIES[AgentType.PLANNER]
        assert TOOL in caps["allowed_tools"]

    def test_reviewer_has_tool(self):
        caps = _DEFAULT_AGENT_CAPABILITIES[AgentType.REVIEWER]
        assert TOOL in caps["allowed_tools"]

    def test_judge_does_not_have_tool(self):
        caps = _DEFAULT_AGENT_CAPABILITIES[AgentType.JUDGE]
        assert TOOL not in caps["allowed_tools"]

    def test_executor_is_unrestricted(self):
        """EXECUTOR uses allowed_tools=None so the tool is implicitly allowed."""
        caps = _DEFAULT_AGENT_CAPABILITIES[AgentType.EXECUTOR]
        assert caps["allowed_tools"] is None

    def test_chat_is_unrestricted(self):
        """CHAT uses allowed_tools=None; in practice it would spawn arcs."""
        caps = _DEFAULT_AGENT_CAPABILITIES[AgentType.CHAT]
        assert caps["allowed_tools"] is None


class TestEffectiveCapabilities:
    """get_agent_capabilities() merges config overrides; defaults must win."""

    def test_planner_has_tool_via_getter(self):
        caps = get_agent_capabilities()
        assert TOOL in caps[AgentType.PLANNER]["allowed_tools"]

    def test_reviewer_has_tool_via_getter(self):
        caps = get_agent_capabilities()
        assert TOOL in caps[AgentType.REVIEWER]["allowed_tools"]

    def test_judge_does_not_have_tool_via_getter(self):
        caps = get_agent_capabilities()
        assert TOOL not in caps[AgentType.JUDGE]["allowed_tools"]


class TestDispatchRegistration:
    def test_tool_registered_in_dispatch_table(self):
        from carpenter.api.callbacks import _DISPATCH
        assert TOOL in _DISPATCH
        assert callable(_DISPATCH[TOOL])
