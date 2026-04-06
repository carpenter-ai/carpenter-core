"""Tests for verification sibling arc pattern.

Verifies:
- Verification arcs are auto-created when a coding arc completes
- Self-verification is rejected (verification_target_id != id)
- Sibling relationship is enforced (shared parent_id)
- The coding-change template includes verification steps
- arc_role validation works
- Documentation arc is created after judge
"""

import json
import os
import shutil

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import template_manager
from carpenter.core.arcs import verification as verification_arcs
from carpenter.db import get_db


TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "config_seed", "templates")


def _copy_templates(tmp_path):
    """Copy template files to tmp_path for test isolation."""
    dest = str(tmp_path / "templates")
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(TEMPLATES_DIR):
        if f.endswith((".yaml", ".yml")):
            shutil.copy(os.path.join(TEMPLATES_DIR, f), dest)
    return dest


# ── Verification arc creation ─────────────────────────────────────


class TestVerificationArcCreation:
    """Tests that verification arcs are correctly created."""

    def test_create_verification_arcs_basic(self, monkeypatch):
        """Verification arcs are created for a completed coding arc."""
        # Enable verification
        import carpenter.config
        cfg = dict(carpenter.config.CONFIG)
        cfg["verification"] = {"enabled": True}
        monkeypatch.setattr("carpenter.config.CONFIG", cfg)

        parent = arc_manager.create_arc("project", goal="Test project")
        impl_id = arc_manager.add_child(parent, "coding-change", goal="Implement feature")
        arc_manager.update_status(impl_id, "active")
        arc_manager.update_status(impl_id, "completed")

        # Create verification arcs
        v_ids = verification_arcs.create_verification_arcs(impl_id)

        assert len(v_ids) >= 3  # correctness + judge + docs (quality may be skipped)

        # All verification arcs should exist and be siblings
        for v_id in v_ids:
            v_arc = arc_manager.get_arc(v_id)
            assert v_arc is not None
            assert v_arc["parent_id"] == parent

        # First arc should be correctness (quality skipped for non-platform code)
        correctness = arc_manager.get_arc(v_ids[0])
        assert correctness["name"] == "verify-correctness"
        assert correctness["arc_role"] == "verifier"
        assert correctness["verification_target_id"] == impl_id
        assert correctness["agent_type"] == "REVIEWER"

    def test_create_verification_arcs_platform_code(self, monkeypatch, tmp_path):
        """Quality check arc is created for platform/tool code, runs before correctness."""
        import carpenter.config
        cfg = dict(carpenter.config.CONFIG)
        cfg["verification"] = {"enabled": True}
        monkeypatch.setattr("carpenter.config.CONFIG", cfg)

        parent = arc_manager.create_arc("project", goal="Test project")
        impl_id = arc_manager.add_child(
            parent, "coding-change-platform",
            goal=f"Modify platform code in {tmp_path / 'platform'}",
        )
        arc_manager.update_status(impl_id, "active")
        arc_manager.update_status(impl_id, "completed")

        v_ids = verification_arcs.create_verification_arcs(impl_id)

        # Should have quality + correctness + judge + docs = 4
        assert len(v_ids) == 4

        names = [arc_manager.get_arc(v)["name"] for v in v_ids]
        assert "verify-quality" in names
        assert "verify-correctness" in names
        assert "judge-verification" in names
        assert "post-verification-docs" in names

        # Quality runs BEFORE correctness (security: static review gates test execution)
        quality = arc_manager.get_arc(v_ids[0])
        correctness = arc_manager.get_arc(v_ids[1])
        assert quality["name"] == "verify-quality"
        assert correctness["name"] == "verify-correctness"
        assert quality["step_order"] < correctness["step_order"]

    def test_verification_arcs_not_created_for_non_coding(self, monkeypatch):
        """Verification arcs are not created for non-coding arcs."""
        import carpenter.config
        cfg = dict(carpenter.config.CONFIG)
        cfg["verification"] = {"enabled": True}
        monkeypatch.setattr("carpenter.config.CONFIG", cfg)

        arc_id = arc_manager.create_arc("chat-task", goal="Just chatting")
        arc_manager.update_status(arc_id, "active")
        arc_manager.update_status(arc_id, "completed")

        arc_info = arc_manager.get_arc(arc_id)
        assert not verification_arcs.should_create_verification_arcs(arc_info)

    def test_verification_arcs_not_created_when_disabled(self, monkeypatch):
        """Verification arcs are not created when verification is disabled."""
        import carpenter.config
        cfg = dict(carpenter.config.CONFIG)
        cfg["verification"] = {"enabled": False}
        monkeypatch.setattr("carpenter.config.CONFIG", cfg)

        parent = arc_manager.create_arc("project", goal="Test")
        impl_id = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(impl_id, "active")
        arc_manager.update_status(impl_id, "completed")

        arc_info = arc_manager.get_arc(impl_id)
        assert not verification_arcs.should_create_verification_arcs(arc_info)

    def test_verification_arcs_not_recursive(self, monkeypatch):
        """Verifier arcs do not themselves trigger more verification arcs."""
        import carpenter.config
        cfg = dict(carpenter.config.CONFIG)
        cfg["verification"] = {"enabled": True}
        monkeypatch.setattr("carpenter.config.CONFIG", cfg)

        parent = arc_manager.create_arc("project", goal="Test")
        verifier_id = arc_manager.create_arc(
            "coding-change-verify",
            goal="Verify something",
            parent_id=parent,
            arc_role="verifier",
        )
        arc_manager.update_status(verifier_id, "active")
        arc_manager.update_status(verifier_id, "completed")

        arc_info = arc_manager.get_arc(verifier_id)
        assert not verification_arcs.should_create_verification_arcs(arc_info)

    def test_verification_arcs_history_logged(self, monkeypatch):
        """Verification arc creation is logged in the implementation arc's history."""
        import carpenter.config
        cfg = dict(carpenter.config.CONFIG)
        cfg["verification"] = {"enabled": True}
        monkeypatch.setattr("carpenter.config.CONFIG", cfg)

        parent = arc_manager.create_arc("project", goal="Test")
        impl_id = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(impl_id, "active")
        arc_manager.update_status(impl_id, "completed")

        verification_arcs.create_verification_arcs(impl_id)

        history = arc_manager.get_history(impl_id)
        verification_entries = [
            h for h in history if h["entry_type"] == "verification_arc_created"
        ]
        assert len(verification_entries) >= 3  # correctness + judge + docs

    def test_documentation_arc_is_worker_role(self, monkeypatch):
        """Documentation arc has arc_role='worker' not 'verifier'."""
        import carpenter.config
        cfg = dict(carpenter.config.CONFIG)
        cfg["verification"] = {"enabled": True}
        monkeypatch.setattr("carpenter.config.CONFIG", cfg)

        parent = arc_manager.create_arc("project", goal="Test")
        impl_id = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(impl_id, "active")
        arc_manager.update_status(impl_id, "completed")

        v_ids = verification_arcs.create_verification_arcs(impl_id)

        # Last arc should be documentation
        docs_arc = arc_manager.get_arc(v_ids[-1])
        assert docs_arc["name"] == "post-verification-docs"
        assert docs_arc["arc_role"] == "worker"
        assert docs_arc["agent_type"] == "EXECUTOR"

    def test_judge_depends_on_checks(self, monkeypatch):
        """Judge arc has higher step_order than check arcs."""
        import carpenter.config
        cfg = dict(carpenter.config.CONFIG)
        cfg["verification"] = {"enabled": True}
        monkeypatch.setattr("carpenter.config.CONFIG", cfg)

        parent = arc_manager.create_arc("project", goal="Test")
        impl_id = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(impl_id, "active")
        arc_manager.update_status(impl_id, "completed")

        v_ids = verification_arcs.create_verification_arcs(impl_id)

        # For non-platform code: correctness, judge, docs (3 arcs)
        correctness = arc_manager.get_arc(v_ids[0])
        judge = arc_manager.get_arc(v_ids[1])  # second (no quality for non-platform)
        docs = arc_manager.get_arc(v_ids[2])

        assert correctness["name"] == "verify-correctness"
        assert judge["name"] == "judge-verification"
        assert docs["name"] == "post-verification-docs"
        assert judge["step_order"] > correctness["step_order"]
        assert docs["step_order"] > judge["step_order"]

    def test_create_verification_arcs_pre_approval(self, monkeypatch):
        """Verification arcs can be created for an active (not completed) arc."""
        import carpenter.config
        cfg = dict(carpenter.config.CONFIG)
        cfg["verification"] = {"enabled": True}
        monkeypatch.setattr("carpenter.config.CONFIG", cfg)

        parent = arc_manager.create_arc("project", goal="Test")
        impl_id = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(impl_id, "active")
        # NOT completed — testing pre-approval creation

        v_ids = verification_arcs.create_verification_arcs(
            impl_id, require_completed=False,
        )
        assert len(v_ids) >= 3  # correctness + judge + docs

        # All arcs should reference the implementation arc
        for v_id in v_ids:
            v_arc = arc_manager.get_arc(v_id)
            assert v_arc is not None

    def test_pre_approval_rejected_when_require_completed(self, monkeypatch):
        """Default require_completed=True rejects non-completed arcs."""
        import carpenter.config
        cfg = dict(carpenter.config.CONFIG)
        cfg["verification"] = {"enabled": True}
        monkeypatch.setattr("carpenter.config.CONFIG", cfg)

        parent = arc_manager.create_arc("project", goal="Test")
        impl_id = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(impl_id, "active")

        with pytest.raises(ValueError, match="expected 'completed'"):
            verification_arcs.create_verification_arcs(impl_id)

    def test_docs_arc_has_verification_target_id(self, monkeypatch):
        """Documentation arc has verification_target_id set."""
        import carpenter.config
        cfg = dict(carpenter.config.CONFIG)
        cfg["verification"] = {"enabled": True}
        monkeypatch.setattr("carpenter.config.CONFIG", cfg)

        parent = arc_manager.create_arc("project", goal="Test")
        impl_id = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(impl_id, "active")
        arc_manager.update_status(impl_id, "completed")

        v_ids = verification_arcs.create_verification_arcs(impl_id)
        docs = arc_manager.get_arc(v_ids[-1])
        assert docs["name"] == "post-verification-docs"
        assert docs["verification_target_id"] == impl_id


