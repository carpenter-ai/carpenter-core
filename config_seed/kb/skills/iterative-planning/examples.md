# Iterative Planning Examples

## Example 1: Spec-driven development

**Scenario**: Generate code from a specification, run tests, iterate until the pass rate exceeds 90% or 10 iterations are reached.

### Planner setup (creates first pair)

```python
from carpenter_tools.act.arc import add_child, set_state

# Store configuration in parent state
parent_id = current_arc_id
set_state("spec", spec_text)
set_state("target_pass_rate", 0.9)
set_state("max_iterations", 10)
set_state("iteration", 1)

# Create first implementation + monitor pair
impl_1 = add_child(
    parent_id=parent_id,
    name="impl-1",
    goal="Generate code from spec (iteration 1)",
)
monitor_1 = add_child(
    parent_id=parent_id,
    name="monitor-1",
    goal="Run tests and evaluate pass rate (iteration 1)",
)
```

### Implementation arc code

```python
from carpenter_tools.read.state import get_state
from carpenter_tools.act.state import set_state

spec = get_state("spec")
iteration = get_state("iteration")

# If there is feedback from a previous monitor, use it
feedback = get_state("feedback") if iteration > 1 else None
previous_code = get_state("generated_code") if iteration > 1 else None

# Generate code (in practice, this invokes an agent or runs a code generation step)
generated_code = generate_from_spec(spec, feedback=feedback, previous=previous_code)

set_state("generated_code", generated_code)
```

### Monitor arc code

```python
from carpenter_tools.read.state import get_state
from carpenter_tools.read.arc import get_arc_detail
from carpenter_tools.act.state import set_state
from carpenter_tools.act.arc import add_child

generated_code = get_state("generated_code")
target = get_state("target_pass_rate")
max_iter = get_state("max_iterations")
iteration = get_state("iteration")

# Run tests
pass_rate = run_tests(generated_code)
set_state("last_pass_rate", pass_rate)

# Check resource limits via platform counters
parent_id = get_state("_parent_arc_id")
parent_info = get_arc_detail(parent_id)

# Decision: stop or continue?
if pass_rate >= target:
    set_state("result", "success")
    set_state("final_pass_rate", pass_rate)
    # Done. Do not create more arcs.
elif iteration >= max_iter:
    set_state("result", "max_iterations_reached")
    set_state("final_pass_rate", pass_rate)
elif parent_info.get("descendant_tokens", 0) > 100000:
    set_state("result", "token_budget_exceeded")
    set_state("final_pass_rate", pass_rate)
else:
    # Continue: generate feedback and create next pair
    feedback = analyze_failures(generated_code, pass_rate)
    set_state("feedback", feedback)

    next_iter = iteration + 1
    set_state("iteration", next_iter)

    add_child(
        parent_id=parent_id,
        name=f"impl-{next_iter}",
        goal=f"Revise code based on feedback (iteration {next_iter})",
    )
    add_child(
        parent_id=parent_id,
        name=f"monitor-{next_iter}",
        goal=f"Run tests and evaluate pass rate (iteration {next_iter})",
    )
```

---

## Example 2: Polling/waiting for external condition

**Scenario**: Check whether a deployment has completed by polling an external system. Continue until the deployment reports success or 30 minutes elapse.

### Planner setup

```python
from carpenter_tools.act.arc import add_child, set_state
import time

parent_id = current_arc_id
set_state("deployment_id", deployment_id)
set_state("poll_start_time", time.time())
set_state("poll_timeout_seconds", 1800)  # 30 minutes
set_state("poll_count", 0)
set_state("max_polls", 60)

add_child(parent_id=parent_id, name="poll-1", goal="Check deployment status")
add_child(parent_id=parent_id, name="poll-monitor-1", goal="Evaluate deployment status")
```

### Poll implementation code

