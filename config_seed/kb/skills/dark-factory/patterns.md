# Dark Factory Arc Patterns

## Iteration Pair Pattern

The PLANNER creates impl + validate pairs under the mutable loop arc:

```python
# Create implementation arc
impl_id = arc.add_child(
    parent_id=loop_arc_id,
    name=f"impl-{iteration}",
    goal=f"Implement based on feedback: {feedback_summary}",
    agent_type="EXECUTOR",
)

# Create validation arc (depends on impl completing)
validate_id = arc.add_child(
    parent_id=loop_arc_id,
    name=f"validate-{iteration}",
    goal="Run test scenarios against latest implementation",
    agent_type="EXECUTOR",
)
```

## Reading Validation Results

After validation completes, the PLANNER reads results from the child:

```python
result = state.get("validation_result", arc_id=validate_arc_id)
vr = ValidationResult(**result)

if vr.pass_rate >= threshold:
    # Done — signal completion
    state.set("decision", "done")
else:
    # Generate feedback for next iteration
    feedback = IterationFeedback(
        failed_scenarios=vr.failed,
        error_details=[...],
        iteration=iteration,
    )
    state.set("iteration_feedback", feedback.model_dump())
```

## Tainted Research Arc Tree

When the PLANNER needs web research:

```python
result = arc.create_batch(arcs=[
    {
        "name": "fetch-api-docs",
        "goal": "Fetch API documentation from https://example.com/docs",
        "parent_id": loop_arc_id,
        "taint_level": "tainted",
        "agent_type": "EXECUTOR",
        "step_order": 0,
    },
    {
        "name": "review-api-docs",
        "goal": "Review fetched documentation for safety",
        "parent_id": loop_arc_id,
        "agent_type": "REVIEWER",
        "reviewer_profile": "default",
        "step_order": 1,
    },
    {
        "name": "judge-api-docs",
        "goal": "Approve reviewed documentation",
        "parent_id": loop_arc_id,
        "agent_type": "JUDGE",
        "reviewer_profile": "default",
        "step_order": 2,
    },
])
```

After the JUDGE approves, the promoted result is clean and readable by the PLANNER.

## Parent State Read Pattern

The root PLANNER orchestrates by reading from completed children:

```python
# Read spec from spec-refinement child
spec_data = state.get("development_spec", arc_id=spec_arc_id)
spec = DevelopmentSpec(**spec_data)

# Read test suite from scenario-generation child
suite_data = state.get("test_suite", arc_id=gen_arc_id)
suite = TestSuite(**suite_data)

# Pass data forward by including in next child's goal
arc.update_status(gen_arc_id, "active")
```

## Diminishing Returns Detection

Track pass rates across iterations:

```python
pass_rates = []
for i in range(iteration_count):
    vr_data = state.get("validation_result", arc_id=validate_ids[i])
    vr = ValidationResult(**vr_data)
    pass_rates.append(vr.pass_rate)

# Check last N iterations for improvement
window = config.diminishing_returns_window
if len(pass_rates) >= window:
    recent = pass_rates[-window:]
    delta = max(recent) - min(recent)
    if delta < config.diminishing_returns_min_delta:
        # Diminishing returns — stop iterating
        decision = "done"
```
