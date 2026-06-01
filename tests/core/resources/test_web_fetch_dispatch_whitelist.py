"""Whitelist tests for ``web.fetch_webpage_to_resource`` (Phase B PR B3).

Guards the agent-type allowed_tools policy decision:
  - EXECUTOR: allowed_tools=None (unrestricted — tool is implicitly allowed).
  - PLANNER: allowed_tools contains "web.fetch_webpage_to_resource".
  - REVIEWER: allowed_tools contains "web.fetch_webpage_to_resource".
  - JUDGE: allowed_tools does NOT contain the tool (kept narrow — JUDGE
    is deterministic platform code with only ``resource.submit_verdict``).
  - CHAT: allowed_tools=None — but in practice chat agents should use the
    higher-level ``fetch_web_content`` chat tool which wraps the full
    reviewed pipeline (B3 is dispatch-only for arcs).

See ``core/trust/types.py`` near ``_DEFAULT_AGENT_CAPABILITIES``.
"""

from carpenter.core.trust.types import (
    AgentType,
    _DEFAULT_AGENT_CAPABILITIES,
    get_agent_capabilities,
)


TOOL = "web.fetch_webpage_to_resource"


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
        """CHAT uses allowed_tools=None; in practice it uses fetch_web_content."""
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


class TestExternalAccessClassification:
    def test_tool_is_external_access(self):
        """Fetch-to-Resource hits an external URL, so it should be gated
        like other web.* tools via external_access classification."""
        from carpenter.api.callbacks import get_external_access_tools
        assert TOOL in get_external_access_tools()
