"""Tests that resource.submit_verdict is whitelisted JUDGE-only.

Guards against config drift or whitelist regressions.  The dispatch
layer uses agent_type allowed_tools to gate tool calls; this tool must
never appear in the REVIEWER / EXECUTOR / PLANNER / CHAT sets, because
only JUDGE arcs are allowed to flip a Resource's template_verdict.
"""

from carpenter.core.trust.types import (
    AgentType,
    _DEFAULT_AGENT_CAPABILITIES,
    get_agent_capabilities,
)


TOOL = "resource.submit_verdict"


class TestDefaultWhitelist:
    def test_judge_has_tool(self):
        caps = _DEFAULT_AGENT_CAPABILITIES[AgentType.JUDGE]
        assert TOOL in caps["allowed_tools"]

    def test_reviewer_does_not_have_tool(self):
        caps = _DEFAULT_AGENT_CAPABILITIES[AgentType.REVIEWER]
        assert TOOL not in caps["allowed_tools"]

    def test_planner_does_not_have_tool(self):
        caps = _DEFAULT_AGENT_CAPABILITIES[AgentType.PLANNER]
        assert TOOL not in caps["allowed_tools"]

    def test_executor_is_unrestricted_but_still_not_explicitly_listed(self):
        """EXECUTOR uses allowed_tools=None (unrestricted via session).

        This is fine — EXECUTOR arcs are platform code; the gating for
        resource.submit_verdict is done by the handler's JUDGE check.
        We assert only that EXECUTOR's entry remains None (the existing
        convention) rather than accidentally being restricted.
        """
        caps = _DEFAULT_AGENT_CAPABILITIES[AgentType.EXECUTOR]
        assert caps["allowed_tools"] is None

    def test_chat_is_unrestricted(self):
        caps = _DEFAULT_AGENT_CAPABILITIES[AgentType.CHAT]
        assert caps["allowed_tools"] is None


class TestEffectiveCapabilities:
    """get_agent_capabilities() merges config overrides; defaults must win."""

    def test_judge_has_tool_via_getter(self):
        caps = get_agent_capabilities()
        assert TOOL in caps[AgentType.JUDGE]["allowed_tools"]

    def test_reviewer_does_not_have_tool_via_getter(self):
        caps = get_agent_capabilities()
        assert TOOL not in caps[AgentType.REVIEWER]["allowed_tools"]

    def test_planner_does_not_have_tool_via_getter(self):
        caps = get_agent_capabilities()
        assert TOOL not in caps[AgentType.PLANNER]["allowed_tools"]


class TestDispatchRegistration:
    def test_tool_registered_in_dispatch_table(self):
        from carpenter.api.callbacks import _DISPATCH
        assert TOOL in _DISPATCH
        # Also verify it's callable
        assert callable(_DISPATCH[TOOL])
