# Arc Operations: Complete Examples

**CRITICAL**: `arc.create()` and `arc.add_child()` return **integers directly** (the arc ID), NOT dicts!

**IMPORTANT**: `arc.add_child()` does NOT accept a `step_order` parameter. Step order is auto-calculated (max sibling order + 1) when you add children. Children added in sequence will execute sequentially. For parallel execution or custom ordering, use `arc.create_batch()` with explicit `step_order` values.

**NOTE**: Only `arc.create_batch()` returns a dict: `{"arc_ids": [list of arc IDs]}`

## Example 1: Simple Task Arc

**Goal**: Create a standalone arc to process a file.

**Code to submit**:
```python
from carpenter_tools.act import arc, state

# Create arc (returns int directly)
arc_id = arc.create(
    name="Process data.csv",
    goal="Convert CSV to JSON",
    parent_id=None,
    output_type="json"
)

# Store the arc_id for later reference
state.set(key="current_task_arc", value=arc_id, arc_id=0)  # arc_id=0 = conversation-level state

print(f"✓ Created arc #{arc_id}")
print(f"Next step: The platform will invoke this arc and it will process the file")
```

## Example 2: Multi-Step Sequential Workflow

**Goal**: Three-step workflow that runs in sequence.

**Code to submit**:
```python
from carpenter_tools.act import arc

# Create parent planner arc (returns int)
parent_id = arc.create(
    name="Generate Monthly Report",
    goal="Compile and publish monthly metrics report",
    parent_id=None,
    agent_type="PLANNER"
)

# Step 1: Gather data (returns int, step_order auto-calculated as 0)
step1_id = arc.add_child(
    parent_id=parent_id,
    name="Gather metrics",
    goal="Query database for last month's metrics",
    agent_type="EXECUTOR",
    output_type="json"
)

# Step 2: Generate charts (returns int, step_order auto-calculated as 1)
step2_id = arc.add_child(
    parent_id=parent_id,
    name="Generate visualizations",
    goal="Create charts and graphs from metrics",
    agent_type="EXECUTOR",
    output_type="python"
)

# Step 3: Publish report (returns int, step_order auto-calculated as 2)
step3_id = arc.add_child(
    parent_id=parent_id,
    name="Publish report",
    goal="Send report to stakeholders",
    agent_type="EXECUTOR",
    output_type="text"
)

print(f"✓ Created workflow with 3 steps")
print(f"  Parent arc: #{parent_id}")
print(f"  Step 1: #{step1_id} (gather)")
print(f"  Step 2: #{step2_id} (visualize)")
print(f"  Step 3: #{step3_id} (publish)")
```

## Example 3: Parallel Tasks with Gate

**Goal**: Run multiple tests in parallel, then verify all passed.

**Important**: Parallel execution requires `arc.create_batch()` with explicit `step_order` values. Using `arc.add_child()` sequentially will result in sequential execution.

**Code to submit**:
```python
from carpenter_tools.act import arc

# Create parent (returns int)
parent_id = arc.create(
    name="Run Test Suite",
    goal="Execute all tests and verify results",
    parent_id=None,
    agent_type="PLANNER"
)

# Create parallel test arcs using create_batch (all with step_order=0)
batch_result = arc.create_batch(
    arcs=[
        {
            "parent_id": parent_id,
            "name": "Unit tests",
            "goal": "Run pytest unit tests",
            "step_order": 0,
            "output_type": "python",
        },
        {
            "parent_id": parent_id,
            "name": "Integration tests",
            "goal": "Run integration test suite",
            "step_order": 0,
            "output_type": "python",
        },
        {
            "parent_id": parent_id,
            "name": "E2E tests",
            "goal": "Run end-to-end tests",
            "step_order": 0,
            "output_type": "python",
        },
    ]
)

test_arc_ids = batch_result["arc_ids"]

# Gate: runs after ALL step_order=0 arcs complete (step_order auto-calculated as 1)
gate = arc.add_child(
    parent_id=pid,
    name="Verify all tests passed",
    goal="Check that all test arcs completed successfully",
    agent_type="JUDGE",
    output_type="text"
)

print(f"✓ Created parallel test workflow")
print(f"  Tests (parallel): {test_arc_ids}")
print(f"  Gate: #{gate['arc_id']}")
```

## Example 4: Tainted Data Pipeline

**Goal**: Fetch external data, review it for safety, then process.

