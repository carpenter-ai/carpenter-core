# Workflow Planning

When users ask for a 'workflow' or 'multi-step process', they mean a structure of parent and child arcs.

## Basic pattern
```python
from carpenter_tools.act import arc
parent_id = arc.create(name="My Workflow", agent_type="PLANNER")
arc.add_child(parent_id, name="Step 1", goal="Do X using messaging.send()")
arc.add_child(parent_id, name="Step 2", goal="Do Y using messaging.send()")
```

## Key rules
- Workflow = arc structure, NOT a loop in submitted code
- Each child arc gets its own execution context when the platform invokes it
- Create ALL arcs (parent + all children) in a SINGLE submit_code call
- For parallel execution, use `arc.create_batch()` with explicit step_order values
- `arc.create()` and `arc.add_child()` return **integers**, NOT dicts
- `arc.create_batch()` returns `{"arc_ids": [list]}` — this IS a dict
- `arc.add_child()` does NOT accept `step_order` — it's auto-calculated
- Arc executor code goes through CaMeL verification — ALL string literals must be wrapped in SecurityType constructors (Label, UnstructuredText, etc.)

**WRONG patterns:**
- `parent = arc.create(...); parent_id = parent["arc_id"]` — TypeError, returns int
- `arc.add_child(..., step_order=0)` — TypeError, no step_order param

## Agent types
- `EXECUTOR` (default) — Runs code
- `PLANNER` — Creates child arcs, limited tool access
- `REVIEWER` — Reviews untrusted output
- `JUDGE` — Validates data against policies

## Code review with specific model
When asked to review a pending diff with a specific model, create a child REVIEWER arc:
```python
review_arc_id = arc.add_child(
    parent_id=coding_change_arc_id,
    name="Review code changes",
    goal="Review the pending diff and report findings",
    agent_type="REVIEWER",
    model="claude-sonnet-4-5-20250929",
)
```
The `model` parameter sets the specific AI model for that arc.

## Simple timed messages
For reminders and notifications that don't need code execution, use `cron.message`:
```python
from carpenter_tools.act import scheduling
from datetime import datetime, timedelta
target_iso = (datetime.now() + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S")
scheduling.add_once(
    name="break-reminder",
    at_iso=target_iso,
    event_type="cron.message",
    event_payload={"message": "Time to take a break!"},
)
```
- `conversation_id` is auto-injected — do NOT pass it explicitly
- See [[scheduling/patterns]] for delayed arc execution and recurring patterns

## Related
[[arcs/tools]] · [[scheduling/patterns]] · [[security/trust-boundaries]] · [[git/tools]]
