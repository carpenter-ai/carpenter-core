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
    """create_arc resolves agent_model to model_policy_id."""
    arc_id = arc_manager.create_arc(
        name="test-agent-model",
        goal="Test model selection",
        agent_model="opus",
    )
    assert arc_id > 0

    arc = arc_manager.get_arc(arc_id)
    assert arc is not None
    assert arc["model_policy_id"] is not None

    # Verify the policy has the resolved model
    policy = arc_manager.get_model_policy(arc["model_policy_id"])
    assert policy is not None
    assert "opus" in policy["model"]
    assert policy["model"].startswith("anthropic:")


def test_create_arc_agent_model_does_not_override_explicit_model():
    """Explicit model parameter takes precedence over agent_model."""
    arc_id = arc_manager.create_arc(
        name="test-explicit-model",
        goal="Test precedence",
        model="anthropic:claude-sonnet-4-6",
        agent_model="opus",  # Should be ignored because model is set
    )
    arc = arc_manager.get_arc(arc_id)
    policy = arc_manager.get_model_policy(arc["model_policy_id"])
    assert policy["model"] == "anthropic:claude-sonnet-4-6"


def test_add_child_with_agent_model():
    """add_child passes agent_model through to create_arc."""
    parent_id = arc_manager.create_arc(name="parent", goal="Parent arc")
    child_id = arc_manager.add_child(
        parent_id, name="child-haiku", goal="Use haiku", agent_model="haiku"
    )
    child = arc_manager.get_arc(child_id)
    assert child["model_policy_id"] is not None
    policy = arc_manager.get_model_policy(child["model_policy_id"])
    assert "haiku" in policy["model"]


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
    policy1 = arc_manager.get_model_policy(child1["model_policy_id"])
    assert "opus" in policy1["model"]

    # Second child should use haiku
    child2 = arc_manager.get_arc(arc_ids[1])
    policy2 = arc_manager.get_model_policy(child2["model_policy_id"])
    assert "haiku" in policy2["model"]


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


# ── Preset seeding idempotence ───────────────────────────────────
#
# The presets are re-seeded on every platform start with INSERT OR IGNORE.
# That is only idempotent because of the partial unique index over named
# selector policies; without it, OR IGNORE had no constraint to conflict on
# and every start appended another copy of every preset.


def _preset_rows(name=None):
    """Count model_policies rows, optionally for one preset name."""
    db = get_db()
    try:
        if name is None:
            return db.execute("SELECT COUNT(*) FROM model_policies").fetchone()[0]
        return db.execute(
            "SELECT COUNT(*) FROM model_policies WHERE name = ?", (name,)
        ).fetchone()[0]
    finally:
        db.close()


def _seed_presets():
    from carpenter.coordinator import Coordinator

    Coordinator()._ensure_model_policy_presets()


def test_preset_seeding_inserts_each_preset_once():
    """Seeding leaves exactly one row per preset name."""
    from carpenter.core.models.selector import get_presets

    _seed_presets()
    names = set(get_presets())
    assert _preset_rows() == len(names)
    for name in names:
        assert _preset_rows(name) == 1


def test_preset_seeding_twice_adds_nothing():
    """A second seeding pass (i.e. a restart) inserts no rows."""
    from carpenter.core.models.selector import get_presets

    _seed_presets()
    before = _preset_rows()
    _seed_presets()
    _seed_presets()
    assert _preset_rows() == before == len(get_presets())


def test_pinned_policy_rows_may_repeat_a_name():
    """Hard-pinned rows (policy_json NULL) are outside the index.

    get_or_create_model_policy deduplicates those itself, on
    (model, agent_role, temperature, max_tokens) — the name is free to
    repeat, so the index must not constrain it.
    """
    _seed_presets()
    id1 = arc_manager.get_or_create_model_policy(
        model="anthropic:claude-sonnet-4-6", name="careful-coding",
    )
    id2 = arc_manager.get_or_create_model_policy(
        model="anthropic:claude-haiku-4-5", name="careful-coding",
    )
    assert id1 != id2
    db = get_db()
    try:
        rows = db.execute(
            "SELECT COUNT(*) FROM model_policies "
            "WHERE name = 'careful-coding' AND policy_json IS NULL"
        ).fetchone()[0]
    finally:
        db.close()
    assert rows == 2


def test_template_step_resolution_reuses_the_seeded_preset_row():
    """Resolving a preset for a template step returns the seeded row's id.

    This is how an arc comes to reference a preset row: the resolved id is
    stored as arcs.model_policy_id. It is why the de-duplication migration
    repoints arcs instead of simply deleting the extra rows.
    """
    from carpenter.core.models.selector import get_presets

    _seed_presets()
    preset = get_presets()["careful-coding"]
    seeded_id = arc_manager.get_policy_id_by_name("careful-coding")
    resolved = arc_manager.get_or_create_model_policy(
        model=preset.model,
        agent_role=preset.agent_role,
        temperature=preset.temperature,
        max_tokens=preset.max_tokens,
        policy_json=preset.to_policy_json(),
        name="careful-coding",
    )
    assert resolved == seeded_id
    assert _preset_rows("careful-coding") == 1


def test_named_selector_policy_with_an_override_is_insertable():
    """A named selector policy that pins a model is outside the index.

    A name-only index would turn this plain INSERT into an IntegrityError
    once the same preset name had been seeded — reachable through a
    ``model_presets`` config override, which puts a model on a preset.
    """
    from carpenter.core.models.selector import get_presets

    _seed_presets()
    body = get_presets()["careful-coding"].to_policy_json()
    pinned = arc_manager.get_or_create_model_policy(
        model="anthropic:claude-opus-4-7",
        policy_json=body,
        name="careful-coding",
    )
    assert pinned != arc_manager.get_policy_id_by_name("careful-coding")
    # and it deduplicates on a repeat
    assert pinned == arc_manager.get_or_create_model_policy(
        model="anthropic:claude-opus-4-7",
        policy_json=body,
        name="careful-coding",
    )


def test_changed_preset_body_adds_a_row_instead_of_failing():
    """A preset whose body changed gets a second row, not an IntegrityError.

    Policy rows are immutable records that arcs point at, so a changed body
    cannot overwrite the old row. The index keys on (name, policy_json) so
    this stays an insert rather than a conflict.
    """
    _seed_presets()
    changed = json.dumps({"constraints": {"min_quality": 5}, "preference": [0.0, 1.0, 0.0]})
    new_id = arc_manager.get_or_create_model_policy(policy_json=changed, name="careful-coding")
    assert new_id != arc_manager.get_policy_id_by_name("careful-coding")
    assert _preset_rows("careful-coding") == 2
    # Still deduplicated on a repeat of the same body.
    assert new_id == arc_manager.get_or_create_model_policy(
        policy_json=changed, name="careful-coding",
    )
    assert _preset_rows("careful-coding") == 2