# ── Self-verification rejection ───────────────────────────────────


class TestSelfVerificationRejection:
    """Tests that self-verification is rejected."""

    def test_self_verification_rejected_at_creation(self):
        """Cannot create an arc with verification_target_id == its own id.

        Since the arc doesn't exist yet at creation time, we test the
        validation function directly with a pre-existing arc.
        """
        parent = arc_manager.create_arc("project", goal="Test")
        existing_id = arc_manager.add_child(parent, "some-arc")

        db = get_db()
        try:
            with pytest.raises(ValueError, match="Self-verification not allowed"):
                arc_manager._validate_verification_target(
                    db, existing_id, parent, arc_id=existing_id,
                )
        finally:
            db.close()

    def test_self_verification_same_id(self):
        """_validate_verification_target rejects target == arc_id."""
        parent = arc_manager.create_arc("project", goal="Test")
        arc_id = arc_manager.add_child(parent, "target")

        db = get_db()
        try:
            with pytest.raises(ValueError, match="Self-verification not allowed"):
                arc_manager._validate_verification_target(
                    db, arc_id, parent, arc_id=arc_id,
                )
        finally:
            db.close()


# ── Sibling relationship enforcement ──────────────────────────────


class TestSiblingEnforcement:
    """Tests that verification target must be a sibling (shared parent_id)."""

    def test_non_sibling_target_rejected(self):
        """Cannot set verification_target_id to an arc with different parent."""
        parent_a = arc_manager.create_arc("project-a", goal="Project A")
        parent_b = arc_manager.create_arc("project-b", goal="Project B")
        target_in_a = arc_manager.add_child(parent_a, "target")

        # Try to create a verifier under parent_b that targets an arc under parent_a
        with pytest.raises(ValueError, match="not a sibling"):
            arc_manager.create_arc(
                name="verifier",
                goal="Verify",
                parent_id=parent_b,
                arc_role="verifier",
                verification_target_id=target_in_a,
            )

    def test_sibling_target_accepted(self):
        """Can set verification_target_id to a sibling arc (same parent)."""
        parent = arc_manager.create_arc("project", goal="Test")
        target = arc_manager.add_child(parent, "implementation")
        arc_manager.update_status(target, "active")
        arc_manager.update_status(target, "completed")

        verifier = arc_manager.create_arc(
            name="verifier",
            goal="Verify implementation",
            parent_id=parent,
            arc_role="verifier",
            verification_target_id=target,
        )

        v_arc = arc_manager.get_arc(verifier)
        assert v_arc["verification_target_id"] == target
        assert v_arc["arc_role"] == "verifier"
        assert v_arc["parent_id"] == parent

    def test_nonexistent_target_rejected(self):
        """Cannot set verification_target_id to a nonexistent arc."""
        parent = arc_manager.create_arc("project", goal="Test")

        with pytest.raises(ValueError, match="not found"):
            arc_manager.create_arc(
                name="verifier",
                goal="Verify",
                parent_id=parent,
                arc_role="verifier",
                verification_target_id=99999,
            )


