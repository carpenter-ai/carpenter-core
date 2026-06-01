# Scheduling Patterns

Patterns for `scheduling.add_cron` / `scheduling.add_once`. Cron's hard
minimum granularity is **1 minute**; sub-minute requests round UP to
`"* * * * *"`. `conversation_id` is auto-injected into every cron payload.

## Recurring "ping me / monitor / remind me" — use `cron.message`

For ANY "monitor X and tell me" / "ping me every N" / "remind me…"
request: ONE `add_cron` with `event_type="cron.message"` and a
NON-EMPTY `event_payload={"message": "..."}`. The string is delivered on
every fire and cannot fail at runtime.

```python
from carpenter_tools.act import scheduling

scheduling.add_cron(
    name="s031-monitor-httpbin",     # follow any name prefix the user gave
    cron_expr="* * * * *",           # every minute — cron's finest granularity
    event_type="cron.message",
    event_payload={
        "message": "Monitoring check: httpbin.org/status/200 — status OK (200). Say 'stop monitoring' to cancel."
    },
)
```

Cancel: `scheduling.remove_cron(name="...")`. Use `arc.dispatch` (below)
ONLY when the user needs the LIVE result of a fetch/computation each
fire (e.g. "alert me when the price drops below $50").

## Sub-minute requests ("every 30s / 10s / 5s") — JUST DO IT

The 1-minute floor is the only available answer. VERY NEXT action:
submit code creating the cron at `cron_expr="* * * * *"`. After
approval, send ONE short confirmation noting the 1-minute resolution.
The reviewer is explicitly instructed to APPROVE `* * * * *` here.

After approval message: *"Monitoring httpbin once per minute (cron's
finest resolution). Say 'stop monitoring' to cancel."*

### Pitfalls

- Empty `event_payload["message"]` delivers silently — the user sees nothing.
- Splitting arc-creation and cron-registration across separate `submit_code` calls leaves the cron pointing at a missing arc.

## Recurring LIVE fetch / compute — `arc.dispatch`

Use ONLY when each fire must execute fresh code. Create a **single
standalone EXECUTOR arc** (NOT a PLANNER with children). The arc's `goal`
MUST direct it to call `messaging.send(message="...")` on every fire,
or the cron fires silently.

```python
from carpenter_tools.act import arc, scheduling

# arc.create() returns {"arc_id": <int>} — unwrap to int.
arc_id = arc.create(
    name="Check httpbin endpoint",
    goal=("Fetch https://httpbin.org/status/200 and report status via "
          "messaging.send(). Include the HTTP status code in the message."),
)["arc_id"]
scheduling.add_cron(
    name="httpbin-monitor",
    cron_expr="*/2 * * * *",
    event_type="arc.dispatch",
    event_payload={"arc_id": arc_id},   # MUST be int
)
```

## Delayed (one-shot) arc execution

Reminders / future-time sends: `arc.create(wait_until=…)` + `add_once`.
`wait_until` prevents heartbeat dispatching early; `add_once` auto-deletes.

```python
from carpenter_tools.act import arc, scheduling
from datetime import datetime, timedelta

target_iso = (datetime.now() + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
arc_id = arc.create(
    name="reminder: take a break",
    goal="Send 'Time for a break!' using messaging.send()",
    wait_until=target_iso,
)["arc_id"]
scheduling.add_once(
    name="break-reminder",
    at_iso=target_iso,
    event_type="arc.dispatch",
    event_payload={"arc_id": arc_id},
)
```

## Modifying an existing schedule

No in-place update. `remove_cron` then `add_cron` under the **same name**
in ONE `submit_code` call. `remove_cron` is a no-op on missing names.

```python
from carpenter_tools.act import scheduling

scheduling.remove_cron(name="posture-check")
scheduling.add_cron(
    name="posture-check",
    cron_expr="*/1 * * * *",
    event_type="cron.message",
    event_payload={"message": "Arrr, check yer posture, matey!"},
)
```

## Related
[[scheduling/tools]] · [[arcs/planning]]
