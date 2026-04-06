# The Iterative Planning Pattern

## Overview

A planner creates pairs of sibling arcs under a single parent:

1. **Implementation arc** -- does the work (runs code, generates output, makes changes)
2. **Monitor arc** -- evaluates the result and decides whether to continue or stop

All arcs are flat siblings under one parent, executing in step_order. Each monitor, if it decides to continue, creates two more sibling arcs (next implementation + next monitor) with incrementing step_orders. When a monitor decides "done" and creates no more arcs, all children eventually complete and the parent regains agency.

```
Parent Arc (planner, creates first pair then waits)
  |-- Impl 1    (step_order=1)
  |-- Monitor 1 (step_order=2, decides continue -> creates Impl 2 + Monitor 2)
  |-- Impl 2    (step_order=3)
  |-- Monitor 2 (step_order=4, decides continue -> creates Impl 3 + Monitor 3)
  |-- Impl 3    (step_order=5)
  |-- Monitor 3 (step_order=6, decides done)
```

The parent waits (status `waiting`) while children execute sequentially. Because each monitor runs only after its preceding implementation completes (step_order dependency), the pattern is naturally sequential: implement, evaluate, decide, repeat.

## Monitor types

The monitor arc can take several forms depending on the use case:

### Script monitor
A simple Python script that checks data conditions. Fast, cheap, deterministic. Good for pass_rate thresholds, error counts, timeout checks.

### Agent monitor
An agent arc that reasons about whether the approach is working. More expensive but can make qualitative judgments about progress, detect when the approach needs to change, or decide to escalate.

### Human-gated monitor
A monitor that sets its status to `waiting` with an activation condition on a human approval event. Useful for review checkpoints in sensitive workflows.

## Performance counters

Monitors should read platform-managed counters from the parent arc via `get_arc_detail`. These counters are maintained by the platform itself and cannot be tampered with by executor code:

- **`descendant_tokens`** -- total API tokens consumed by all arcs under the parent
- **`descendant_executions`** -- total code executions under the parent
- **`descendant_arc_count`** -- total child arcs created under the parent

To read them, the monitor code calls:

```python
from carpenter_tools.read.arc import get_arc_detail
parent_info = get_arc_detail(parent_arc_id)
# parent_info contains descendant_tokens, descendant_executions, descendant_arc_count
```

These counters enable resource-aware decisions: "stop iterating if we have consumed more than 50,000 tokens" or "stop after 20 code executions."

## When to stop

A monitor should stop the loop (not create more arcs) when any of these conditions hold:

1. **Success condition met** -- pass_rate exceeds threshold, task completed, approval received, external condition satisfied
2. **Resource limits hit** -- token budget exceeded (check `descendant_tokens`), too many iterations (check `descendant_arc_count`), too many executions (check `descendant_executions`)
3. **Diminishing returns** -- the last N iterations did not improve metrics. The monitor can track progress in arc state and compare across iterations.
4. **Hard iteration cap** -- always set a maximum number of iterations to prevent runaway loops. A monitor that has created more than the cap number of arc pairs should stop unconditionally.

## Review caching

If the implementation code is identical across iterations (e.g., the same script reads different state each time), it only goes through the review pipeline once. The SHA-256 hash check in the review pipeline passes on subsequent iterations, so the code is approved immediately. This makes repeated iterations cheap from a review perspective.

## Key design decisions

- **Flat sibling structure**: All arcs are siblings under one parent, not nested. This keeps the tree shallow and makes counter tracking straightforward.
- **Step_order sequencing**: The platform's dependency check ensures each arc runs only after all preceding siblings complete. No explicit wait logic needed.
- **Monitor creates next pair**: The monitor is the decision point. It either creates the next pair (continue) or does nothing (stop). This gives the monitor full control over loop termination.
- **Parent regains agency**: When all children complete, the parent's status can transition from `waiting` back to `active`. The parent can then inspect results and proceed.