# ── arc_role validation ───────────────────────────────────────────


class TestArcRoleValidation:
    """Tests for arc_role field validation."""

    def test_valid_arc_roles(self):
        """All valid arc_role values are accepted."""
        for role in ("coordinator", "worker", "verifier"):
            arc_id = arc_manager.create_arc(f"test-{role}", arc_role=role)
            arc = arc_manager.get_arc(arc_id)
            assert arc["arc_role"] == role

    def test_invalid_arc_role_rejected(self):
        """Invalid arc_role values are rejected."""
        with pytest.raises(ValueError, match="Invalid arc_role"):
            arc_manager.create_arc("bad-arc", arc_role="invalid")

    def test_default_arc_role_is_worker(self):
        """Default arc_role is 'worker'."""
        arc_id = arc_manager.create_arc("default-arc")
        arc = arc_manager.get_arc(arc_id)
        assert arc["arc_role"] == "worker"


# ── Template verification steps ───────────────────────────────────


class TestTemplateVerificationSteps:
    """Tests that the coding-change template includes verification steps."""

    def test_coding_change_template_has_verification_steps(self, tmp_path):
        """The coding-change template YAML includes verification steps."""
        templates_dir = _copy_templates(tmp_path)
        yaml_path = os.path.join(templates_dir, "coding-change.yaml")
        tid = template_manager.load_template(yaml_path)
        template = template_manager.get_template(tid)

        step_names = [s["name"] for s in template["steps"]]
        assert "verify-correctness" in step_names
        assert "verify-quality" in step_names
        assert "judge-verification" in step_names
        assert "post-verification-docs" in step_names

    def test_template_verification_steps_have_correct_roles(self, tmp_path):
        """Verification steps in the template have arc_role='verifier'."""
        templates_dir = _copy_templates(tmp_path)
        yaml_path = os.path.join(templates_dir, "coding-change.yaml")
        tid = template_manager.load_template(yaml_path)
        template = template_manager.get_template(tid)

        step_map = {s["name"]: s for s in template["steps"]}

        assert step_map["verify-correctness"].get("arc_role") == "verifier"
        assert step_map["verify-quality"].get("arc_role") == "verifier"
        assert step_map["judge-verification"].get("arc_role") == "verifier"
        # Documentation step should NOT be a verifier
        assert step_map["post-verification-docs"].get("arc_role") is None

    def test_template_verification_step_ordering(self, tmp_path):
        """Verification runs before human approval: quality → correctness → judge → docs → approval."""
        templates_dir = _copy_templates(tmp_path)
        yaml_path = os.path.join(templates_dir, "coding-change.yaml")
        tid = template_manager.load_template(yaml_path)
        template = template_manager.get_template(tid)

        step_map = {s["name"]: s for s in template["steps"]}

        # Quality gates correctness (sequential, not parallel)
        assert step_map["verify-quality"]["order"] < step_map["verify-correctness"]["order"]
        # Correctness before judge
        assert step_map["verify-correctness"]["order"] < step_map["judge-verification"]["order"]
        # Judge before docs
        assert step_map["judge-verification"]["order"] < step_map["post-verification-docs"]["order"]
        # Docs before human approval
        assert step_map["post-verification-docs"]["order"] < step_map["await-approval"]["order"]
        # Implementation before verification
        assert step_map["generate-review"]["order"] < step_map["verify-quality"]["order"]

    def test_template_instantiation_creates_verification_arcs(self, tmp_path):
        """Instantiating the coding-change template creates verification step arcs."""
        templates_dir = _copy_templates(tmp_path)
        yaml_path = os.path.join(templates_dir, "coding-change.yaml")
        tid = template_manager.load_template(yaml_path)

        parent = arc_manager.create_arc("coding-change-parent", goal="Test")
        arc_ids = template_manager.instantiate_template(tid, parent)

        # 7 steps total: invoke-agent, generate-review, verify-quality,
        # verify-correctness, judge-verification, post-verification-docs, await-approval
        assert len(arc_ids) == 7

        children = arc_manager.get_children(parent)
        names = [c["name"] for c in children]
        assert "verify-correctness" in names
        assert "verify-quality" in names
        assert "judge-verification" in names
        assert "post-verification-docs" in names
        assert "await-approval" in names

        # Check that verification steps got arc_role from template
        for child in children:
            if child["name"] in ("verify-correctness", "verify-quality", "judge-verification"):
                assert child["arc_role"] == "verifier", (
                    f"Expected arc_role='verifier' for {child['name']}, got '{child['arc_role']}'"
                )


