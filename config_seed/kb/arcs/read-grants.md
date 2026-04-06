# Cross-Arc Read Grants

Read grants allow arcs to read state across the normal parent-child tree boundaries. By default, arcs can only read state of their descendants. A read grant extends this to allow reading a specific arc (and optionally its subtree).

## Python module tool
- `arc.grant_read_access(reader_arc_id, target_arc_id, depth?)` — Grant one arc read access to another arc's state. `depth` is `"self"` (exact arc only) or `"subtree"` (arc and all descendants, default). Called via `submit_code`.

## When read grants are used
- **Self-escalation**: When an arc calls `escalate`, the platform automatically grants the new sibling read access to the original arc's subtree
- **Platform escalation**: Automatic escalation on failure also creates read grants
- **Manual grants**: A parent arc or chat agent can explicitly grant read access between arcs using `arc.grant_read_access()` via `submit_code`

## Access control
Read grants bypass topology checks (parent-child), but NOT trust boundaries. An arc with a read grant can call `get_arc_detail(arc_id=N)` to read the target's state, children, and history.

## Related
[[arcs/escalation]] · [[arcs/tools]] · [[security/trust-boundaries]]
