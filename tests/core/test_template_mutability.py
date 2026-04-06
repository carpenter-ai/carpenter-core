"""Tests for template mutability — mutable template arcs accept agent-created children."""

import os
import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import template_manager
from carpenter.db import get_db
from carpenter.tool_backends.arc import _PLAN_FIELDS


class TestTemplateMutability:

    def test_mutable_template_arc_allows_children(self):
        """from_template=True, template_mutable=True accepts add_child()."""
        parent = arc_manager.create_arc(name="root")
        mutable_arc = arc_manager.create_arc(
            name="mutable-step",
            parent_id=parent,
            from_template=True,
            template_mutable=True,
        )
        arc_manager.update_status(mutable_arc, "active")

        child_id = arc_manager.add_child(
            mutable_arc, "agent-child",
            goal="Created by agent",
            agent_type="EXECUTOR",
        )
        assert child_id is not None

        children = arc_manager.get_children(mutable_arc)
        assert len(children) == 1
        assert children[0]["name"] == "agent-child"

    def test_immutable_template_arc_rejects_children(self):
        """from_template=True, template_mutable=False raises ValueError."""
        parent = arc_manager.create_arc(name="root")
        immutable_arc = arc_manager.create_arc(
            name="immutable-step",
            parent_id=parent,
            from_template=True,
            template_mutable=False,
        )
        arc_manager.update_status(immutable_arc, "active")

        with pytest.raises(ValueError, match="from_template=True"):
            arc_manager.add_child(immutable_arc, "rejected-child")

    def test_default_template_mutable_is_false(self):
        """from_template=True without explicit mutable rejects children."""
        parent = arc_manager.create_arc(name="root")
        default_arc = arc_manager.create_arc(
            name="default-step",
            parent_id=parent,
            from_template=True,
        )
        arc_manager.update_status(default_arc, "active")

        with pytest.raises(ValueError, match="from_template=True"):
            arc_manager.add_child(default_arc, "rejected-child")

    def test_rigidity_valid_with_extra_children_on_mutable(self):
        """Mutable arc with agent-created children passes rigidity validation."""
        # Load template
        yaml_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "config_seed", "templates",
            "dark-factory.yaml",
        )
        template_id = template_manager.load_template(yaml_path)

        parent = arc_manager.create_arc(name="dark-factory-run")
        # Set template_id on parent for rigidity check
        db = get_db()
        try:
            db.execute(
                "UPDATE arcs SET template_id = ? WHERE id = ?",
                (template_id, parent),
            )
            db.commit()
        finally:
            db.close()

        arc_ids = template_manager.instantiate_template(template_id, parent)
        loop_arc = arc_ids[2]  # implementation-loop

        # Add agent-created children to the mutable loop arc
        arc_manager.update_status(loop_arc, "active")
        arc_manager.add_child(loop_arc, "impl-1", agent_type="EXECUTOR")
        arc_manager.add_child(loop_arc, "validate-1", agent_type="EXECUTOR")

        # Rigidity should still validate (only counts from_template children)
        assert template_manager.validate_template_rigidity(parent) is True

    def test_rigidity_fails_if_template_step_removed(self):
        """Removing a template step still fails validation even on mutable parent."""
        yaml_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "config_seed", "templates",
            "dark-factory.yaml",
        )
        template_id = template_manager.load_template(yaml_path)

        parent = arc_manager.create_arc(name="dark-factory-run")
        db = get_db()
        try:
            db.execute(
                "UPDATE arcs SET template_id = ? WHERE id = ?",
                (template_id, parent),
            )
            db.commit()
        finally:
            db.close()

        arc_ids = template_manager.instantiate_template(template_id, parent)

        # Delete a template step (simulate tampering)
        db = get_db()
        try:
            db.execute("DELETE FROM arcs WHERE id = ?", (arc_ids[0],))
            db.commit()
        finally:
            db.close()

        assert template_manager.validate_template_rigidity(parent) is False

    def test_dark_factory_template_mutable_loop(self):
        """Instantiate dark-factory, verify implementation-loop has template_mutable=True."""
        yaml_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "config_seed", "templates",
            "dark-factory.yaml",
        )
        template_id = template_manager.load_template(yaml_path)
        parent = arc_manager.create_arc(name="dark-factory-run")
        arc_ids = template_manager.instantiate_template(template_id, parent)

        loop_arc = arc_manager.get_arc(arc_ids[2])
        assert loop_arc["name"] == "implementation-loop"
        assert loop_arc["template_mutable"] == 1  # SQLite stores bools as int

    def test_dark_factory_template_immutable_steps(self):
        """spec-refinement, scenario-generation, completion-gate are NOT mutable."""
        yaml_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "config_seed", "templates",
            "dark-factory.yaml",
        )
        template_id = template_manager.load_template(yaml_path)
        parent = arc_manager.create_arc(name="dark-factory-run")
        arc_ids = template_manager.instantiate_template(template_id, parent)

        for idx in [0, 1, 3]:  # spec-refinement, scenario-generation, completion-gate
            arc = arc_manager.get_arc(arc_ids[idx])
            assert not arc["template_mutable"], (
                f"Arc {arc['name']} should not be mutable"
            )

    def test_plan_fields_includes_template_mutable(self):
        """arc.get_plan() returns template_mutable field."""
        assert "template_mutable" in _PLAN_FIELDS