# ── Integration: should_create_verification_arcs ──────────────────


class TestShouldCreateVerificationArcs:
    """Tests for the should_create_verification_arcs predicate."""

    def test_returns_true_for_completed_coding_arc(self, monkeypatch):
        """Returns True for a completed coding arc when verification is enabled."""
        import carpenter.config
        cfg = dict(carpenter.config.CONFIG)
        cfg["verification"] = {"enabled": True}
        monkeypatch.setattr("carpenter.config.CONFIG", cfg)

        parent = arc_manager.create_arc("project", goal="Test")
        impl_id = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(impl_id, "active")
        arc_manager.update_status(impl_id, "completed")

        arc_info = arc_manager.get_arc(impl_id)
        assert verification_arcs.should_create_verification_arcs(arc_info)

    def test_returns_false_for_incomplete_coding_arc(self, monkeypatch):
        """Returns False if the coding arc is not yet completed (default)."""
        import carpenter.config
        cfg = dict(carpenter.config.CONFIG)
        cfg["verification"] = {"enabled": True}
        monkeypatch.setattr("carpenter.config.CONFIG", cfg)

        parent = arc_manager.create_arc("project", goal="Test")
        impl_id = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(impl_id, "active")

        arc_info = arc_manager.get_arc(impl_id)
        assert not verification_arcs.should_create_verification_arcs(arc_info)

    def test_returns_true_for_active_arc_with_require_completed_false(self, monkeypatch):
        """Returns True for an active coding arc when require_completed=False."""
        import carpenter.config
        cfg = dict(carpenter.config.CONFIG)
        cfg["verification"] = {"enabled": True}
        monkeypatch.setattr("carpenter.config.CONFIG", cfg)

        parent = arc_manager.create_arc("project", goal="Test")
        impl_id = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(impl_id, "active")

        arc_info = arc_manager.get_arc(impl_id)
        assert verification_arcs.should_create_verification_arcs(
            arc_info, require_completed=False,
        )

    def test_returns_false_for_failed_coding_arc(self, monkeypatch):
        """Returns False if the coding arc failed."""
        import carpenter.config
        cfg = dict(carpenter.config.CONFIG)
        cfg["verification"] = {"enabled": True}
        monkeypatch.setattr("carpenter.config.CONFIG", cfg)

        parent = arc_manager.create_arc("project", goal="Test")
        impl_id = arc_manager.add_child(parent, "coding-change", goal="Implement")
        arc_manager.update_status(impl_id, "active")
        arc_manager.update_status(impl_id, "failed")

        arc_info = arc_manager.get_arc(impl_id)
        assert not verification_arcs.should_create_verification_arcs(arc_info)
