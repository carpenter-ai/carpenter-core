"""Integration tests for the dark-factory template and arc flow.

Tests the full dark-factory workflow: spec refinement -> scenario generation
-> iterative implementation loop -> completion gate. All AI calls and coding
agent execution are mocked. This is a STRUCTURAL test proving the arc system,
templates, iterative planning pattern, and data models compose correctly.
"""

import json
import os

import cattrs
import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import template_manager
from carpenter.db import get_db

# Import data models
from data_models.dark_factory import (
    DevelopmentSpec,
    TestScenario,
    TestSuite,
    ValidationResult,
    IterationFeedback,
)


# ── Helpers ─────────────────────────────────────────────────────────


def _set_arc_state(arc_id: int, key: str, value):
    """Set a value in arc_state (same pattern as coding_change_handler)."""
    db = get_db()
    try:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?) "
            "ON CONFLICT(arc_id, key) DO UPDATE SET value_json = excluded.value_json, "
            "updated_at = CURRENT_TIMESTAMP",
            (arc_id, key, json.dumps(value)),
        )
        db.commit()
    finally:
        db.close()


def _get_arc_state(arc_id: int, key: str, default=None):
    """Get a value from arc_state."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = ?",
            (arc_id, key),
        ).fetchone()
        return json.loads(row["value_json"]) if row else default
    finally:
        db.close()


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def dark_factory_template():
    """Load the dark-factory template and return its ID."""
    yaml_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "config_seed", "templates",
        "dark-factory.yaml",
    )
    template_id = template_manager.load_template(yaml_path)
    return template_id


@pytest.fixture
def sample_spec():
    """A sample DevelopmentSpec for testing."""
    return DevelopmentSpec(
        description="Implement a word frequency counter",
        requirements=[
            "Accept a string of text as input",
            "Return a dictionary of word -> count",
            "Handle punctuation and case-insensitivity",
        ],
        acceptance_criteria=[
            "Correctly counts words in simple sentences",
            "Handles mixed case (Hello == hello)",
            "Strips punctuation before counting",
        ],
        constraints=["No external dependencies"],
        language="python",
        target_directory="/tmp/wordcount",
    )


@pytest.fixture
def sample_test_suite():
    """A sample TestSuite with visible and holdout scenarios."""
    return TestSuite(
        scenarios=[
            TestScenario(
                name="basic_count",
                input={"text": "hello world hello"},
                expected_output={"hello": 2, "world": 1},
                category="functional",
            ),
            TestScenario(
                name="case_insensitive",
                input={"text": "Hello hello HELLO"},
                expected_output={"hello": 3},
                category="functional",
            ),
            TestScenario(
                name="with_punctuation",
                input={"text": "hello, world! hello."},
                expected_output={"hello": 2, "world": 1},
                category="edge_case",
            ),
        ],
        holdout_scenarios=[
            TestScenario(
                name="empty_string",
                input={"text": ""},
                expected_output={},
                category="edge_case",
            ),
            TestScenario(
                name="numbers_mixed",
                input={"text": "test 123 test"},
                expected_output={"test": 2, "123": 1},
                category="adversarial",
            ),
        ],
    )


# ── Test: Template Structure ────────────────────────────────────────


class TestTemplateStructure:
    """Tests that the dark-factory template creates the correct arc tree."""

    def test_template_loads(self, dark_factory_template):
        """Template loads from YAML and is stored in the database."""
        template = template_manager.get_template(dark_factory_template)
        assert template is not None
        assert template["name"] == "dark-factory"
        assert len(template["steps"]) == 4

    def test_template_creates_four_step_arc_structure(self, dark_factory_template):
        """Instantiating the template creates 4 child arcs with correct properties."""
        parent_id = arc_manager.create_arc(
            name="dark-factory-run",
            goal="Build a word frequency counter",
        )

        arc_ids = template_manager.instantiate_template(
            dark_factory_template, parent_id
        )
        assert len(arc_ids) == 4

        children = arc_manager.get_children(parent_id)
        assert len(children) == 4

        # Verify names and order
        names = [c["name"] for c in children]
        assert names == [
            "spec-refinement",
            "scenario-generation",
            "implementation-loop",
            "completion-gate",
        ]

        # Verify step_order values
        orders = [c["step_order"] for c in children]
        assert orders == [0, 1, 2, 3]

        # Verify from_template flag
        for child in children:
            assert child["from_template"] == 1  # SQLite stores bools as int

    def test_template_agent_types(self, dark_factory_template):
        """Each template step creates an arc with the correct agent_type."""
        parent_id = arc_manager.create_arc(
            name="dark-factory-run",
            goal="Test agent types",
        )

        arc_ids = template_manager.instantiate_template(
            dark_factory_template, parent_id
        )

        children = arc_manager.get_children(parent_id)
        agent_types = {c["name"]: c["agent_type"] for c in children}

        assert agent_types == {
            "spec-refinement": "CHAT",
            "scenario-generation": "EXECUTOR",
            "implementation-loop": "PLANNER",
            "completion-gate": "JUDGE",
        }

    def test_completion_gate_has_activation_event(self, dark_factory_template):
        """The completion-gate step has an arc.manual_trigger activation event."""
        parent_id = arc_manager.create_arc(
            name="dark-factory-run",
            goal="Test activations",
        )

        arc_ids = template_manager.instantiate_template(
            dark_factory_template, parent_id
        )

        # completion-gate is the last arc
        gate_arc_id = arc_ids[3]

        db = get_db()
        try:
            row = db.execute(
                "SELECT event_type FROM arc_activations WHERE arc_id = ?",
                (gate_arc_id,),
            ).fetchone()
            assert row is not None
            assert row["event_type"] == "arc.manual_trigger"
        finally:
            db.close()

    def test_template_rigidity_validation(self, dark_factory_template):
        """Template rigidity validates correctly after instantiation."""
        parent_id = arc_manager.create_arc(
            name="dark-factory-run",
            goal="Test rigidity",
        )
        # Store template_id on the parent for rigidity check
        db = get_db()
        try:
            db.execute(
                "UPDATE arcs SET template_id = ? WHERE id = ?",
                (dark_factory_template, parent_id),
            )
            db.commit()
        finally:
            db.close()

        template_manager.instantiate_template(dark_factory_template, parent_id)
        assert template_manager.validate_template_rigidity(parent_id) is True


# ── Test: Data Model Contracts ──────────────────────────────────────


class TestDataModelContracts:
    """Tests that data models serialize/deserialize correctly through arc state."""

    def test_development_spec_roundtrip(self, sample_spec):
        """DevelopmentSpec survives JSON serialization through arc_state."""
        arc_id = arc_manager.create_arc(name="spec-test")

        # Serialize to arc_state
        _set_arc_state(arc_id, "development_spec", cattrs.unstructure(sample_spec))

        # Deserialize back
        raw = _get_arc_state(arc_id, "development_spec")
        recovered = DevelopmentSpec(**raw)

        assert recovered.description == sample_spec.description
        assert recovered.requirements == sample_spec.requirements
        assert recovered.acceptance_criteria == sample_spec.acceptance_criteria
        assert recovered.constraints == sample_spec.constraints
        assert recovered.language == "python"

    def test_test_suite_roundtrip(self, sample_test_suite):
        """TestSuite with holdout scenarios survives serialization."""
        arc_id = arc_manager.create_arc(name="suite-test")

        _set_arc_state(arc_id, "test_suite", cattrs.unstructure(sample_test_suite))

        raw = _get_arc_state(arc_id, "test_suite")
        recovered = cattrs.structure(raw, TestSuite)

        assert len(recovered.scenarios) == 3
        assert len(recovered.holdout_scenarios) == 2
        assert recovered.scenarios[0].name == "basic_count"
        assert recovered.holdout_scenarios[0].name == "empty_string"

    def test_validation_result_roundtrip(self):
        """ValidationResult roundtrips through arc_state."""
        result = ValidationResult(
            passed=["basic_count", "case_insensitive"],
            failed=["with_punctuation"],
            errors=[],
            pass_rate=0.667,
            iteration=1,
            execution_traces={
                "with_punctuation": "AssertionError: {'hello,': 1} != {'hello': 2}",
            },
        )
        arc_id = arc_manager.create_arc(name="result-test")

        _set_arc_state(arc_id, "validation_result", cattrs.unstructure(result))

        raw = _get_arc_state(arc_id, "validation_result")
        recovered = ValidationResult(**raw)

        assert recovered.pass_rate == pytest.approx(0.667)
        assert recovered.failed == ["with_punctuation"]
        assert "with_punctuation" in recovered.execution_traces

    def test_iteration_feedback_roundtrip(self):
        """IterationFeedback roundtrips through arc_state."""
        feedback = IterationFeedback(
            failed_scenarios=["with_punctuation"],
            error_details=["Punctuation not stripped before counting"],
            suggestions=["Use str.translate() or regex to strip punctuation"],
            iteration=1,
            tokens_used=1500,
        )
        arc_id = arc_manager.create_arc(name="feedback-test")

        _set_arc_state(arc_id, "iteration_feedback", cattrs.unstructure(feedback))

        raw = _get_arc_state(arc_id, "iteration_feedback")
        recovered = IterationFeedback(**raw)

        assert recovered.iteration == 1
        assert recovered.tokens_used == 1500
        assert "str.translate()" in recovered.suggestions[0]


# ── Test: Implementation Loop Structure ─────────────────────────────


class TestImplementationLoop:
    """Tests the iterative implementation loop creates correct arc structure."""

    def _create_loop_parent(self):
        """Create a parent arc for the implementation loop."""
        root = arc_manager.create_arc(
            name="dark-factory-run",
            goal="Test implementation loop",
        )
        loop_id = arc_manager.add_child(
            root, "implementation-loop",
            goal="Iterative implementation",
            agent_type="PLANNER",
        )
        return root, loop_id

    def test_impl_validate_monitor_triplet_structure(self):
        """Implementation loop creates impl+validate+monitor triplets as children."""
        root, loop_id = self._create_loop_parent()

        # Activate the loop arc
        arc_manager.update_status(loop_id, "active")

        # Simulate iteration 1: create impl, validate, monitor children
        impl_1 = arc_manager.add_child(
            loop_id, "impl-1",
            goal="First implementation attempt",
            agent_type="EXECUTOR",
        )
        validate_1 = arc_manager.add_child(
            loop_id, "validate-1",
            goal="Run test scenarios against implementation 1",
            agent_type="EXECUTOR",
        )
        monitor_1 = arc_manager.add_child(
            loop_id, "monitor-1",
            goal="Check validation results and decide continue/done",
            agent_type="PLANNER",
        )

        children = arc_manager.get_children(loop_id)
        assert len(children) == 3

        names = [c["name"] for c in children]
        assert names == ["impl-1", "validate-1", "monitor-1"]

        # Step orders are auto-assigned sequentially
        orders = [c["step_order"] for c in children]
        assert orders == [0, 1, 2]

    def test_two_iteration_sibling_structure(self):
        """Two iterations produce 6 sibling children (3 per iteration)."""
        root, loop_id = self._create_loop_parent()
        arc_manager.update_status(loop_id, "active")

        # Iteration 1
        arc_manager.add_child(loop_id, "impl-1", agent_type="EXECUTOR")
        arc_manager.add_child(loop_id, "validate-1", agent_type="EXECUTOR")
        arc_manager.add_child(loop_id, "monitor-1", agent_type="PLANNER")

        # Iteration 2
        arc_manager.add_child(loop_id, "impl-2", agent_type="EXECUTOR")
        arc_manager.add_child(loop_id, "validate-2", agent_type="EXECUTOR")
        arc_manager.add_child(loop_id, "monitor-2", agent_type="PLANNER")

        children = arc_manager.get_children(loop_id)
        assert len(children) == 6

        names = [c["name"] for c in children]
        assert names == [
            "impl-1", "validate-1", "monitor-1",
            "impl-2", "validate-2", "monitor-2",
        ]

        # All step orders sequential
        orders = [c["step_order"] for c in children]
        assert orders == [0, 1, 2, 3, 4, 5]

    def test_monitor_continue_decision(self):
        """Monitor stores 'continue' decision when pass_rate < threshold."""
        root, loop_id = self._create_loop_parent()
        arc_manager.update_status(loop_id, "active")

        # Create and complete iteration 1 arcs
        impl_1 = arc_manager.add_child(loop_id, "impl-1", agent_type="EXECUTOR")
        validate_1 = arc_manager.add_child(loop_id, "validate-1", agent_type="EXECUTOR")
        monitor_1 = arc_manager.add_child(loop_id, "monitor-1", agent_type="PLANNER")

        # Mock validation result: 50% pass rate (below threshold)
        result_1 = ValidationResult(
            passed=["basic_count"],
            failed=["case_insensitive", "with_punctuation"],
            pass_rate=0.5,
            iteration=1,
        )
        _set_arc_state(validate_1, "validation_result", cattrs.unstructure(result_1))

        # Monitor logic: read validation result, decide based on pass_rate
        raw = _get_arc_state(validate_1, "validation_result")
        vr = ValidationResult(**raw)

        threshold = 0.95
        decision = "done" if vr.pass_rate >= threshold else "continue"
        assert decision == "continue"

        # Store decision on monitor arc
        _set_arc_state(monitor_1, "decision", decision)
        _set_arc_state(monitor_1, "pass_rate", vr.pass_rate)

        # Generate feedback for next iteration
        feedback = IterationFeedback(
            failed_scenarios=vr.failed,
            error_details=["case handling not implemented", "punctuation not stripped"],
            iteration=1,
        )
        _set_arc_state(monitor_1, "iteration_feedback", cattrs.unstructure(feedback))

        # Verify stored decision
        assert _get_arc_state(monitor_1, "decision") == "continue"
        recovered_feedback = IterationFeedback(**_get_arc_state(monitor_1, "iteration_feedback"))
        assert len(recovered_feedback.failed_scenarios) == 2

    def test_monitor_done_decision(self):
        """Monitor stores 'done' decision when pass_rate >= threshold."""
        root, loop_id = self._create_loop_parent()
        arc_manager.update_status(loop_id, "active")

        impl_1 = arc_manager.add_child(loop_id, "impl-1", agent_type="EXECUTOR")
        validate_1 = arc_manager.add_child(loop_id, "validate-1", agent_type="EXECUTOR")
        monitor_1 = arc_manager.add_child(loop_id, "monitor-1", agent_type="PLANNER")

        # Mock validation result: 100% pass rate (above threshold)
        result_1 = ValidationResult(
            passed=["basic_count", "case_insensitive", "with_punctuation"],
            failed=[],
            pass_rate=1.0,
            iteration=1,
        )
        _set_arc_state(validate_1, "validation_result", cattrs.unstructure(result_1))

        raw = _get_arc_state(validate_1, "validation_result")
        vr = ValidationResult(**raw)

        threshold = 0.95
        decision = "done" if vr.pass_rate >= threshold else "continue"
        assert decision == "done"

        _set_arc_state(monitor_1, "decision", decision)
        assert _get_arc_state(monitor_1, "decision") == "done"


# ── Test: Performance Counters ──────────────────────────────────────


class TestPerformanceCounters:
    """Tests that performance counters update correctly through the loop."""

    def test_descendant_arc_count_increments(self, dark_factory_template):
        """Parent arc's descendant_arc_count reflects all children and grandchildren."""
        parent_id = arc_manager.create_arc(
            name="dark-factory-run",
            goal="Counter test",
        )

        # Instantiate template (4 children)
        arc_ids = template_manager.instantiate_template(
            dark_factory_template, parent_id
        )

        # Recompute counters to be sure
        arc_manager.update_arc_counters(parent_id)

        parent = arc_manager.get_arc(parent_id)
        assert parent["descendant_arc_count"] == 4

    def test_loop_children_increment_ancestor_counts(self):
        """Adding loop children increments counters on both loop and root arcs."""
        root = arc_manager.create_arc(name="dark-factory-run")
        loop_id = arc_manager.add_child(
            root, "implementation-loop", agent_type="PLANNER",
        )
        arc_manager.update_status(loop_id, "active")

        # Add 3 children to loop (one iteration triplet)
        arc_manager.add_child(loop_id, "impl-1", agent_type="EXECUTOR")
        arc_manager.add_child(loop_id, "validate-1", agent_type="EXECUTOR")
        arc_manager.add_child(loop_id, "monitor-1", agent_type="PLANNER")

        # Recompute for accuracy
        arc_manager.update_arc_counters(loop_id)
        arc_manager.update_arc_counters(root)

        loop = arc_manager.get_arc(loop_id)
        assert loop["descendant_arc_count"] == 3

        root_arc = arc_manager.get_arc(root)
        # Root has: loop (1) + loop's 3 children = 4
        assert root_arc["descendant_arc_count"] == 4


