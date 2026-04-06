# Trust Boundaries

Three-level integrity lattice for arc data.

## Integrity levels
- **Trusted** (default) — Full access, standard review
- **Constrained** — Q-LLM extracted data, needs policy check
- **Untrusted** — Raw external data, needs full review pipeline

## Rules
- Arcs fetching external data (web, webhooks, APIs) MUST be untrusted
- Every untrusted arc MUST have at least one review arc as a sibling
- Trusted arcs CANNOT read output from untrusted arcs
- Use `arc.get_plan()` and `arc.get_children_plan()` to read arc structural data safely

## Who creates arcs?
The AGENT creates arcs, not the platform. The platform enforces boundaries:
- Callback API returns 403 if web tools are called from chat or trusted context
- Tainted conversations get output withheld (metadata only)
- Individual arc.create() rejects integrity_level='untrusted'

But the platform does NOT auto-create arcs when it detects tainted code. Your job
as the planning agent is to recognise when external data access is needed and
create the proper arc batch BEFORE attempting the access.

## Creating untrusted arcs
Use `arc.create_batch()` -- individual `arc.create()` rejects untrusted. A batch must include REVIEWER and JUDGE arcs alongside the untrusted arc.

## Taint tracking
Tools declare `trusted_output` via `@tool()`. Untrusted tool use taints the conversation. Tainted `submit_code` returns only metadata, never raw output.

## Related
[[security/review-pipeline]] · [[arcs/planning]] · [[web/trust-warning]]
