"""Tests for the reflection template handler.

Tests the template-based reflection workflow: template loading, handler
creates parent arc and instantiates template, reflect arc goal is updated,
save-reflection intercept detection, save-reflection reads agent response,
quiet period handling, template-not-found error, daily model speed
update, and auto-action processing.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import template_manager, work_queue
from carpenter.core.workflows import reflection_template_handler
from carpenter.db import get_db


# ── Fixtures ─────────────────────────────────────────────────────────

REFLECTION_YAML = """\
name: reflection
description: Platform activity reflection and knowledge distillation
capabilities:
  - system.read
  - kb.write
steps:
  - name: reflect
    description: Analyze platform activity data and produce actionable reflection
    order: 0
    agent_type: EXECUTOR
    model_policy: background-batch
  - name: save-reflection
    description: Save reflection output and run post-processing
    order: 1
"""


@pytest.fixture
def reflection_template(tmp_path):
    """Load the reflection template into the database."""
    yaml_file = tmp_path / "reflection.yaml"
    yaml_file.write_text(REFLECTION_YAML)
    tid = template_manager.load_template(str(yaml_file))
    return tid


@pytest.fixture
def _enable_reflection(monkeypatch):
    """Enable reflection in config for tests that need it."""
    import carpenter.config
    cfg = carpenter.config.CONFIG.copy()
    cfg["reflection"] = {
        **cfg.get("reflection", {}),
        "enabled": True,
        "min_daily_conversations": 1,
        "auto_action": False,
    }
    monkeypatch.setattr("carpenter.config.CONFIG", cfg)


# ── Template loading ─────────────────────────────────────────────────

def test_reflection_template_loads(reflection_template):
    """The reflection template loads correctly with capabilities."""
    tmpl = template_manager.get_template(reflection_template)
    assert tmpl is not None
    assert tmpl["name"] == "reflection"
    assert "system.read" in tmpl["capabilities"]
    assert "kb.write" in tmpl["capabilities"]
    assert len(tmpl["steps"]) == 2
    assert tmpl["steps"][0]["name"] == "reflect"
    assert tmpl["steps"][1]["name"] == "save-reflection"


def test_reflection_template_by_name(reflection_template):
    """get_template_by_name finds the reflection template."""
    tmpl = template_manager.get_template_by_name("reflection")
    assert tmpl is not None
    assert tmpl["id"] == reflection_template


def test_reflection_template_instantiation(reflection_template):
    """Instantiating the reflection template creates correct child arcs."""
    parent_id = arc_manager.create_arc(
        "daily-reflection",
        goal="Daily reflection",
        agent_type="PLANNER",
        _allow_tainted=True,
    )
    arc_ids = template_manager.instantiate_template(reflection_template, parent_id)
    assert len(arc_ids) == 2

    children = arc_manager.get_children(parent_id)
    assert len(children) == 2

    names = [c["name"] for c in children]
    assert "reflect" in names
    assert "save-reflection" in names

    # Check capabilities were persisted on child arcs
    for child in children:
        db = get_db()
        try:
            row = db.execute(
                "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = ?",
                (child["id"], "_capabilities"),
            ).fetchone()
        finally:
            db.close()
        assert row is not None
        caps = json.loads(row["value_json"])
        assert "system.read" in caps
        assert "kb.write" in caps


# ── Handler creates parent arc and template ──────────────────────────

@pytest.mark.asyncio
async def test_handler_creates_parent_and_template(
    reflection_template, _enable_reflection,
):
    """handle_reflection_trigger creates parent arc, conversation, and template arcs."""
    with patch(
        "carpenter.core.workflows.reflection_template_handler.should_reflect",
        return_value=True,
    ), patch(
        "carpenter.core.workflows.reflection_template_handler.gather_daily_data",
        return_value="# Daily Data\nSome activity...",
    ):
        await reflection_template_handler.handle_reflection_trigger(
            work_id=1,
            payload={"event_payload": {"cadence": "daily"}},
        )

    # Find the parent arc
    db = get_db()
    try:
        parent_row = db.execute(
            "SELECT * FROM arcs WHERE name = 'daily-reflection' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        db.close()

    assert parent_row is not None
    parent_id = parent_row["id"]
    assert parent_row["agent_type"] == "PLANNER"

    # Check children were created from template
    children = arc_manager.get_children(parent_id)
    assert len(children) == 2
    names = [c["name"] for c in children]
    assert "reflect" in names
    assert "save-reflection" in names

    # Check conversation was created and linked
    db = get_db()
    try:
        conv_row = db.execute(
            "SELECT c.title FROM conversations c "
            "JOIN conversation_arcs ca ON c.id = ca.conversation_id "
            "WHERE ca.arc_id = ?",
            (parent_id,),
        ).fetchone()
    finally:
        db.close()
    assert conv_row is not None
    assert "Daily Reflection" in conv_row["title"]


# ── Reflect arc goal is updated with gathered data ───────────────────

@pytest.mark.asyncio
async def test_reflect_arc_goal_updated(reflection_template, _enable_reflection):
    """The reflect arc's goal is updated with gathered data."""
    gathered = "# Daily Reflection Data\n\nLots of activity here."

    with patch(
        "carpenter.core.workflows.reflection_template_handler.should_reflect",
        return_value=True,
    ), patch(
        "carpenter.core.workflows.reflection_template_handler.gather_daily_data",
        return_value=gathered,
    ):
        await reflection_template_handler.handle_reflection_trigger(
            work_id=1,
            payload={"event_payload": {"cadence": "daily"}},
        )

    # Find the reflect arc
    db = get_db()
    try:
        reflect_row = db.execute(
            "SELECT * FROM arcs WHERE name = 'reflect' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        db.close()

    assert reflect_row is not None
    assert reflect_row["goal"] == gathered


# ── is_reflection_save_step ──────────────────────────────────────────

def test_is_reflection_save_step_true(reflection_template):
    """is_reflection_save_step returns True for a valid save-reflection arc."""
    parent_id = arc_manager.create_arc(
        "daily-reflection",
        goal="Daily reflection",
        agent_type="PLANNER",
        _allow_tainted=True,
    )
    arc_ids = template_manager.instantiate_template(reflection_template, parent_id)

    # The second arc is save-reflection
    save_arc = arc_manager.get_arc(arc_ids[1])
    assert save_arc["name"] == "save-reflection"
    assert reflection_template_handler.is_reflection_save_step(save_arc) is True


def test_is_reflection_save_step_false_wrong_name():
    """is_reflection_save_step returns False if name doesn't match."""
    arc_info = {"name": "not-save-reflection", "from_template": True, "parent_id": 1}
    assert reflection_template_handler.is_reflection_save_step(arc_info) is False


def test_is_reflection_save_step_false_no_template():
    """is_reflection_save_step returns False if not from template."""
    arc_info = {"name": "save-reflection", "from_template": False, "parent_id": 1}
    assert reflection_template_handler.is_reflection_save_step(arc_info) is False


def test_is_reflection_save_step_false_no_parent():
    """is_reflection_save_step returns False if no parent."""
    arc_info = {"name": "save-reflection", "from_template": True, "parent_id": None}
    assert reflection_template_handler.is_reflection_save_step(arc_info) is False


def test_is_reflection_save_step_false_wrong_parent():
    """is_reflection_save_step returns False if parent name doesn't match."""
    parent_id = arc_manager.create_arc(
        "some-other-workflow",
        goal="Something else",
        agent_type="PLANNER",
        _allow_tainted=True,
    )
    arc_info = {"name": "save-reflection", "from_template": True, "parent_id": parent_id}
    assert reflection_template_handler.is_reflection_save_step(arc_info) is False


# ── save-reflection reads agent response ─────────────────────────────

@pytest.mark.asyncio
async def test_save_reflection_reads_agent_response(reflection_template, _enable_reflection):
    """handle_save_reflection reads _agent_response from sibling reflect arc."""
    parent_id = arc_manager.create_arc(
        "daily-reflection",
        goal="Daily reflection",
        agent_type="PLANNER",
        _allow_tainted=True,
    )

    # Store metadata on parent
    reflection_template_handler._set_arc_state(parent_id, "cadence", "daily")
    reflection_template_handler._set_arc_state(
        parent_id, "period_start", "2025-01-01T00:00:00+00:00"
    )
    reflection_template_handler._set_arc_state(
        parent_id, "period_end", "2025-01-02T00:00:00+00:00"
    )

    arc_ids = template_manager.instantiate_template(reflection_template, parent_id)
    reflect_arc_id = arc_ids[0]
    save_arc_id = arc_ids[1]

    # Simulate reflect arc completed with agent response
    arc_manager.update_status(reflect_arc_id, "active")
    arc_manager.update_status(reflect_arc_id, "completed")
    arc_manager.freeze_arc(reflect_arc_id)
    reflection_template_handler._set_arc_state(
        reflect_arc_id, "_agent_response", "AI generated reflection content here."
    )

    # Create conversation linked to parent (so archive works)
    from carpenter.agent import conversation as conv_module
    conv_id = conv_module.create_conversation()
    conv_module.link_arc_to_conversation(conv_id, parent_id)

    save_arc_info = arc_manager.get_arc(save_arc_id)

    with patch(
        "carpenter.core.workflows.reflection_template_handler.model_resolver"
    ) as mock_resolver:
        mock_resolver.get_model_for_role.return_value = "test-model"
        await reflection_template_handler.handle_save_reflection(
            save_arc_id, save_arc_info
        )

    # Verify reflection was saved in DB
    db = get_db()
    try:
        refl_row = db.execute(
            "SELECT * FROM reflections ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        db.close()

    assert refl_row is not None
    assert refl_row["cadence"] == "daily"
    assert refl_row["content"] == "AI generated reflection content here."
    assert refl_row["model"] == "test-model"

    # Verify the save arc was completed
    save_arc = arc_manager.get_arc(save_arc_id)
    assert save_arc["status"] in ("completed", "frozen")

    # Verify conversation was archived
    db = get_db()
    try:
        conv_row = db.execute(
            "SELECT archived FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()
    finally:
        db.close()
    assert conv_row["archived"] == 1


@pytest.mark.asyncio
async def test_save_reflection_no_response(reflection_template, _enable_reflection):
    """handle_save_reflection uses fallback text when no agent response exists."""
    parent_id = arc_manager.create_arc(
        "daily-reflection",
        goal="Daily reflection",
        agent_type="PLANNER",
        _allow_tainted=True,
    )

    reflection_template_handler._set_arc_state(parent_id, "cadence", "daily")
    reflection_template_handler._set_arc_state(
        parent_id, "period_start", "2025-01-01T00:00:00+00:00"
    )
    reflection_template_handler._set_arc_state(
        parent_id, "period_end", "2025-01-02T00:00:00+00:00"
    )

    arc_ids = template_manager.instantiate_template(reflection_template, parent_id)
    reflect_arc_id = arc_ids[0]
    save_arc_id = arc_ids[1]

    # Complete reflect arc WITHOUT storing _agent_response
    arc_manager.update_status(reflect_arc_id, "active")
    arc_manager.update_status(reflect_arc_id, "completed")
    arc_manager.freeze_arc(reflect_arc_id)

    from carpenter.agent import conversation as conv_module
    conv_id = conv_module.create_conversation()
    conv_module.link_arc_to_conversation(conv_id, parent_id)

    save_arc_info = arc_manager.get_arc(save_arc_id)

    with patch(
        "carpenter.core.workflows.reflection_template_handler.model_resolver"
    ) as mock_resolver:
        mock_resolver.get_model_for_role.return_value = "test-model"
        await reflection_template_handler.handle_save_reflection(
            save_arc_id, save_arc_info
        )

    # Should save with fallback text
    db = get_db()
    try:
        refl_row = db.execute(
            "SELECT * FROM reflections ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        db.close()

    assert refl_row is not None
    assert refl_row["content"] == "(No reflection output)"


# ── Quiet period handling ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quiet_period_skips_template(reflection_template, _enable_reflection):
    """Below-threshold activity saves minimal reflection without creating arcs."""
    with patch(
        "carpenter.core.workflows.reflection_template_handler.should_reflect",
        return_value=False,
    ):
        await reflection_template_handler.handle_reflection_trigger(
            work_id=1,
            payload={"event_payload": {"cadence": "daily"}},
        )

    # A reflection should have been saved
    db = get_db()
    try:
        refl_row = db.execute(
            "SELECT * FROM reflections ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        db.close()

    assert refl_row is not None
    assert refl_row["cadence"] == "daily"
    assert "Quiet period" in refl_row["content"]

    # No parent arcs should have been created
    db = get_db()
    try:
        arc_row = db.execute(
            "SELECT * FROM arcs WHERE name = 'daily-reflection'"
        ).fetchone()
    finally:
        db.close()

    assert arc_row is None


# ── Template not found fallback ──────────────────────────────────────

@pytest.mark.asyncio
async def test_template_not_found_raises_error(_enable_reflection):
    """When reflection template is missing, raises RuntimeError."""
    # Don't load the template -- it won't be found

    with patch(
        "carpenter.core.workflows.reflection_template_handler.should_reflect",
        return_value=True,
    ), patch(
        "carpenter.core.workflows.reflection_template_handler.gather_daily_data",
        return_value="# Some data",
    ), pytest.raises(RuntimeError, match="Reflection template not found"):
        await reflection_template_handler.handle_reflection_trigger(
            work_id=1,
            payload={"event_payload": {"cadence": "daily"}},
        )


# ── Daily cadence triggers model speed update ────────────────────────

@pytest.mark.asyncio
async def test_daily_reflection_updates_model_speeds(
    reflection_template, _enable_reflection,
):
    """Daily cadence save-reflection triggers model speed update."""
    parent_id = arc_manager.create_arc(
        "daily-reflection",
        goal="Daily reflection",
        agent_type="PLANNER",
        _allow_tainted=True,
    )

    reflection_template_handler._set_arc_state(parent_id, "cadence", "daily")
    reflection_template_handler._set_arc_state(
        parent_id, "period_start", "2025-01-01T00:00:00+00:00"
    )
    reflection_template_handler._set_arc_state(
        parent_id, "period_end", "2025-01-02T00:00:00+00:00"
    )

    arc_ids = template_manager.instantiate_template(reflection_template, parent_id)
    reflect_arc_id = arc_ids[0]
    save_arc_id = arc_ids[1]

    arc_manager.update_status(reflect_arc_id, "active")
    arc_manager.update_status(reflect_arc_id, "completed")
    arc_manager.freeze_arc(reflect_arc_id)
    reflection_template_handler._set_arc_state(
        reflect_arc_id, "_agent_response", "reflection text"
    )

    from carpenter.agent import conversation as conv_module
    conv_id = conv_module.create_conversation()
    conv_module.link_arc_to_conversation(conv_id, parent_id)

    save_arc_info = arc_manager.get_arc(save_arc_id)

    with patch(
        "carpenter.core.workflows.reflection_template_handler.model_resolver"
    ) as mock_resolver, patch(
        "carpenter.core.models.speed_tracker.update_registry_speeds",
        return_value=3,
    ) as mock_speed:
        mock_resolver.get_model_for_role.return_value = "test-model"
        await reflection_template_handler.handle_save_reflection(
            save_arc_id, save_arc_info
        )

    mock_speed.assert_called_once()


# ── Auto-action processing ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_auto_action_processing(
    reflection_template, monkeypatch,
):
    """Auto-action processing runs when config enables it."""
    import carpenter.config
    cfg = carpenter.config.CONFIG.copy()
    cfg["reflection"] = {
        **cfg.get("reflection", {}),
        "enabled": True,
        "auto_action": True,
    }
    monkeypatch.setattr("carpenter.config.CONFIG", cfg)

    parent_id = arc_manager.create_arc(
        "daily-reflection",
        goal="Daily reflection",
        agent_type="PLANNER",
        _allow_tainted=True,
    )

    reflection_template_handler._set_arc_state(parent_id, "cadence", "daily")
    reflection_template_handler._set_arc_state(
        parent_id, "period_start", "2025-01-01T00:00:00+00:00"
    )
    reflection_template_handler._set_arc_state(
        parent_id, "period_end", "2025-01-02T00:00:00+00:00"
    )

    arc_ids = template_manager.instantiate_template(reflection_template, parent_id)
    reflect_arc_id = arc_ids[0]
    save_arc_id = arc_ids[1]

    arc_manager.update_status(reflect_arc_id, "active")
    arc_manager.update_status(reflect_arc_id, "completed")
    arc_manager.freeze_arc(reflect_arc_id)
    reflection_template_handler._set_arc_state(
        reflect_arc_id, "_agent_response", "reflection text"
    )

    from carpenter.agent import conversation as conv_module
    conv_id = conv_module.create_conversation()
    conv_module.link_arc_to_conversation(conv_id, parent_id)

    save_arc_info = arc_manager.get_arc(save_arc_id)

    with patch(
        "carpenter.core.workflows.reflection_template_handler.model_resolver"
    ) as mock_resolver, patch(
        "carpenter.agent.reflection_action.process_reflection_actions",
    ) as mock_action:
        mock_resolver.get_model_for_role.return_value = "test-model"
        await reflection_template_handler.handle_save_reflection(
            save_arc_id, save_arc_info
        )

    mock_action.assert_called_once()


# ── Weekly and monthly cadences ──────────────────────────────────────

@pytest.mark.asyncio
async def test_weekly_reflection(reflection_template, _enable_reflection):
    """Weekly cadence creates correct parent arc name and gathers weekly data."""
    with patch(
        "carpenter.core.workflows.reflection_template_handler.should_reflect",
        return_value=True,
    ), patch(
        "carpenter.core.workflows.reflection_template_handler.gather_weekly_data",
        return_value="# Weekly Data\nSome weekly activity...",
    ):
        await reflection_template_handler.handle_reflection_trigger(
            work_id=1,
            payload={"event_payload": {"cadence": "weekly"}},
        )

    db = get_db()
    try:
        parent_row = db.execute(
            "SELECT * FROM arcs WHERE name = 'weekly-reflection' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        db.close()

    assert parent_row is not None

    # Check reflect arc goal was set with weekly data
    db = get_db()
    try:
        reflect_row = db.execute(
            "SELECT goal FROM arcs WHERE name = 'reflect' AND parent_id = ?",
            (parent_row["id"],),
        ).fetchone()
    finally:
        db.close()

    assert reflect_row is not None
    assert "Weekly Data" in reflect_row["goal"]


@pytest.mark.asyncio
async def test_monthly_reflection(reflection_template, _enable_reflection):
    """Monthly cadence creates correct parent arc name and gathers monthly data."""
    with patch(
        "carpenter.core.workflows.reflection_template_handler.should_reflect",
        return_value=True,
    ), patch(
        "carpenter.core.workflows.reflection_template_handler.gather_monthly_data",
        return_value="# Monthly Data\nSome monthly activity...",
    ):
        await reflection_template_handler.handle_reflection_trigger(
            work_id=1,
            payload={"event_payload": {"cadence": "monthly"}},
        )

    db = get_db()
    try:
        parent_row = db.execute(
            "SELECT * FROM arcs WHERE name = 'monthly-reflection' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        db.close()

    assert parent_row is not None


# ── Unknown cadence ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_cadence_is_ignored(reflection_template, _enable_reflection):
    """Unknown cadence returns early without creating anything."""
    await reflection_template_handler.handle_reflection_trigger(
        work_id=1,
        payload={"event_payload": {"cadence": "hourly"}},
    )

    db = get_db()
    try:
        arc_row = db.execute(
            "SELECT * FROM arcs WHERE name LIKE '%reflection%'"
        ).fetchone()
    finally:
        db.close()

    assert arc_row is None


# ── Reflect arc is enqueued for dispatch ─────────────────────────────

@pytest.mark.asyncio
async def test_reflect_arc_enqueued(reflection_template, _enable_reflection):
    """The reflect arc is enqueued for dispatch after template instantiation."""
    with patch(
        "carpenter.core.workflows.reflection_template_handler.should_reflect",
        return_value=True,
    ), patch(
        "carpenter.core.workflows.reflection_template_handler.gather_daily_data",
        return_value="# Some data",
    ):
        await reflection_template_handler.handle_reflection_trigger(
            work_id=1,
            payload={"event_payload": {"cadence": "daily"}},
        )

    # Find the reflect arc
    db = get_db()
    try:
        reflect_row = db.execute(
            "SELECT id FROM arcs WHERE name = 'reflect' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        db.close()

    assert reflect_row is not None
    reflect_arc_id = reflect_row["id"]

    # Check it was enqueued
    db = get_db()
    try:
        wq_row = db.execute(
            "SELECT * FROM work_queue WHERE event_type = 'arc.dispatch' "
            "AND payload_json = ? AND status = 'pending'",
            (json.dumps({"arc_id": reflect_arc_id}),),
        ).fetchone()
    finally:
        db.close()

    assert wq_row is not None


# ── Parent arc_state stores metadata ─────────────────────────────────

@pytest.mark.asyncio
async def test_parent_arc_state_metadata(reflection_template, _enable_reflection):
    """Parent arc_state stores cadence, period_start, period_end."""
    with patch(
        "carpenter.core.workflows.reflection_template_handler.should_reflect",
        return_value=True,
    ), patch(
        "carpenter.core.workflows.reflection_template_handler.gather_daily_data",
        return_value="# Data",
    ):
        await reflection_template_handler.handle_reflection_trigger(
            work_id=1,
            payload={"event_payload": {"cadence": "daily"}},
        )

    db = get_db()
    try:
        parent_row = db.execute(
            "SELECT id FROM arcs WHERE name = 'daily-reflection' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        db.close()

    parent_id = parent_row["id"]
    assert reflection_template_handler._get_arc_state(parent_id, "cadence") == "daily"
    assert reflection_template_handler._get_arc_state(parent_id, "period_start") is not None
    assert reflection_template_handler._get_arc_state(parent_id, "period_end") is not None


# ── Register handlers ────────────────────────────────────────────────

def test_register_handlers():
    """register_handlers registers the reflection.trigger event."""
    registered = {}

    def mock_register(event_type, handler):
        registered[event_type] = handler

    reflection_template_handler.register_handlers(mock_register)
    assert "reflection.trigger" in registered
    assert registered["reflection.trigger"] is reflection_template_handler.handle_reflection_trigger
