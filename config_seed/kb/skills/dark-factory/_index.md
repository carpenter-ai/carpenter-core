# Dark Factory

Autonomous spec-driven development workflow. A PLANNER agent orchestrates the entire process using existing arc tools — no special platform machinery required.

## Overview

The dark factory is a **template + agent strategy** for autonomous code generation with iterative validation. The template provides the scaffold (4 steps); the PLANNER agent makes all orchestration decisions.

## Template Structure

```
dark-factory-run (PLANNER, root)
|-- spec-refinement (CHAT, from_template)
|-- scenario-generation (EXECUTOR, from_template)
|-- implementation-loop (PLANNER, from_template, mutable)
|   |-- impl-1 (EXECUTOR, created by planner)
|   |-- validate-1 (EXECUTOR, created by planner)
|   |-- impl-2 (EXECUTOR, created by planner)
|   |-- validate-2 (EXECUTOR, created by planner)
|   +-- ...
+-- completion-gate (JUDGE, from_template, activation: manual_trigger)
```

The `implementation-loop` arc is **mutable** (`template_mutable=true`), meaning the PLANNER can add iteration children directly to it despite it being a template arc.

## Step-by-Step Flow

### 1. Spec Refinement (CHAT)

The CHAT agent talks to the user to refine requirements into a `DevelopmentSpec`:

```python
from data_models.dark_factory import DevelopmentSpec
state.set_typed("development_spec", spec)
```

### 2. Scenario Generation (EXECUTOR)

Reads the spec (via parent reading child state), generates test scenarios split into visible + holdout sets:

```python
from data_models.dark_factory import TestSuite, TestScenario
state.set_typed("test_suite", suite)
```

### 3. Implementation Loop (PLANNER, mutable)

The PLANNER reads results from previous steps and creates impl/validate pairs as children:

- **Read child state**: `state.get("development_spec", arc_id=spec_arc_id)`
- **Create children**: `arc.add_child(loop_arc_id, "impl-1", agent_type="EXECUTOR")`
- **Read validation results**: `state.get("validation_result", arc_id=validate_arc_id)`
- **Decide continue/done**: Compare `pass_rate` against threshold

### 4. Completion Gate (JUDGE)

Activated manually after the PLANNER signals completion. Runs holdout scenarios for final validation.

## Data Flow Pattern

Parent reads child state, passes context to next child via goal or state:

1. Root PLANNER reads `development_spec` from spec-refinement arc
2. Root PLANNER activates scenario-generation, includes spec in goal
3. Root PLANNER reads `test_suite` from scenario-generation arc
4. Root PLANNER activates implementation-loop, passes spec + test data
5. Implementation-loop PLANNER creates iteration children, reads results
6. Root PLANNER signals completion-gate when loop is done

## Key Data Models

All defined in `config_seed/data_models/dark_factory.py`:

- `DevelopmentSpec` — Requirements, acceptance criteria, constraints
- `TestSuite` — Visible scenarios + holdout scenarios
- `TestScenario` — Single test case (name, input, expected_output)
- `ValidationResult` — Pass/fail lists, pass_rate, execution traces
- `IterationFeedback` — Failed scenarios + suggestions for next attempt
- `MonitorConfig` — Threshold and iteration limit configuration
- `DarkFactoryResult` — Final outcome summary

## Stopping Conditions

The PLANNER should stop iterating when:

1. **Pass rate threshold met** (default 0.95)
2. **Max iterations reached** (default 10)
3. **Diminishing returns** — pass rate delta below threshold for N consecutive iterations

## Web Research Pattern

If the PLANNER needs external information, it creates a tainted arc tree:

```python
arc.create_batch(arcs=[
    {"name": "fetch-docs", "taint_level": "tainted", "agent_type": "EXECUTOR", ...},
    {"name": "review-docs", "agent_type": "REVIEWER", "reviewer_profile": "default", ...},
    {"name": "judge-docs", "agent_type": "JUDGE", "reviewer_profile": "default", ...},
])
```

The JUDGE promotes clean results that the PLANNER can safely read.

## Related

[[skills/dark-factory/examples]] · [[skills/dark-factory/patterns]]
