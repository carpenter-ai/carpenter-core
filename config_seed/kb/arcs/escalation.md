# Arc Escalation

Self-escalation lets an arc request a stronger model when it determines it cannot complete its task.

## Chat tools
- `escalate` — Self-escalate: freeze this arc and create a stronger sibling. The new arc gets full read access to this arc's subtree via a read grant. No parameters needed.
- `escalate_current_arc(reason, task_type)` — Request escalation to a more powerful model from the chat context (not from within an arc).

## How self-escalation works
1. Arc calls the `escalate` tool (zero params)
2. Platform freezes the current arc (status → `escalated`)
3. Platform creates a sibling PLANNER arc with the next model in the escalation stack
4. New arc receives an enhanced goal with escalation context and a summary of the original arc's children
5. A read grant is created so the new arc can inspect the original arc's subtree via `get_arc_detail`

## Escalation stacks
Configured in `config.yaml` under `escalation.stacks`:
```yaml
escalation:
  stacks:
    general:
      - anthropic:claude-haiku-4-5-20251001
      - anthropic:claude-sonnet-4-5-20250929
    coding:
      - anthropic:claude-haiku-4-5-20251001
      - anthropic:claude-sonnet-4-5-20250929
  require_confirmation: false
```

Each stack is an ordered list from weakest to strongest. When `escalate` is called, the platform finds the current model in the stack and moves to the next one.

## Related
[[arcs/tools]] · [[arcs/read-grants]] · [[config/models]]
