# Failure Patterns

Common failure modes encountered during platform operation, with symptoms, root causes, and specific escape routes.

## Resource files

| Resource | Contents |
|----------|----------|
| `execution.md` | Execution failures: timeout, OOM, permission denied, import error, callback unreachable |
| `review.md` | Review pipeline failures: injection false positive, AST parse error, reviewer unavailable, REWORK loop exhaustion |
| `network.md` | Network failures: callback timeout, DNS resolution, rate limiting, connection refused |
| `state.md` | State and data failures: encryption unavailable, DB locked, key not found, arc frozen, taint boundary violations |
| `context.md` | Context and conversation failures: window exhaustion, tool output too large, taint preventing skill KB modification |

## Quick reference

If you know the error message, jump directly to the relevant resource file. If not, scan the symptoms lists in each file to find the matching pattern.

## Related

[[skills/failure-patterns/context]] · [[skills/failure-patterns/execution]] · [[skills/failure-patterns/network]] · [[skills/failure-patterns/review]] · [[skills/failure-patterns/state]]
