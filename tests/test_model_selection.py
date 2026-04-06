"""Tests for model selection as a planner-accessible resource.

Covers:
- Config schema: models section in DEFAULTS
- Model resolver: resolve_model_identifier, get_model_manifest, cost tier comparison
- Arc creation with agent_model parameter
- Template instantiation with agent_model and model_min_tier
- Config tool backend: handle_models
- Read tool: config.models()
"""

import json
import os

import pytest

from carpenter import config
from carpenter.agent import model_resolver
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import template_manager
from carpenter.db import get_db
from carpenter.tool_backends import config_tool


# ── Config schema tests ───────────────────────────────────────────


def test_models_in_defaults():
    """DEFAULTS contains a models section with expected structure."""
    models = config.DEFAULTS["models"]
    assert "opus" in models
    assert "sonnet" in models
    assert "haiku" in models


def test_model_entry_has_required_fields():
    """Each model entry has provider, model_id, description, cost_tier, context_window, roles."""
    required_fields = {"provider", "model_id", "description", "cost_tier", "context_window", "roles"}
    for key, entry in config.DEFAULTS["models"].items():
        missing = required_fields - set(entry.keys())
        assert not missing, f"Model {key!r} missing fields: {missing}"


def test_model_cost_tiers_are_valid():
    """All cost_tier values are in the recognized set."""
    valid_tiers = {"low", "medium", "high"}
    for key, entry in config.DEFAULTS["models"].items():
        assert entry["cost_tier"] in valid_tiers, f"Model {key!r} has invalid cost_tier: {entry['cost_tier']!r}"


def test_model_roles_are_valid():
    """All roles values are from the recognized set."""
    valid_roles = {"planning", "review", "implementation", "documentation", "summarization"}
    for key, entry in config.DEFAULTS["models"].items():
        for role in entry["roles"]:
            assert role in valid_roles, f"Model {key!r} has invalid role: {role!r}"


# ── Model resolver tests ─────────────────────────────────────────


def test_resolve_model_identifier_opus():
    """Resolve 'opus' to provider:model_id string."""
    result = model_resolver.resolve_model_identifier("opus")
    assert result.startswith("anthropic:")
    assert "opus" in result


def test_resolve_model_identifier_sonnet():
    """Resolve 'sonnet' to provider:model_id string."""
    result = model_resolver.resolve_model_identifier("sonnet")
    assert result.startswith("anthropic:")
    assert "sonnet" in result


def test_resolve_model_identifier_haiku():
    """Resolve 'haiku' to provider:model_id string."""
    result = model_resolver.resolve_model_identifier("haiku")
    assert result.startswith("anthropic:")
    assert "haiku" in result


def test_resolve_model_identifier_unknown():
    """ValueError for unknown identifier."""
    with pytest.raises(ValueError, match="Unknown model identifier"):
        model_resolver.resolve_model_identifier("nonexistent")


def test_get_model_manifest():
    """get_model_manifest returns all models."""
    manifest = model_resolver.get_model_manifest()
    assert "opus" in manifest
    assert "sonnet" in manifest
    assert "haiku" in manifest
    assert manifest["opus"]["cost_tier"] == "high"


def test_get_cost_tier():
    """get_cost_tier returns correct tier."""
    assert model_resolver.get_cost_tier("opus") == "high"
    assert model_resolver.get_cost_tier("sonnet") == "medium"
    assert model_resolver.get_cost_tier("haiku") == "low"


def test_get_cost_tier_unknown():
    """get_cost_tier raises for unknown model."""
    with pytest.raises(ValueError, match="Unknown model identifier"):
        model_resolver.get_cost_tier("nonexistent")


def test_compare_cost_tiers():
    """compare_cost_tiers returns correct ordering."""
    assert model_resolver.compare_cost_tiers("low", "high") < 0
    assert model_resolver.compare_cost_tiers("high", "low") > 0
    assert model_resolver.compare_cost_tiers("medium", "medium") == 0
    assert model_resolver.compare_cost_tiers("low", "medium") < 0
    assert model_resolver.compare_cost_tiers("high", "medium") > 0


