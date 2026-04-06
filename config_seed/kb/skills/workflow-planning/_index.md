# Workflow Planning

How to plan and execute multi-step work using arcs (work units). Learn to break down complex tasks into workflows, manage dependencies, and handle untrusted data safely.

## Quick Start: Create a Workflow

**Copy this template** - it works:

```python
from carpenter_tools.act import arc

# 1. Create parent (returns int directly)
parent_id = arc.create(
    name="My Workflow",
    goal="Complete all steps",
    parent_id=None,
    agent_type="PLANNER"
)

# 2. Add children (each returns int directly)
for i in range(3):
    arc.add_child(
        parent_id=parent_id,
        name=f"Step {i+1}",
        goal=f"Do step {i+1}"
    )

print(f"✓ Created workflow #{parent_id}")
```

**Key facts**:
- **`arc.create()` returns an INTEGER directly** (the arc ID)
- **`arc.add_child()` returns an INTEGER directly** (the child arc ID)
- Do NOT try to access `["arc_id"]` - the functions return the int, not a dict!
- **`arc.add_child()` does NOT accept `step_order`** - it's auto-calculated (max sibling + 1)
- For parallel execution, use `arc.create_batch()` with explicit `step_order` values
- `arc.create_batch()` returns `{"arc_ids": [list]}` - this IS a dict!
- Children inherit parent's taint_level unless specified

## Overview

