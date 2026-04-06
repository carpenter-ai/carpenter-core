# Scheduling Patterns

## Delayed arc execution
To schedule an action for a future time (reminders, delayed sends):

```python
from carpenter_tools.act import arc, scheduling
from datetime import datetime, timedelta

target_iso = (datetime.now() + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
arc_id = arc.create(
    name="reminder: take a break",
    goal="Send 'Time for a break!' using messaging.send()",
    wait_until=target_iso,
)
scheduling.add_once(
    name="break-reminder",
    at_iso=target_iso,
    event_type="arc.dispatch",
    event_payload={"arc_id": arc_id},
)
```

- `wait_until` prevents heartbeat from dispatching early
- `add_once` fires a trigger that auto-deletes
- Use both together for time-delayed execution

## Recurring message delivery (reminders, notifications)
For simple recurring messages that don't need web access or complex logic,
use `cron.message` event type — it delivers directly to the conversation:

```python
from carpenter_tools.act import scheduling

scheduling.add_cron(
    name="posture-check",
    cron_expr="*/1 * * * *",  # every minute
    event_type="cron.message",
    event_payload={"message": "Time to check your posture!"},
)
```

- `event_type="cron.message"` delivers the message directly (no arc needed)
- `conversation_id` is auto-injected — no need to pass it
- To cancel: `scheduling.remove_cron(name="posture-check")`

## Recurring arc execution (monitoring, fetch + analyze)
For recurring tasks that need code execution (web fetches, data processing),
use `arc.dispatch` event type. Create a **single EXECUTOR arc** (NOT a
PLANNER with children). The platform clones the arc on each fire:

```python
from carpenter_tools.act import arc, scheduling

# IMPORTANT: Create a standalone EXECUTOR arc — do NOT use add_child or PLANNER.
# The arc's goal tells the executor what to do on each recurring fire.
arc_id = arc.create(
    name="Check httpbin endpoint",
    goal="Fetch https://httpbin.org/status/200 and report status via messaging.send(). Include the HTTP status code in the message.",
)
scheduling.add_cron(
    name="httpbin-monitor",
    cron_expr="*/2 * * * *",  # every 2 minutes
    event_type="arc.dispatch",
    event_payload={"arc_id": arc_id},
)
```

- On each fire, the platform creates a fresh arc copy and executes it
- The arc's `messaging.send()` calls deliver to the original conversation
- `conversation_id` is auto-injected into the cron payload
- **Do NOT** create a PLANNER/parent arc for recurring tasks — use a single EXECUTOR
- To cancel: `scheduling.remove_cron(name="httpbin-monitor")`

## Related
[[scheduling/tools]] · [[arcs/planning]]