```python
from carpenter_tools.read.state import get_state
from carpenter_tools.act.state import set_state
import requests

deployment_id = get_state("deployment_id")
response = requests.get(f"https://api.example.com/deployments/{deployment_id}")
status = response.json()

set_state("last_poll_status", status["state"])
set_state("last_poll_details", status)
```

### Poll monitor code

```python
from carpenter_tools.read.state import get_state
from carpenter_tools.act.arc import add_child
from carpenter_tools.act.state import set_state
import time

status = get_state("last_poll_status")
start_time = get_state("poll_start_time")
timeout = get_state("poll_timeout_seconds")
poll_count = get_state("poll_count") + 1
max_polls = get_state("max_polls")

set_state("poll_count", poll_count)

elapsed = time.time() - start_time

if status == "success":
    set_state("result", "deployment_complete")
elif status == "failed":
    set_state("result", "deployment_failed")
elif elapsed > timeout:
    set_state("result", "timeout")
elif poll_count >= max_polls:
    set_state("result", "max_polls_reached")
else:
    # Continue polling
    parent_id = get_state("_parent_arc_id")
    n = poll_count + 1
    add_child(parent_id=parent_id, name=f"poll-{n}", goal="Check deployment status")
    add_child(parent_id=parent_id, name=f"poll-monitor-{n}", goal="Evaluate deployment status")
```

---

## Example 3: Iterative refinement with quality feedback

**Scenario**: Improve a document through iterative editing. An agent makes improvements, a monitor evaluates quality. Feedback from each monitor feeds into the next implementation's state.

### Planner setup

```python
from carpenter_tools.act.arc import add_child, set_state

parent_id = current_arc_id
set_state("document", initial_document)
set_state("quality_criteria", criteria)
set_state("iteration", 1)
set_state("max_iterations", 5)
set_state("quality_scores", [])

add_child(parent_id=parent_id, name="edit-1", goal="Improve document (iteration 1)")
add_child(parent_id=parent_id, name="evaluate-1", goal="Evaluate document quality")
```

### Edit implementation (agent arc)

```python
from carpenter_tools.read.state import get_state
from carpenter_tools.act.state import set_state

document = get_state("document")
criteria = get_state("quality_criteria")
feedback = get_state("feedback")  # None on first iteration

# Agent-driven editing: apply feedback to improve the document
improved = apply_improvements(document, criteria, feedback)
set_state("document", improved)
```

### Quality monitor

```python
from carpenter_tools.read.state import get_state
from carpenter_tools.read.arc import get_arc_detail
from carpenter_tools.act.state import set_state
from carpenter_tools.act.arc import add_child

document = get_state("document")
criteria = get_state("quality_criteria")
iteration = get_state("iteration")
max_iter = get_state("max_iterations")
scores = get_state("quality_scores")

# Evaluate quality
score = evaluate_quality(document, criteria)
scores.append(score)
set_state("quality_scores", scores)

# Check for diminishing returns (last 2 iterations no improvement)
diminishing = (
    len(scores) >= 3
    and scores[-1] <= scores[-2]
    and scores[-2] <= scores[-3]
)

# Check platform counters
parent_id = get_state("_parent_arc_id")
parent_info = get_arc_detail(parent_id)
token_budget = 50000

if score >= 0.9:
    set_state("result", "quality_target_met")
elif iteration >= max_iter:
    set_state("result", "max_iterations_reached")
elif diminishing:
    set_state("result", "diminishing_returns")
elif parent_info.get("descendant_tokens", 0) > token_budget:
    set_state("result", "token_budget_exceeded")
else:
    # Continue: generate targeted feedback
    feedback = generate_feedback(document, criteria, score)
    set_state("feedback", feedback)

    next_iter = iteration + 1
    set_state("iteration", next_iter)

    add_child(
        parent_id=parent_id,
        name=f"edit-{next_iter}",
        goal=f"Improve document based on feedback (iteration {next_iter})",
    )
    add_child(
        parent_id=parent_id,
        name=f"evaluate-{next_iter}",
        goal=f"Evaluate document quality (iteration {next_iter})",
    )
```