# ── Test: Completion Gate ───────────────────────────────────────────


class TestCompletionGate:
    """Tests that the completion gate receives holdout scenarios for validation."""

    def test_holdout_scenarios_available_at_gate(self, sample_test_suite):
        """Holdout scenarios stored during generation are accessible at the gate."""
        root = arc_manager.create_arc(name="dark-factory-run")

        # Scenario generation stores suite including holdout
        gen_arc = arc_manager.add_child(root, "scenario-generation", agent_type="EXECUTOR")
        _set_arc_state(gen_arc, "test_suite", cattrs.unstructure(sample_test_suite))

        # Completion gate reads holdout from the scenario-generation arc's state
        raw = _get_arc_state(gen_arc, "test_suite")
        suite = cattrs.structure(raw, TestSuite)

        assert len(suite.holdout_scenarios) == 2
        holdout_names = [s.name for s in suite.holdout_scenarios]
        assert "empty_string" in holdout_names
        assert "numbers_mixed" in holdout_names

    def test_holdout_validation_result(self, sample_test_suite):
        """Completion gate can store holdout validation results for human review."""
        root = arc_manager.create_arc(name="dark-factory-run")
        gate_arc = arc_manager.add_child(
            root, "completion-gate", agent_type="JUDGE",
        )

        # Simulate holdout validation
        holdout_result = ValidationResult(
            passed=["empty_string"],
            failed=["numbers_mixed"],
            pass_rate=0.5,
            iteration=1,
            execution_traces={
                "numbers_mixed": "AssertionError: expected {'test': 2, '123': 1}",
            },
        )
        _set_arc_state(gate_arc, "holdout_result", cattrs.unstructure(holdout_result))

        # Verify result is accessible
        raw = _get_arc_state(gate_arc, "holdout_result")
        recovered = ValidationResult(**raw)
        assert recovered.pass_rate == 0.5
        assert "numbers_mixed" in recovered.failed


