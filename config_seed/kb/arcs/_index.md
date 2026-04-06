# Arcs

Units of work in a tree structure. Arcs are the core execution primitive.

## In this section
- [[arcs/tools]] — Create, manage, and inspect arcs
- [[arcs/state-tools]] — Read and write arc state
- [[arcs/planning]] — Workflow planning guide
- [[arcs/templates]] — Available workflow templates
- [[arcs/escalation]] — Self-escalation tool and escalation stacks
- [[arcs/read-grants]] — Cross-arc read grants

## State machine
pending → active → waiting/completed/failed/cancelled/escalated

Frozen statuses (completed/failed/cancelled/escalated) are immutable.

## Related
[[scheduling/tools]] · [[security/trust-boundaries]]
