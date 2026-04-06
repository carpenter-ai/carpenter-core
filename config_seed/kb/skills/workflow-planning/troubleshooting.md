# Arc Operations Troubleshooting

## Error: "unable to open database file"

**Full error**:
```
sqlite3.OperationalError: unable to open database file
```

**Root cause**: You imported platform internals from submitted code.

**What you probably did**:
```python
# ❌ WRONG
import sys
sys.path.insert(0, '/path/to/YourProject')
from carpenter.core import arc_manager
from carpenter.db import get_db

arc_id = arc_manager.create_arc(...)
```

**Why it fails**:
- Submitted code runs in an isolated executor subprocess
- The executor cannot access the platform's database file at `~/carpenter/data/platform.db`
- Even though `config.CONFIG["database_path"]` is set correctly, the subprocess doesn't have permissions or context to open it

**Solution**:
```python
# ✓ CORRECT
from carpenter_tools.act import arc

result = arc.create(
    name="My Arc",
    goal="Do something"
)
arc_id = result["arc_id"]
```

The `carpenter_tools.act.arc` module is a callback wrapper that sends HTTP requests to the platform, which then executes `arc_manager.create_arc()` in the correct context.

## Error: HTTP 403 Forbidden on arc.create

**Full error**:
```
HTTP 403: Forbidden - Execution session not found or expired
```

**Root cause**: You tried to call an action tool (`carpenter_tools.act.*`) from outside submitted code.

**What you probably did**:
- Called `arc.create()` directly in a script that wasn't submitted via `submit_code`
- Or the execution session expired (sessions last 1 hour)

**Solution**:
- Action tools (`carpenter_tools.act.*`) can ONLY be called from code submitted via `submit_code`
- Use chat tools (`list_arcs`, `get_arc_detail`) for read-only operations from chat context
- Use `carpenter_tools.read.*` for read operations from submitted code

## Error: "Tainted arcs require at least one REVIEWER"

**Full error**:
```
{"error": "Tainted arcs require at least one REVIEWER or JUDGE arc"}
```

**Root cause**: You tried to create a tainted arc without reviewers.

**What you did**:
```python
# ❌ Won't work
arc.create(
    name="Fetch web data",
    taint_level="tainted",
    ...
)
```

**Solution**: Use `arc.create_batch()` with reviewers:
```python
arc.create_batch(
    parent_id=parent_id,
    arcs=[
        {
            "name": "Fetch web data",
            "taint_level": "tainted",
            "output_type": "json",
            ...
        },
        {
            "name": "Review data",
            "agent_type": "REVIEWER",
            "taint_level": "review",
            ...
        },
        {
            "name": "Judge review",
            "agent_type": "JUDGE",
            "taint_level": "review",
            ...
        }
    ]
)
```

## Error: "Maximum one JUDGE arc allowed per batch"

**Root cause**: You included multiple JUDGE arcs in a single `create_batch()` call.

**Solution**: Only include ONE judge arc per batch. The judge is the final decision maker.

## Error: "Clean arc cannot read output from tainted arc"

**Symptom**: You try to read `arc.read_output_UNTRUSTED()` from a clean arc and get HTTP 403.

**Root cause**: Clean arcs are isolated from untrusted data. The callback handler enforces this.

**Solution**:
- Create a review arc (with `agent_type="REVIEWER"` and `taint_level="review"`)
- The review arc CAN read untrusted output
- After the review arc completes and the judge approves, subsequent clean arcs can depend on the review outcome (stored in clean state)

## Common Architectural Misunderstandings

### Misunderstanding 1: "The agent can't create arcs"

**Wrong**: The agent CAN create arcs.

**Right**: The agent must submit code that imports `carpenter_tools.act.arc`, not directly call platform code.

### Misunderstanding 2: "I need to set database_path"

**Wrong**: The database path is already configured correctly.

**Right**: The problem is trying to access the database from executor code (architectural boundary violation).

### Misunderstanding 3: "I should use arc_manager.create_arc() for better performance"

**Wrong**: Direct platform calls fail from executor context.

**Right**: Callback tools (`carpenter_tools.act.arc`) are the ONLY way to modify arcs from submitted code. They work via HTTP, which is the correct architectural pattern.

## Debugging Tips

### Check execution logs

If submitted code fails, read the log:
```python
# After submit_code returns execution_id
# Use get_execution_output tool to see full traceback
```

### Verify imports

Good pattern:
```python
# ✓ Executor-safe imports
from carpenter_tools.act import arc, state, files
from carpenter_tools.read import arc as arc_read
import json  # stdlib is fine
import requests  # pip packages work if installed in executor environment
```

Bad pattern:
```python
# ❌ Will fail
from carpenter.core import arc_manager
from carpenter.db import get_db
import carpenter.config
```

### Test arc creation

Minimal test:
```python
from carpenter_tools.act import arc

result = arc.create(
    name="Test Arc",
    goal="Testing arc creation",
    parent_id=None
)

print(f"Success! Created arc #{result['arc_id']}")
```

If this fails, check:
1. Is this code submitted via `submit_code`? (not run directly)
2. Is the execution session valid? (should be automatic)
3. Are there any network/firewall issues preventing executor → platform HTTP callbacks?