def test_compare_cost_tiers_unknown():
    """compare_cost_tiers raises for unknown tier."""
    with pytest.raises(ValueError, match="Unknown cost tier"):
        model_resolver.compare_cost_tiers("low", "ultra")


def test_cost_tier_order_constant():
    """COST_TIER_ORDER is ordered low -> medium -> high."""
    assert model_resolver.COST_TIER_ORDER == ["low", "medium", "high"]


# ── Arc creation with agent_model ────────────────────────────────


def test_create_arc_with_agent_model():
    """create_arc resolves agent_model to agent_config_id."""
    arc_id = arc_manager.create_arc(
        name="test-agent-model",
        goal="Test model selection",
        agent_model="opus",
    )
    assert arc_id > 0

    arc = arc_manager.get_arc(arc_id)
    assert arc is not None
    assert arc["agent_config_id"] is not None

    # Verify the agent_config has the resolved model
    agent_config = arc_manager.get_agent_config(arc["agent_config_id"])
    assert agent_config is not None
    assert "opus" in agent_config["model"]
    assert agent_config["model"].startswith("anthropic:")


def test_create_arc_agent_model_does_not_override_explicit_model():
    """Explicit model parameter takes precedence over agent_model."""
    arc_id = arc_manager.create_arc(
        name="test-explicit-model",
        goal="Test precedence",
        model="anthropic:claude-sonnet-4-20250514",
        agent_model="opus",  # Should be ignored because model is set
    )
    arc = arc_manager.get_arc(arc_id)
    agent_config = arc_manager.get_agent_config(arc["agent_config_id"])
    assert agent_config["model"] == "anthropic:claude-sonnet-4-20250514"


def test_add_child_with_agent_model():
    """add_child passes agent_model through to create_arc."""
    parent_id = arc_manager.create_arc(name="parent", goal="Parent arc")
    child_id = arc_manager.add_child(
        parent_id, name="child-haiku", goal="Use haiku", agent_model="haiku"
    )
    child = arc_manager.get_arc(child_id)
    assert child["agent_config_id"] is not None
    agent_config = arc_manager.get_agent_config(child["agent_config_id"])
    assert "haiku" in agent_config["model"]


def test_create_arc_unknown_agent_model():
    """create_arc raises ValueError for unknown agent_model."""
    with pytest.raises(ValueError, match="Unknown model identifier"):
        arc_manager.create_arc(
            name="test-bad-model",
            goal="Should fail",
            agent_model="nonexistent",
        )


# ── Template instantiation with model_min_tier ───────────────────


def _create_template_yaml(tmp_path, steps, name="test-model-template"):
    """Helper: write a template YAML file and return its path."""
    import yaml
    template = {
        "name": name,
        "description": "Test template for model selection",
        "steps": steps,
    }
    yaml_path = tmp_path / "templates" / f"{name}.yaml"
    yaml_path.parent.mkdir(exist_ok=True)
    yaml_path.write_text(yaml.dump(template, default_flow_style=False))
    return str(yaml_path)


def test_template_with_agent_model(tmp_path):
    """Template step with agent_model creates arc with correct model."""
    yaml_path = _create_template_yaml(tmp_path, [
        {"name": "review-step", "description": "Security review", "order": 1,
         "agent_model": "opus"},
        {"name": "format-step", "description": "Format output", "order": 2,
         "agent_model": "haiku"},
    ])
    tid = template_manager.load_template(yaml_path)
    parent_id = arc_manager.create_arc(name="parent", goal="Test")
    arc_ids = template_manager.instantiate_template(tid, parent_id)

    assert len(arc_ids) == 2

    # First child should use opus
    child1 = arc_manager.get_arc(arc_ids[0])
    config1 = arc_manager.get_agent_config(child1["agent_config_id"])
    assert "opus" in config1["model"]

    # Second child should use haiku
    child2 = arc_manager.get_arc(arc_ids[1])
    config2 = arc_manager.get_agent_config(child2["agent_config_id"])
    assert "haiku" in config2["model"]


