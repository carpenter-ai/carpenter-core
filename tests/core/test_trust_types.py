"""Tests for carpenter.core.trust_types."""

import pytest

from carpenter.core.trust.types import (
    IntegrityLevel,
    OutputType,
    AgentType,
    _DEFAULT_AGENT_CAPABILITIES,
    get_agent_capabilities,
    validate_integrity_level,
    validate_output_type,
    validate_agent_type,
)


# ── _DEFAULT_AGENT_CAPABILITIES ──────────────────────

def test_all_agent_types_have_capabilities():
    for at in AgentType:
        assert at in _DEFAULT_AGENT_CAPABILITIES, f"{at} missing from _DEFAULT_AGENT_CAPABILITIES"


def test_planner_cannot_read_untrusted():
    caps = _DEFAULT_AGENT_CAPABILITIES[AgentType.PLANNER]
    assert caps["can_read_untrusted"] is False


def test_planner_has_allowed_tools():
    caps = _DEFAULT_AGENT_CAPABILITIES[AgentType.PLANNER]
    assert "arc.create" in caps["allowed_tools"]
    assert "arc.get_plan" in caps["allowed_tools"]
    assert "web.get" not in caps["allowed_tools"]


def test_reviewer_can_read_untrusted():
    caps = _DEFAULT_AGENT_CAPABILITIES[AgentType.REVIEWER]
    assert caps["can_read_untrusted"] is True


def test_reviewer_allowed_tools_include_verdict():
    caps = _DEFAULT_AGENT_CAPABILITIES[AgentType.REVIEWER]
    assert "review.submit_verdict" in caps["allowed_tools"]
    assert "arc.read_output_UNTRUSTED" in caps["allowed_tools"]


def test_executor_allowed_tools_is_none():
    caps = _DEFAULT_AGENT_CAPABILITIES[AgentType.EXECUTOR]
    assert caps["allowed_tools"] is None


# ── get_agent_capabilities() ─────────────────────────────────────────

def test_get_agent_capabilities_returns_defaults_with_empty_config(monkeypatch):
    """With no config override, getter returns the hardcoded defaults."""
    import carpenter.config as cfg
    monkeypatch.setitem(cfg.CONFIG, "agent_capabilities", {})
    result = get_agent_capabilities()
    for at in AgentType:
        assert at in result, f"{at} missing from get_agent_capabilities()"
    assert result[AgentType.PLANNER]["can_read_untrusted"] is False
    assert result[AgentType.EXECUTOR]["allowed_tools"] is None


def test_get_agent_capabilities_config_override_tools(monkeypatch):
    """Config can override allowed_tools for a specific agent type."""
    import carpenter.config as cfg
    monkeypatch.setitem(cfg.CONFIG, "agent_capabilities", {
        "PLANNER": {
            "allowed_tools": ["arc.create", "arc.get"],
        },
    })
    result = get_agent_capabilities()
    planner_tools = result[AgentType.PLANNER]["allowed_tools"]
    assert isinstance(planner_tools, frozenset)
    assert planner_tools == frozenset({"arc.create", "arc.get"})
    # Boolean flags should still come from defaults
    assert result[AgentType.PLANNER]["can_read_untrusted"] is False
    assert result[AgentType.PLANNER]["can_create_untrusted_arcs"] is True


def test_get_agent_capabilities_config_override_booleans(monkeypatch):
    """Config can override boolean flags."""
    import carpenter.config as cfg
    monkeypatch.setitem(cfg.CONFIG, "agent_capabilities", {
        "REVIEWER": {
            "can_read_untrusted": False,
        },
    })
    result = get_agent_capabilities()
    assert result[AgentType.REVIEWER]["can_read_untrusted"] is False
    # allowed_tools should come from defaults
    assert "review.submit_verdict" in result[AgentType.REVIEWER]["allowed_tools"]


def test_get_agent_capabilities_null_tools_stays_none(monkeypatch):
    """Config null for allowed_tools means unrestricted (None)."""
    import carpenter.config as cfg
    monkeypatch.setitem(cfg.CONFIG, "agent_capabilities", {
        "EXECUTOR": {
            "allowed_tools": None,
        },
    })
    result = get_agent_capabilities()
    assert result[AgentType.EXECUTOR]["allowed_tools"] is None


def test_get_agent_capabilities_unmentioned_types_use_defaults(monkeypatch):
    """Agent types not mentioned in config keep their default capabilities."""
    import carpenter.config as cfg
    monkeypatch.setitem(cfg.CONFIG, "agent_capabilities", {
        "PLANNER": {"can_read_untrusted": True},
    })
    result = get_agent_capabilities()
    # REVIEWER should be unchanged from defaults
    assert result[AgentType.REVIEWER]["can_read_untrusted"] is True
    assert "review.submit_verdict" in result[AgentType.REVIEWER]["allowed_tools"]
    # JUDGE should be unchanged
    assert result[AgentType.JUDGE]["allowed_tools"] == frozenset()


# ── Validation functions ─────────────────────────────────────────────

def test_validate_integrity_level_valid():
    assert validate_integrity_level("trusted") == "trusted"
    assert validate_integrity_level("constrained") == "constrained"
    assert validate_integrity_level("untrusted") == "untrusted"


def test_validate_integrity_level_invalid():
    with pytest.raises(ValueError, match="Invalid integrity_level"):
        validate_integrity_level("dirty")


def test_validate_output_type_valid():
    assert validate_output_type("python") == "python"
    assert validate_output_type("text") == "text"


def test_validate_output_type_invalid():
    with pytest.raises(ValueError, match="Invalid output_type"):
        validate_output_type("xml")


def test_validate_agent_type_valid():
    assert validate_agent_type("PLANNER") == "PLANNER"
    assert validate_agent_type("EXECUTOR") == "EXECUTOR"


def test_validate_agent_type_invalid():
    with pytest.raises(ValueError, match="Invalid agent_type"):
        validate_agent_type("ADMIN")