# ── Test: Full End-to-End Flow ──────────────────────────────────────


class TestEndToEndFlow:
    """Full end-to-end flow with 2 implementation iterations."""

    def test_full_dark_factory_flow(
        self, dark_factory_template, sample_spec, sample_test_suite
    ):
        """Complete dark-factory flow: spec -> scenarios -> 2 iterations -> gate."""
        # ── Step 0: Create root arc and instantiate template ──
        root_id = arc_manager.create_arc(
            name="dark-factory-run",
            goal="Build a word frequency counter",
        )
        arc_ids = template_manager.instantiate_template(
            dark_factory_template, root_id,
        )
        spec_arc, gen_arc, loop_arc, gate_arc = arc_ids

        # Verify initial structure
        children = arc_manager.get_children(root_id)
        assert len(children) == 4
        assert all(c["status"] == "pending" for c in children)

        # ── Step 1: Spec refinement (CHAT agent produces spec) ──
        arc_manager.update_status(spec_arc, "active")
        _set_arc_state(spec_arc, "development_spec", cattrs.unstructure(sample_spec))
        arc_manager.update_status(spec_arc, "completed")

        # Verify spec is accessible from downstream arcs
        raw_spec = _get_arc_state(spec_arc, "development_spec")
        assert DevelopmentSpec(**raw_spec).description == sample_spec.description

        # ── Step 2: Scenario generation (EXECUTOR produces test suite) ──
        arc_manager.update_status(gen_arc, "active")
        _set_arc_state(gen_arc, "test_suite", cattrs.unstructure(sample_test_suite))
        arc_manager.update_status(gen_arc, "completed")

        raw_suite = _get_arc_state(gen_arc, "test_suite")
        suite = cattrs.structure(raw_suite, TestSuite)
        assert len(suite.scenarios) == 3
        assert len(suite.holdout_scenarios) == 2

        # ── Step 3: Implementation loop ──
        # The loop arc is from_template=True with template_mutable=True,
        # so the PLANNER can add iteration children directly to it.
        arc_manager.update_status(loop_arc, "active")

        # ── Iteration 1: partial success ──
        impl_1 = arc_manager.add_child(
            loop_arc, "impl-1",
            goal="First implementation attempt",
            agent_type="EXECUTOR",
        )
        arc_manager.update_status(impl_1, "active")
        _set_arc_state(impl_1, "code_produced", True)
        _set_arc_state(impl_1, "approach", "Simple split and count")
        arc_manager.update_status(impl_1, "completed")

        validate_1 = arc_manager.add_child(
            loop_arc, "validate-1",
            goal="Run tests against implementation 1",
            agent_type="EXECUTOR",
        )
        arc_manager.update_status(validate_1, "active")

        result_1 = ValidationResult(
            passed=["basic_count"],
            failed=["case_insensitive", "with_punctuation"],
            pass_rate=1 / 3,
            iteration=1,
            execution_traces={
                "case_insensitive": "AssertionError: {'Hello': 1, 'hello': 1, 'HELLO': 1}",
                "with_punctuation": "AssertionError: {'hello,': 1, 'world!': 1, 'hello.': 1}",
            },
        )
        _set_arc_state(validate_1, "validation_result", cattrs.unstructure(result_1))
        arc_manager.update_status(validate_1, "completed")

        monitor_1 = arc_manager.add_child(
            loop_arc, "monitor-1",
            goal="Evaluate iteration 1 results",
            agent_type="PLANNER",
        )
        arc_manager.update_status(monitor_1, "active")

        # Monitor reads validation result and decides
        vr_1 = ValidationResult(**_get_arc_state(validate_1, "validation_result"))
        threshold = 0.95
        decision_1 = "done" if vr_1.pass_rate >= threshold else "continue"
        assert decision_1 == "continue"

        feedback_1 = IterationFeedback(
            failed_scenarios=vr_1.failed,
            error_details=[
                "Case handling: need .lower() before counting",
                "Punctuation: need to strip before splitting",
            ],
            suggestions=[
                "Use str.lower() on input",
                "Use str.translate() with str.maketrans to remove punctuation",
            ],
            iteration=1,
            tokens_used=500,
        )
        _set_arc_state(monitor_1, "decision", decision_1)
        _set_arc_state(monitor_1, "iteration_feedback", cattrs.unstructure(feedback_1))
        arc_manager.update_status(monitor_1, "completed")

        # ── Iteration 2: full success ──
        impl_2 = arc_manager.add_child(
            loop_arc, "impl-2",
            goal="Second attempt: fix case and punctuation handling",
            agent_type="EXECUTOR",
        )
        arc_manager.update_status(impl_2, "active")
        _set_arc_state(impl_2, "code_produced", True)
        _set_arc_state(impl_2, "approach", "lower() + translate() + split()")
        arc_manager.update_status(impl_2, "completed")

        validate_2 = arc_manager.add_child(
            loop_arc, "validate-2",
            goal="Run tests against implementation 2",
            agent_type="EXECUTOR",
        )
        arc_manager.update_status(validate_2, "active")

        result_2 = ValidationResult(
            passed=["basic_count", "case_insensitive", "with_punctuation"],
            failed=[],
            pass_rate=1.0,
            iteration=2,
        )
        _set_arc_state(validate_2, "validation_result", cattrs.unstructure(result_2))
        arc_manager.update_status(validate_2, "completed")

        monitor_2 = arc_manager.add_child(
            loop_arc, "monitor-2",
            goal="Evaluate iteration 2 results",
            agent_type="PLANNER",
        )
        arc_manager.update_status(monitor_2, "active")

        vr_2 = ValidationResult(**_get_arc_state(validate_2, "validation_result"))
        decision_2 = "done" if vr_2.pass_rate >= threshold else "continue"
        assert decision_2 == "done"

        _set_arc_state(monitor_2, "decision", decision_2)
        arc_manager.update_status(monitor_2, "completed")

        # Mark the loop arc as completed (planner done iterating)
        arc_manager.update_status(loop_arc, "completed")

        # ── Step 4: Completion gate (JUDGE) ──
        arc_manager.update_status(gate_arc, "active")

        # Run holdout scenarios
        holdout_result = ValidationResult(
            passed=["empty_string", "numbers_mixed"],
            failed=[],
            pass_rate=1.0,
            iteration=2,
        )
        _set_arc_state(gate_arc, "holdout_result", cattrs.unstructure(holdout_result))
        _set_arc_state(gate_arc, "iterations_used", 2)
        _set_arc_state(gate_arc, "final_pass_rate", 1.0)

        arc_manager.update_status(gate_arc, "completed")

        # ── Verify full arc tree ──
        # Root should have 4 children (all from template)
        all_children = arc_manager.get_children(root_id)
        assert len(all_children) == 4

        # Template children are still intact
        template_children = [c for c in all_children if c["from_template"]]
        assert len(template_children) == 4

        # Loop arc (mutable template) has 6 children (2 iterations x 3 arcs)
        iter_children = arc_manager.get_children(loop_arc)
        assert len(iter_children) == 6
        iter_names = [c["name"] for c in iter_children]
        assert iter_names == [
            "impl-1", "validate-1", "monitor-1",
            "impl-2", "validate-2", "monitor-2",
        ]

        # All arcs should be completed
        for child in all_children:
            arc_data = arc_manager.get_arc(child["id"])
            assert arc_data["status"] == "completed", (
                f"Arc {child['name']} has status {arc_data['status']}"
            )

        # ── Verify data model contracts ──
        # Each step's output deserializes into the expected Pydantic model
        assert DevelopmentSpec(**_get_arc_state(spec_arc, "development_spec"))
        assert cattrs.structure(_get_arc_state(gen_arc, "test_suite"), TestSuite)
        assert ValidationResult(**_get_arc_state(validate_1, "validation_result"))
        assert ValidationResult(**_get_arc_state(validate_2, "validation_result"))
        assert IterationFeedback(**_get_arc_state(monitor_1, "iteration_feedback"))
        assert ValidationResult(**_get_arc_state(gate_arc, "holdout_result"))

        # ── Verify performance counters ──
        arc_manager.update_arc_counters(root_id)
        root_arc = arc_manager.get_arc(root_id)
        # Root should have: 4 template + 6 iteration children under loop = 10 descendants
        assert root_arc["descendant_arc_count"] == 10

        arc_manager.update_arc_counters(loop_arc)
        loop_arc_data = arc_manager.get_arc(loop_arc)
        assert loop_arc_data["descendant_arc_count"] == 6

        # ── Verify final outputs ──
        assert _get_arc_state(gate_arc, "iterations_used") == 2
        assert _get_arc_state(gate_arc, "final_pass_rate") == 1.0
        gate_holdout = ValidationResult(**_get_arc_state(gate_arc, "holdout_result"))
        assert gate_holdout.pass_rate == 1.0
        assert gate_holdout.failed == []

    def test_single_iteration_success(
        self, dark_factory_template, sample_spec, sample_test_suite
    ):
        """Dark factory completes in a single iteration when first attempt passes."""
        root_id = arc_manager.create_arc(
            name="dark-factory-run",
            goal="Quick build",
        )
        arc_ids = template_manager.instantiate_template(
            dark_factory_template, root_id,
        )
        spec_arc, gen_arc, loop_arc, gate_arc = arc_ids

        # Spec + scenario generation
        arc_manager.update_status(spec_arc, "active")
        _set_arc_state(spec_arc, "development_spec", cattrs.unstructure(sample_spec))
        arc_manager.update_status(spec_arc, "completed")

        arc_manager.update_status(gen_arc, "active")
        _set_arc_state(gen_arc, "test_suite", cattrs.unstructure(sample_test_suite))
        arc_manager.update_status(gen_arc, "completed")

        # Single iteration: pass on first try using mutable loop arc directly
        arc_manager.update_status(loop_arc, "active")

        impl_1 = arc_manager.add_child(loop_arc, "impl-1", agent_type="EXECUTOR")
        arc_manager.update_status(impl_1, "active")
        arc_manager.update_status(impl_1, "completed")

        validate_1 = arc_manager.add_child(loop_arc, "validate-1", agent_type="EXECUTOR")
        arc_manager.update_status(validate_1, "active")
        result = ValidationResult(
            passed=["basic_count", "case_insensitive", "with_punctuation"],
            failed=[],
            pass_rate=1.0,
            iteration=1,
        )
        _set_arc_state(validate_1, "validation_result", cattrs.unstructure(result))
        arc_manager.update_status(validate_1, "completed")

        monitor_1 = arc_manager.add_child(loop_arc, "monitor-1", agent_type="PLANNER")
        arc_manager.update_status(monitor_1, "active")
        _set_arc_state(monitor_1, "decision", "done")
        arc_manager.update_status(monitor_1, "completed")

        arc_manager.update_status(loop_arc, "completed")

        # Completion gate with holdout
        arc_manager.update_status(gate_arc, "active")
        holdout_result = ValidationResult(
            passed=["empty_string", "numbers_mixed"],
            failed=[],
            pass_rate=1.0,
            iteration=1,
        )
        _set_arc_state(gate_arc, "holdout_result", cattrs.unstructure(holdout_result))
        arc_manager.update_status(gate_arc, "completed")

        # Verify only 1 iteration (3 children under mutable loop arc)
        iter_children = arc_manager.get_children(loop_arc)
        assert len(iter_children) == 3

        # Verify root has 4 children (all from template)
        all_children = arc_manager.get_children(root_id)
        assert len(all_children) == 4