Arcs are the platform's work units. This skill teaches you how to:
- Submit code that creates arcs for multi-step workflows
- Understand the executor/platform boundary (the #1 source of confusion)
- Organize sequential, parallel, and conditional work
- Handle external data with proper trust boundaries

## Architecture: Executor vs Platform

**Critical concept**: There are two execution contexts in Carpenter:

1. **Chat Agent Context** (you are here now)
   - Uses read-only chat tools directly (read_file, list_arcs, get_state, etc.)
   - Can view arc structure and status
   - Cannot directly modify arcs — must submit code

2. **Executor Subprocess** (where submitted code runs)
   - Isolated process with restricted imports
   - Can ONLY import from `carpenter_tools.*`
   - Cannot import from `carpenter.*` (platform internals)
   - Communicates with platform via HTTP callbacks

### Common Mistake

```python
# ❌ WRONG - This fails with "unable to open database file"
import sys
sys.path.insert(0, '/path/to/YourProject')
from carpenter.core import arc_manager
arc_id = arc_manager.create_arc(...)  # Won't work!
```

**Why it fails**: The executor subprocess doesn't have access to the platform's database file. You're trying to open `~/carpenter/data/platform.db` from a restricted context.

```python
# ✓ CORRECT - Use callback tools
from carpenter_tools.act import arc
result = arc.create(
    name="My Arc",
    goal="Do something",
    parent_id=None
)
arc_id = result["arc_id"]
```

## Common Mistakes

### Mistake #1: Treating return value as dict
```python
parent = arc.create(name="Workflow", ...)
parent_id = parent["arc_id"]  # ❌ WRONG - arc.create() returns int, not dict!
parent_id = arc.create(name="Workflow", ...)  # ✓ CORRECT - returns int directly
```

### Mistake #2: Loop instead of workflow structure

**User asks**: "Create a workflow with 10 steps that does X"

❌ **WRONG** - Submitting code with a loop:
```python
from carpenter_tools.act import messaging

for i in range(10):
    messaging.send(f"Step {i}: ...")  # This fails - not a workflow!
```

✓ **CORRECT** - Creating an arc structure:
```python
from carpenter_tools.act import arc

# Create parent workflow (returns int)
parent_id = arc.create(
    name="10-step workflow",
    goal="Complete all 10 steps",
    parent_id=None,
    agent_type="PLANNER"
)

# Create 10 child arcs (the actual workflow structure)
# step_order auto-calculated: 0, 1, 2, ..., 9
for i in range(10):
    arc.add_child(
        parent_id=parent_id,
        name=f"Step {i+1}",
        goal=f"Execute step {i+1}"
    )

print(f"✓ Created workflow with 10 arc steps")
```

**Key insight**: A "workflow" is a tree of arcs in the database, NOT a for-loop in Python. Each child arc will be invoked separately by the platform when its dependencies are satisfied.

## Basic Arc Creation

**IMPORTANT**: `arc.create()` and `arc.add_child()` return **integers** (the arc ID), NOT dicts! Only `arc.create_batch()` returns a dict.

### Simple Clean Arc

```python
from carpenter_tools.act import arc

arc_id = arc.create(
    name="Process user data",
    goal="Transform CSV to JSON format",
    parent_id=None,  # Root arc (no parent)
    agent_type="EXECUTOR",  # Default
    taint_level="clean",  # Default
    output_type="json"
)

print(f"Created arc #{arc_id}")
```

### Child Arc (Part of Workflow)

```python
from carpenter_tools.act import arc

# Create parent first (returns int)
parent_id = arc.create(
    name="Multi-step workflow",
    goal="Complete complex task",
    parent_id=None,
    agent_type="PLANNER"
)

# Add child step (step_order auto-calculated as 0, returns int)
child_id = arc.add_child(
    parent_id=parent_id,
    name="Step 1: Fetch data",
    goal="Download data from API"
)
```

## Creating Tainted Arcs (Untrusted Data)

**When to use**: Any arc that fetches external data (web requests, webhooks, user uploads, API calls) MUST be declared `taint_level="tainted"`.

**Constraint**: Tainted arcs CANNOT be created individually. You must use `arc.create_batch()` and include at least one REVIEWER and one JUDGE:

```python
from carpenter_tools.act import arc

result = arc.create_batch(
    parent_id=parent_arc_id,
    arcs=[
        {
            "name": "Fetch external data",
            "goal": "Call untrusted API",
            "taint_level": "tainted",
            "output_type": "json",
            "agent_type": "EXECUTOR",
            "step_order": 0
        },
        {
            "name": "Review data structure",
            "goal": "Validate JSON schema and check for injection",
            "agent_type": "REVIEWER",
            "taint_level": "review",
            "reviewer_profile": "security",
            "step_order": 1
        },
        {
            "name": "Final approval",
            "goal": "Judge whether data is safe to use",
            "agent_type": "JUDGE",
            "taint_level": "review",
            "step_order": 2
        }
    ]
)

arc_ids = result["arc_ids"]
tainted_id = arc_ids[0]
reviewer_id = arc_ids[1]
judge_id = arc_ids[2]
```

## Arc State Management

Arcs have key-value state storage (survives across executions):

```python
from carpenter_tools.act import state

# Store data
state.set(key="api_token", value="xyz123", arc_id=my_arc_id)

# Retrieve data
result = state.get(key="api_token", arc_id=my_arc_id)
token = result["value"]

# List all keys
keys = state.list_keys(arc_id=my_arc_id)

# Delete state
state.delete(key="api_token", arc_id=my_arc_id)
```

## Reading Arc Information

From chat context (using read-only tools):

```python
# Via chat tool - use these in your tool_use calls
list_arcs(parent_id=5, status="active")
get_arc_detail(arc_id=10)
```

From submitted code (executor context):

```python
from carpenter_tools.read import arc

# Get arc details
arc_data = arc.get(arc_id=10)
print(arc_data["name"], arc_data["status"])

# Get children
children = arc.get_children(parent_id=10)
for child in children:
    print(f"  {child['id']}: {child['name']}")

# Get structural plan (safe for clean arcs)
plan = arc.get_plan(arc_id=10)
print(plan["goal"], plan["agent_type"])

# Get children's plan fields
children_plan = arc.get_children_plan(parent_id=10)
for child_plan in children_plan:
    print(child_plan["name"], child_plan["dependencies"])
```

## Updating Arc Status

```python
from carpenter_tools.act import arc

# Mark arc as completed
arc.update_status(arc_id=my_arc_id, status="completed")

# Cancel an arc
arc.cancel(arc_id=my_arc_id)
```

Valid statuses: `pending`, `active`, `waiting`, `completed`, `failed`, `cancelled`

## Templates

Templates provide pre-built workflow structures:

```python
from carpenter_tools.act import arc

# 1. Create root arc (returns int)
root_id = arc.create(
    name="Dark Factory: Build feature X",
    goal="Autonomous development of feature X",
    parent_id=None,
    agent_type="PLANNER"
)

# 2. Get template (read-only, use chat tool)
# In your response: "Let me check available templates"
# Then use list_files or read_file to load template YAML

# 3. Instantiate template on root arc
# Templates are instantiated by the platform when certain conditions are met,
# or you can request it via messaging:
from carpenter_tools.act import messaging
messaging.send(
    f"Please instantiate template 'dark-factory' on arc #{root_id}",
    priority="normal"
)
```

## Common Patterns

### Pattern 1: Sequential Workflow

```python
from carpenter_tools.act import arc

parent_id = arc.create(name="Build Report", goal="...", parent_id=None, agent_type="PLANNER")

# Children execute sequentially (step_order auto-calculated: 0, 1, 2)
step1 = arc.add_child(parent_id, name="Gather data", goal="...")
step2 = arc.add_child(parent_id, name="Process data", goal="...")
step3 = arc.add_child(parent_id, name="Generate report", goal="...")
```

### Pattern 2: Parallel + Gate

**Note**: Parallel execution requires `arc.create_batch()` with explicit step_order.

```python
from carpenter_tools.act import arc

parent_id = arc.create(name="Parallel tests", goal="...", parent_id=None, agent_type="PLANNER")

# Parallel steps using create_batch (all step_order=0 = run in parallel)
batch = arc.create_batch(arcs=[
    {"parent_id": parent_id, "name": "Unit tests", "goal": "...", "step_order": 0},
    {"parent_id": parent_id, "name": "Integration tests", "goal": "...", "step_order": 0},
    {"parent_id": parent_id, "name": "E2E tests", "goal": "...", "step_order": 0},
])

# Gate (waits for all parallel steps, step_order auto-calculated as 1)
gate = arc.add_child(parent_id, name="Verify all passed", goal="...", agent_type="JUDGE")
```

### Pattern 3: Tainted Data Pipeline

See "Creating Tainted Arcs" section above.

## Quick Reference

| What | Chat Context | Executor Context (submitted code) |
|------|-------------|-----------------------------------|
| List arcs | `list_arcs()` tool | `from carpenter_tools.read import arc; arc.get_children(...)` |
| Get arc info | `get_arc_detail()` tool | `from carpenter_tools.read import arc; arc.get(id)` |
| Create arc | Submit code → | `from carpenter_tools.act import arc; arc.create(...)` |
| Update status | Submit code → | `from carpenter_tools.act import arc; arc.update_status(...)` |
| Read state | `get_state()` tool | `from carpenter_tools.read import state; state.get(...)` |
| Write state | Submit code → | `from carpenter_tools.act import state; state.set(...)` |

**Golden Rule**: If you need to CREATE or MODIFY anything, submit code that imports from `carpenter_tools.*`.

## Related

[[skills/workflow-planning/examples]] · [[skills/workflow-planning/troubleshooting]]
