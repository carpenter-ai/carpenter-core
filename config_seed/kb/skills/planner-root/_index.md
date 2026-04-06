# Planner Root

When to use a PLANNER root arc for multi-step work, and why it matters for autonomous escalation.

## The Pattern

For any non-trivial multi-step work, create a PLANNER root arc as the parent:

```python
from carpenter_tools.act import arc

# PLANNER root holds the goal and serves as escalation target
root_id = arc.create(
    name="Migrate database schema",
    goal="Update all tables to v3 schema, verify data integrity, update app queries",
    agent_type="PLANNER"
)

# Children do the actual work
arc.add_child(root_id, name="Backup current schema", goal="...")
arc.add_child(root_id, name="Run migration scripts", goal="...")
arc.add_child(root_id, name="Verify data integrity", goal="...", agent_type="REVIEWER")
```

## Why This Matters

The platform has built-in escalation: when a child arc fails, the parent is re-invoked to decide what to do (retry, restructure, or ask the user). But this only works well when the parent has the right context and capabilities.

A PLANNER arc:
- Runs in a clean context (not tainted by child execution)
- Can create and cancel child arcs
- Gets re-invoked with failure details when a child is stuck
- Can reason about the overall goal and restructure the plan

Without a PLANNER root, failed children escalate to... nothing. The failure hits the top of the tree and the user gets a notification, but nobody tried to recover.

## When NOT to Use a Planner Root

Simple, self-contained work does not need coordination overhead:

- A single Python script triggered by a webhook
- A one-off search or message send
- Any task that is a single arc with no children

If the work has no children, there is nothing to coordinate. A planner root would be an empty wrapper.

**Rule of thumb:** If you are calling `arc.add_child()`, the parent should probably be a PLANNER. If the parent is doing work itself (EXECUTOR), consider whether the work should be restructured so the parent plans and children execute.

## How Escalation Works

```
Child arc fails (exhausts retries)
    |
    v
Platform fires arc.child_failed work item
    |
    v
Parent PLANNER is re-invoked with:
    - Its own goal (the project objective)
    - The failed child's goal and failure details (sanitized)
    - Sibling arcs' statuses (what else is in progress)
    |
    v
PLANNER decides:
    A) Create a new child with an amended approach
    B) Cancel remaining children and restructure the plan
    C) Mark itself as failed (escalates to its parent, if any)
    D) Ask the user via messaging.ask()
```

The escalation policy is configurable per-arc via the `_escalation_policy` state key:

| Policy | Behavior |
|--------|----------|
| `replan` (default) | Re-invoke parent to create alternative children |
| `fail` | No re-invocation, parent not notified |
| `human` | Send notification to human |
| `escalate` | Try a stronger model on the failed child |

## Common Patterns

### Project with verification

```python
from carpenter_tools.act import arc

root_id = arc.create(
    name="Add search feature",
    goal="Implement full-text search with tests and documentation",
    agent_type="PLANNER"
)

arc.add_child(root_id, name="Implement search", goal="Add FTS5 index and query endpoint")
arc.add_child(root_id, name="Write tests", goal="Unit and integration tests for search")
arc.add_child(root_id, name="Verify", goal="Run test suite, check coverage", agent_type="REVIEWER")
```

### Nested coordination

For large projects, sub-coordinators manage their own subtrees:

```python
from carpenter_tools.act import arc

root_id = arc.create(
    name="Annual report",
    goal="Compile Q1-Q4 data into final report",
    agent_type="PLANNER"
)

# Each sub-coordinator manages its own children
q1 = arc.add_child(root_id, name="Q1 analysis", goal="...", agent_type="PLANNER")
arc.add_child(q1, name="Gather Q1 data", goal="...")
arc.add_child(q1, name="Analyze Q1 trends", goal="...")

q2 = arc.add_child(root_id, name="Q2 analysis", goal="...", agent_type="PLANNER")
arc.add_child(q2, name="Gather Q2 data", goal="...")
arc.add_child(q2, name="Analyze Q2 trends", goal="...")
```

Each PLANNER sees only its own children's details. The root PLANNER sees sub-coordinator statuses but not their internal state. This is progressive disclosure — each level provides a compressed view of its subtree.
