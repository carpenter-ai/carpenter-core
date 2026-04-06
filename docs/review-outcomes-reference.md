# Review Outcomes Reference

Quick reference for Carpenter's 5 standardized review outcomes.

## The Five Outcomes

```
┌─────────────┬─────────┬──────────────────────┬────────┬──────────┐
│ Outcome     │ Symbol  │ Meaning              │ Retry? │ Decider  │
├─────────────┼─────────┼──────────────────────┼────────┼──────────┤
│ CACHED      │    ✓    │ Previously approved  │  N/A   │ Auto     │
│ APPROVE     │   ✅    │ Safe to execute      │  N/A   │ AI+Auto  │
│ REWORK      │   ⚠️    │ Fixable issues       │ Yes(3x)│ Agent    │
│ MAJOR       │   🚨    │ Needs human judgment │ Limited│ Human    │
│ REJECTED    │   🚫    │ Policy violation     │   No   │ Auto     │
└─────────────┴─────────┴──────────────────────┴────────┴──────────┘
```

## Decision Flow

```
Code Submitted
    │
    ├─ Hash match? ──YES──> CACHED (execute)
    │
    └─ NO
        │
        ├─ "from x import *"? ──YES──> REJECTED (fail)
        │
        └─ NO
            │
            ├─ High-risk pattern? ──YES──> MAJOR (human)
            │
            └─ NO
                │
                └─ AI Review
                    │
                    ├─ APPROVE ──> APPROVE (execute)
                    ├─ MINOR ──> REWORK (agent fixes)
                    └─ MAJOR ──> MAJOR (human)
```

## Detailed Breakdown

### CACHED ✓
**When**: Code hash matches previous approval
**Action**: Execute immediately (skip analysis)
**User impact**: None (silent optimization)

---

### APPROVE ✅
**When**: All checks pass
**Action**: Execute code
**User sees**: "Changes approved and applied"

---

### REWORK ⚠️
**When**: Fixable issues detected
- Syntax errors
- Minor logic issues
- Medium-risk patterns
- Style problems

**Action**: Agent revises with feedback
**Retry**: 3 attempts → auto-escalate to MAJOR
**User sees**: "Revision requested (Attempt N/3)"

**Examples**:
```
"REWORK: Syntax error on line 5 - expected ':'"
"REWORK: Code reads 3 files but user only asked for 1"
"REWORK: Uses getattr with string - prefer direct access"
```

---

### MAJOR 🚨
**When**: Security concern or serious deviation
- High-risk injection
- High-risk indirection
- AI flags major concern
- Agent failed after 3 REWORK attempts

**Action**: Pause for human decision
**User options**:
1. Approve anyway (informed override)
2. Reject (cleanup)
3. Revise with expanded guidance

**Examples**:
```
"MAJOR: Code makes external network call not discussed"
"MAJOR: Uses exec() with dynamic input"
"MAJOR: Agent failed to satisfy reviewer after 3 attempts"
```

---

### REJECTED 🚫
**When**: Policy violation
- Currently: `from X import *`
- Future: Other banned patterns

**Action**: Immediate failure, cleanup
**Retry**: Never (policy is non-negotiable)
**User must**: Change approach or abandon

**Example**:
```
"REJECTED: Wildcard imports not allowed
Line 5: from os import *
See coding-guidelines.md"
```

## Key Differences

### REWORK vs MAJOR vs REJECTED

| Question | REWORK | MAJOR | REJECTED |
|----------|--------|-------|----------|
| Can agent fix? | Probably | Maybe | No |
| Who decides next? | Agent | Human | Policy |
| Retry allowed? | Yes (3x) | Human choice | Never |
| Override possible? | N/A | Yes | No |
| When detected? | Anytime | Anytime | Early |

### Why 5 outcomes?

1. **CACHED**: Performance optimization
2. **APPROVE**: Clear success path
3. **REWORK**: Unified fixable issues (with feedback)
4. **MAJOR**: Human judgment required
5. **REJECTED**: Hard policy enforcement

**No gaps, no redundancy, clear semantics.**

## Implementation

```python
from carpenter.review.pipeline import ReviewOutcome

# Enum definition
class ReviewOutcome(Enum):
    APPROVE = "approve"
    REWORK = "rework"
    MAJOR = "major"
    REJECTED = "rejected"
```

## Legacy Mapping

For backward compatibility, outcomes map to legacy status strings:

| Outcome | Legacy Status |
|---------|---------------|
| CACHED | "cached_approval" |
| APPROVE | "approved" |
| REWORK | "minor_concern" |
| MAJOR | "major_alert" |
| REJECTED | "rejected" |

## Feedback Mechanism

All outcomes except CACHED and APPROVE include feedback:

```python
# REWORK examples
"Syntax error on line 5: expected ':'"
"Code writes to 2 files but user only mentioned 1"

# MAJOR examples
"Code performs actions not discussed with user"
"High-risk pattern detected: exec() with variable"

# REJECTED examples
"Policy violation: from os import *"
```

The feedback string is **agnostic to issue type** - it describes what needs fixing, regardless of whether it's syntax, logic, security, or style.

## Escalation Path

```
REWORK (Attempt 1)
    ↓ Agent fixes
REWORK (Attempt 2)
    ↓ Agent fixes
REWORK (Attempt 3)
    ↓ Agent fixes (final attempt)
    ↓ Still REWORK?
MAJOR (Auto-escalated)
    ↓
Human Decision
```

## See Also

- **Implementation**: `carpenter/review/pipeline.py`
- **Tests**: `tests/review/test_review_outcomes.py`
- **Coding rules**: `coding-guidelines.md`