def test_template_model_min_tier_enforced(tmp_path):
    """Template step with model_min_tier rejects models below the tier."""
    yaml_path = _create_template_yaml(tmp_path, [
        {"name": "secure-review", "description": "Must use high tier", "order": 1,
         "agent_model": "haiku", "model_min_tier": "high"},
    ])
    tid = template_manager.load_template(yaml_path)
    parent_id = arc_manager.create_arc(name="parent", goal="Test")

    with pytest.raises(ValueError, match="below the required model_min_tier"):
        template_manager.instantiate_template(tid, parent_id)


def test_template_model_min_tier_passes(tmp_path):
    """Template step with model_min_tier passes when model meets tier."""
    yaml_path = _create_template_yaml(tmp_path, [
        {"name": "secure-review", "description": "Must use high tier", "order": 1,
         "agent_model": "opus", "model_min_tier": "high"},
    ])
    tid = template_manager.load_template(yaml_path)
    parent_id = arc_manager.create_arc(name="parent", goal="Test")
    arc_ids = template_manager.instantiate_template(tid, parent_id)
    assert len(arc_ids) == 1

    # Verify model_min_tier is stored in arc_state
    db = get_db()
    try:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = ?",
            (arc_ids[0], "_model_min_tier"),
        ).fetchone()
        assert row is not None
        assert json.loads(row["value_json"]) == "high"
    finally:
        db.close()


def test_template_model_min_tier_medium_with_sonnet(tmp_path):
    """Sonnet (medium) meets model_min_tier medium."""
    yaml_path = _create_template_yaml(tmp_path, [
        {"name": "standard-step", "description": "Medium tier OK", "order": 1,
         "agent_model": "sonnet", "model_min_tier": "medium"},
    ])
    tid = template_manager.load_template(yaml_path)
    parent_id = arc_manager.create_arc(name="parent", goal="Test")
    arc_ids = template_manager.instantiate_template(tid, parent_id)
    assert len(arc_ids) == 1


def test_template_no_agent_model_skips_min_tier_check(tmp_path):
    """model_min_tier without agent_model does not raise (check only applies to assignment)."""
    yaml_path = _create_template_yaml(tmp_path, [
        {"name": "flexible-step", "description": "No model assigned", "order": 1,
         "model_min_tier": "high"},
    ])
    tid = template_manager.load_template(yaml_path)
    parent_id = arc_manager.create_arc(name="parent", goal="Test")
    arc_ids = template_manager.instantiate_template(tid, parent_id)
    assert len(arc_ids) == 1


# ── Config tool backend tests ────────────────────────────────────


def test_handle_models_returns_manifest():
    """handle_models returns models dict."""
    result = config_tool.handle_models({})
    assert "models" in result
    models = result["models"]
    assert "opus" in models
    assert "sonnet" in models
    assert "haiku" in models
    assert models["opus"]["cost_tier"] == "high"


def test_handle_models_has_expected_fields():
    """handle_models returns entries with all required fields."""
    result = config_tool.handle_models({})
    required = {"provider", "model_id", "description", "cost_tier", "context_window", "roles"}
    for key, entry in result["models"].items():
        missing = required - set(entry.keys())
        assert not missing, f"Model {key!r} missing: {missing}"


# ── _enforce_min_tier unit tests ─────────────────────────────────


def test_enforce_min_tier_passes():
    """_enforce_min_tier does not raise when model meets tier."""
    # Should not raise
    template_manager._enforce_min_tier("opus", "high")
    template_manager._enforce_min_tier("sonnet", "medium")
    template_manager._enforce_min_tier("haiku", "low")
    template_manager._enforce_min_tier("opus", "low")  # above minimum


def test_enforce_min_tier_rejects():
    """_enforce_min_tier raises when model is below tier."""
    with pytest.raises(ValueError, match="below the required model_min_tier"):
        template_manager._enforce_min_tier("haiku", "medium")
    with pytest.raises(ValueError, match="below the required model_min_tier"):
        template_manager._enforce_min_tier("haiku", "high")
    with pytest.raises(ValueError, match="below the required model_min_tier"):
        template_manager._enforce_min_tier("sonnet", "high")
