"""Data models for the dark factory autonomous development workflow.

These attrs models define the structured data contracts between
dark-factory arc steps. Each step produces output that the next step
consumes, with serialization/deserialization through arc_state.
"""
import attrs
from typing import Any


@attrs.define
class DevelopmentSpec:
    """Structured development specification produced by spec refinement."""
    description: str
    requirements: list[str]
    acceptance_criteria: list[str]
    constraints: list[str] = attrs.Factory(list)
    language: str = "python"
    target_directory: str = ""


@attrs.define
class TestScenario:
    """A single test scenario."""
    __test__ = False  # Prevent pytest collection

    name: str
    input: dict
    expected_output: Any
    category: str = "functional"  # functional, edge_case, adversarial


@attrs.define
class TestSuite:
    """Collection of test scenarios, split into visible and holdout sets."""
    __test__ = False  # Prevent pytest collection

    scenarios: list[TestScenario]
    holdout_scenarios: list[TestScenario] = attrs.Factory(list)


@attrs.define
class ValidationResult:
    """Results from running test scenarios against implementation."""
    passed: list[str]
    failed: list[str]
    errors: list[str] = attrs.Factory(list)
    pass_rate: float = 0.0
    iteration: int = 1
    execution_traces: dict[str, str] = attrs.Factory(dict)


@attrs.define
class IterationFeedback:
    """Feedback from a failed validation to guide next implementation."""
    failed_scenarios: list[str]
    error_details: list[str]
    suggestions: list[str] = attrs.Factory(list)
    iteration: int = 0
    tokens_used: int = 0


@attrs.define
class MonitorConfig:
    """Suggested configuration for iteration decisions."""
    pass_rate_threshold: float = 0.95
    max_iterations: int = 10
    diminishing_returns_window: int = 3
    diminishing_returns_min_delta: float = 0.05


@attrs.define
class DarkFactoryResult:
    """Final outcome of a dark-factory run."""
    status: str  # "success", "failed", "timeout"
    iterations_used: int = 0
    final_pass_rate: float = 0.0
    holdout_pass_rate: float = 0.0