**Code to submit**:
```python
from carpenter_tools.act import arc

# Create parent planner
parent = arc.create(
    name="External Data Pipeline",
    goal="Safely fetch and process untrusted API data",
    parent_id=None,
    agent_type="PLANNER"
)
parent_id = parent  # parent is already an int

# Create tainted + review arcs together (MUST use create_batch)
batch = arc.create_batch(
    parent_id=pid,
    arcs=[
        # Step 0: Fetch untrusted data
        {
            "name": "Fetch user data from API",
            "goal": "Call external API and store response",
            "taint_level": "tainted",
            "agent_type": "EXECUTOR",
            "output_type": "json",
            "step_order": 0
        },
        # Step 1: Review the data
        {
            "name": "Review API response",
            "goal": "Check for injection attacks, validate schema",
            "agent_type": "REVIEWER",
            "taint_level": "review",
            "reviewer_profile": "security",
            "output_type": "text",
            "step_order": 1
        },
        # Step 2: Judge approval
        {
            "name": "Approve for processing",
            "goal": "Final decision on whether data is safe",
            "agent_type": "JUDGE",
            "taint_level": "review",
            "output_type": "text",
            "step_order": 2
        }
    ]
)

tainted_id, reviewer_id, judge_id = batch["arc_ids"]

# Step 3: Clean processing arc (waits for judge approval, step_order auto-calculated as 3)
processing = arc.add_child(
    parent_id=pid,
    name="Process validated data",
    goal="Transform and store the approved data",
    taint_level="clean",
    output_type="python"
)

print(f"✓ Created tainted data pipeline")
print(f"  Fetch (tainted): #{tainted_id}")
print(f"  Review: #{reviewer_id}")
print(f"  Judge: #{judge_id}")
print(f"  Process (clean): #{processing['arc_id']}")
```

## Example 5: Using Arc State

**Goal**: Pass data between workflow steps using state.

**Code to submit**:
```python
from carpenter_tools.act import arc, state

# Create a workflow
parent = arc.create(
    name="Data Pipeline with State",
    goal="Process data across multiple steps",
    parent_id=None,
    agent_type="PLANNER"
)
parent_id = parent  # parent is already an int

step1 = arc.add_child(
    parent_id=pid,
    name="Download data",
    goal="Fetch data from source"
)

step2 = arc.add_child(
    parent_id=pid,
    name="Transform data",
    goal="Read downloaded data from step1's state and transform"
)

# Store workflow configuration in parent's state
state.set(
    arc_id=pid,
    key="config",
    value={
        "source_url": "https://api.example.com/data",
        "format": "json",
        "output_dir": "/data/processed"
    }
)

# Store step IDs for later reference
state.set(
    arc_id=pid,
    key="step_ids",
    value={
        "download": step1_id,
        "transform": step2_id
    }
)

print(f"✓ Workflow created with state")
print(f"  Parent arc #{pid} has config stored")
print(f"  Step 1 will store its output in its own state")
print(f"  Step 2 will read from step 1's state")
```

**Step 1's executor code** (when invoked by platform):
```python
from carpenter_tools.act import state
from carpenter_tools.read import arc as arc_read
import requests

# Get my arc_id from environment (platform sets this)
import os
my_arc_id = int(os.environ.get("CARPENTER_ARC_ID", "0"))

# Get parent's config
parent_data = arc_read.get(my_arc_id)
parent_id = parent_data["parent_id"]

config = state.get(key="config", arc_id=parent_id)["value"]

# Fetch data
response = requests.get(config["source_url"])
data = response.json()

# Store in MY state for next step
state.set(
    arc_id=my_arc_id,
    key="downloaded_data",
    value=data
)

print(f"✓ Downloaded {len(data)} records")
```

**Step 2's executor code**:
```python
from carpenter_tools.act import state
from carpenter_tools.read import arc as arc_read

my_arc_id = int(os.environ.get("CARPENTER_ARC_ID", "0"))

# Get parent's step_ids to find step 1
parent_data = arc_read.get(my_arc_id)
parent_id = parent_data["parent_id"]

step_ids = state.get(key="step_ids", arc_id=parent_id)["value"]
step1_id = step_ids["download"]

# Read data from step 1
downloaded_data = state.get(key="downloaded_data", arc_id=step1_id)["value"]

# Transform it
transformed = [record.upper() for record in downloaded_data]

# Store result
state.set(
    arc_id=my_arc_id,
    key="transformed_data",
    value=transformed
)

print(f"✓ Transformed {len(transformed)} records")
```

## Example 6: Template Instantiation

**Goal**: Use a pre-built workflow template.

**From chat context** (don't submit this as code):
```
User: "I need to set up a dark factory workflow for building a new feature"

You (agent): "I'll create a root arc and request template instantiation"
```

**Code to submit**:
```python
from carpenter_tools.act import arc, messaging

# Create root arc for the workflow
root = arc.create(
    name="Dark Factory: Build Authentication System",
    goal="Autonomous development of JWT authentication with tests",
    parent_id=None,
    agent_type="PLANNER"
)

root_id = root  # root is already an int

# Request template instantiation via messaging
# The platform will see this notification and instantiate the template
messaging.send(
    message=f"Template instantiation request: 'dark-factory' on arc #{root_id}",
    priority="normal",
    category="workflow_setup"
)

print(f"✓ Created root arc #{root_id}")
print(f"✓ Requested dark-factory template instantiation")
print(f"The platform will create 4 child arcs:")
print(f"  1. spec-refinement (CHAT)")
print(f"  2. scenario-generation (EXECUTOR)")
print(f"  3. implementation-loop (PLANNER, mutable)")
print(f"  4. completion-gate (JUDGE)")
```

**Note**: Template instantiation is typically handled by the platform automatically when certain conditions are met. The messaging approach is a fallback for manual requests.
