# Verified Flow Analysis

**Status:** Design document
**Date:** 2026-03-18
**Companion to:** [`trust-invariants.md`](trust-invariants.md)

---

## Problem Statement

The information-flow security architecture (see companion document)
establishes a hard control-flow boundary between TRUSTED and CONSTRAINED
data.  By default, CONSTRAINED data cannot influence planner decisions.

However, many practical workflows need control flow to depend on external
data: "if there's a meeting request, create a calendar event."  The
question is: under what conditions can we safely allow CONSTRAINED data
to influence control flow?

This document describes **verified flow analysis** — a static
verification technique that proves, for a specific piece of code, that
all possible values of CONSTRAINED inputs lead to policy-compliant
outcomes.  If the proof succeeds, those CONSTRAINED inputs are permitted
to influence control flow in that specific code.

---

## Core Idea

A piece of code that branches on CONSTRAINED data is a program with
untrusted inputs.  If the input space is bounded (booleans, small enums,
bounded-length lists of bounded types), we can **enumerate all possible
inputs and verify that every execution path satisfies security policies**.

This is exhaustive verification, not sampling or heuristic analysis.
If it passes, the code is safe under all possible CONSTRAINED inputs —
including adversarial ones.

```
For every combination of CONSTRAINED input values:
    Simulate execution
    Check: all arcs created comply with security policies?
    Check: no policy violations in any reachable state?
    Check: all tool calls use valid policy-typed arguments?

If ALL combinations pass → code is verified → allow CONSTRAINED branching
If ANY combination fails → code is rejected → planner must restructure
```

---

## The Whitelisted Python Subset

Verified flow analysis operates on a restricted subset of Python.  The
restriction ensures that (a) all execution paths are enumerable and
(b) taint labels can be tracked through all operations.

### Allowed Constructs

| Construct | Why allowed | Verification impact |
|-----------|------------|-------------------|
| `if` / `elif` / `else` | Finite branches | Each branch explored |
| `for` over bounded iterables | Bounded by schema (max_length) | Unrolled during verification |
| Variable assignment | State tracking | Labels propagated |
| Comparison operators | Deterministic | Label rules applied |
| Boolean operators (`and`/`or`/`not`) | Deterministic | Short-circuit paths explored |
| Calls to platform tools | Mocked during verification | Policy compliance checked |
| List/dict/tuple literals | Data construction | All elements labeled |
| Comprehensions over bounded iterables | Bounded | Unrolled |
| Subscript access (`x["key"]`, `x[0]`) | Deterministic | Label inherited |
| Attribute access (`x.field`) | Deterministic | Label inherited |
| f-strings | String formatting | Label is join of parts |
| `try` / `except` | Two paths | Both explored |
| `from carpenter_tools import ...` | Platform tool access | Mocked during verification |

### Rejected Constructs

| Construct | Why rejected | Alternative |
|-----------|-------------|-------------|
| `while` | Unbounded iteration | Use arc-level iteration (mutable arcs) |
| `def` / `lambda` | Unbounded call graph | Keep code flat; split across arcs |
| `import` (non-carpenter_tools) | Arbitrary code | Use platform tools |
| `eval` / `exec` / `compile` | Arbitrary execution | Not needed for orchestration |
| `break` / `continue` | Complicates path analysis | `for` over bounded lists suffices |
| Class definitions | Unnecessary complexity | Use plain data structures |
| `yield` / `async` / `await` | Concurrency | Arc-level concurrency instead |
| `with` | Opaque context managers | Use explicit try/except |

### Expressiveness

This subset covers all reasonable workflow orchestration patterns:

- **Branch on condition**: `if` / `elif` / `else`
- **Process a batch**: `for item in bounded_list`
- **Combine checks**: `and` / `or` / `not`
- **Handle errors**: `try` / `except`
- **Create arcs**: `arc.add_child(...)`, `arc.create_batch(...)`
- **Read state**: `state.get(...)`
- **Pattern match**: `if x == Email("..."):` / `if x in [Enum(...), ...]:`

Complex processing that needs `while`, recursion, or imported libraries
happens in EXECUTOR arcs with full Python — those arcs are not verified
for flow analysis but go through the code review pipeline instead.

---

## Taint Label Tracking

### Layer 1: Static AST Analysis

At code submission time, walk the AST and propagate labels through
all expressions:

**Label assignment rules:**

```
Literal without policy type             → REJECTED (in comparisons against C)
Policy-typed literal: Email("...")      → T (if passes policy validation)
state.get(key, arc_id=X)               → label inherited from arc X
platform_tool_call(...)                 → T (platform tools are trusted)
x op y                                 → join(label(x), label(y))
x[key]                                 → join(label(x), label(key))
f"...{x}..."                           → join of all interpolated labels
```

**The comparison rule:**

When a CONSTRAINED value is compared against a policy-typed TRUSTED
literal, the result is TRUSTED.  This is the decomposition pattern
(deterministic check against trusted reference) happening inline:

```
C == T(policy-typed)    → T    (policy check)
C in [T, T, T]         → T    (membership check against trusted set)
C > T(IntRange)         → T    (threshold check against trusted bound)
C == C                  → C    (no trusted reference — stays constrained)
```

**Control-flow check:**

For every `if` / `elif` condition: is the condition's label T?
- If yes: proceed (the branch decision is trusted)
- If no: flag for dry-run verification (the branch depends on
  constrained data)

### Layer 2: Dry-Run Verification with Tracked Wrappers

For code that branches on CONSTRAINED data (flagged by Layer 1), run
the code with lightweight tracked value wrappers that carry labels
through Python's native evaluation.

#### Tracked Value Classes

```python
class Tracked:
    """Value wrapper that carries a taint label through operations."""

    def __init__(self, value, label):
        self.value = value
        self.label = label  # 'T', 'C', or 'U'

    def __eq__(self, other):
        other_label = other.label if isinstance(other, Tracked) else 'T'
        result = self.value == (other.value if isinstance(other, Tracked) else other)

        # Decomposition pattern: C checked against T → T
        if (self.label == 'C' and other_label == 'T') or \
           (self.label == 'T' and other_label == 'C'):
            return Tracked(result, 'T')
        return Tracked(result, join(self.label, other_label))

    def __bool__(self):
        if self.label != 'T':
            raise ConstrainedControlFlow(
                "CONSTRAINED data reached control-flow position "
                "without policy check"
            )
        return bool(self.value)

    # Similar for __lt__, __gt__, __le__, __ge__, __ne__, __hash__
```

```python
class TrackedList:
    """List wrapper that yields tracked elements and tracks membership."""

    def __init__(self, items, label):
        self.items = items  # list of Tracked values
        self.label = label

    def __contains__(self, item):
        item_val = item.value if isinstance(item, Tracked) else item
        result = any(i.value == item_val for i in self.items)
        item_label = item.label if isinstance(item, Tracked) else 'T'

        # If all list items are T: membership check is a policy check
        if all(i.label == 'T' for i in self.items) and item_label == 'C':
            return Tracked(result, 'T')
        return Tracked(result, join(self.label, item_label))

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)
```

#### Why This Is Exhaustive

Every control-flow decision in the whitelisted Python subset passes
through `__bool__`:

- `if x:` → `x.__bool__()`
- `if x == y:` → `x.__eq__(y).__bool__()`
- `if x and y:` → `x.__bool__()` (short-circuit) then `y.__bool__()`
- `if x in lst:` → `lst.__contains__(x).__bool__()`
- Comprehension filter → `filter_expr.__bool__()`

If `__bool__` raises when the label isn't T, every control-flow use
of CONSTRAINED data is caught.  The `__eq__` / `__contains__` methods
apply the decomposition rule (C checked against T → T), so policy-typed
comparisons produce TRUSTED booleans that pass `__bool__`.

For iteration: `for item in tracked_list` calls `__iter__`, which yields
tracked elements.  Each element inherits the list's label.  If the
element is used in a condition, `__bool__` catches it.

#### Dry-Run Execution

For each combination of CONSTRAINED input values:

1. Inject tracked wrappers for CONSTRAINED inputs
2. Mock platform tool calls (record what would be called with what args)
3. Execute the code in the mocked environment
4. If `ConstrainedControlFlow` is raised → FAIL (code rejected)
5. If execution completes → verify all recorded tool calls comply with
   security policies (valid policy-typed args, allowed operations)

If ALL input combinations pass → code is verified.

---

## Policy-Typed Literals

Literals used in comparisons against CONSTRAINED data must declare
their type.  The type determines which security policy validates them.

### How It Works

```python
from carpenter_tools.policy import Email

ben = Email("ben@website.com")
```

At verification time:
1. `Email("ben@website.com")` calls the Email policy validator
2. Validator checks: is "ben@website.com" in the email allowlist?
3. If yes: `ben` gets label T, verification proceeds
4. If no: verification fails ("literal rejected by email policy")

At runtime (after verification): the policy-typed constructor is a
no-op wrapper — the code was already verified and hashed.

### Policy Validation Defaults

All policies default to **deny** (empty allowlist).  The user must
explicitly approve values.

| Type | Default validation | Configurable |
|------|-------------------|-------------|
| `Email` | Must be in email allowlist | `security.email_allowlist` |
| `Domain` | Must be in domain allowlist | `security.domain_allowlist` |
| `Url` | Scheme must be https; domain must be in allowlist | `security.url_allowlist` |
| `FilePath` | Must be under allowed directories | `security.filepath_allowlist` |
| `Command` | Must be in command allowlist | `security.command_allowlist` |
| `IntRange` | Value must be within declared bounds | Bounds declared in type |
| `Enum` | Value must be in declared set | Set declared in type |
| `Bool` | Always valid (trivially bounded) | N/A |
| `Pattern` | Must pass ReDoS safety check | N/A |

A fresh installation has empty allowlists.  No policy-typed literal
will validate until the user configures their policies.  This means
CONSTRAINED data cannot influence control flow at all until the user
explicitly approves the comparison targets.

### Untyped Literals Are Rejected

```python
sender = state.get("sender_email", arc_id=X)  # C

# REJECTED: untyped literal in comparison against CONSTRAINED data
if sender == "ben@website.com":
    ...

# ALLOWED: policy-typed literal, validated against allowlist
if sender == Email("ben@website.com"):
    ...
```

The rationale: an untyped literal could be anything — a SQL fragment,
a file path, an injection payload.  Requiring a policy type forces the
coder to declare what the literal means and the platform to validate it.

---

## The Verification Flow

### Step by Step

```
Code submitted with CONSTRAINED inputs
         │
         ▼
┌─────────────────────┐
│ 1. AST Parse        │  Parse with ast.parse()
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│ 2. Whitelist Check   │  Only allowed constructs?
│                     │  Only carpenter_tools imports?
└────────┬────────────┘  Reject immediately if not.
         │
         ▼
┌─────────────────────┐
│ 3. Static Taint     │  Propagate labels through AST.
│    Analysis         │  Identify all C data in conditions.
│                     │  Validate all policy-typed literals.
└────────┬────────────┘
         │
         ├──── No C data in conditions ──── ▶ PASS (no verification needed)
         │
         ▼
┌─────────────────────┐
│ 4. Input Space      │  Compute from Pydantic schemas.
│    Enumeration      │  bool: 2 values.
│                     │  Enum[N]: N values.
│                     │  List[T, max=K]: K^|T| combinations.
└────────┬────────────┘
         │
         ├──── Space > threshold ──── ▶ REJECT (ask planner to split)
         │
         ▼
┌─────────────────────┐
│ 5. Dry Run          │  For each input combination:
│                     │    - Inject tracked wrappers
│                     │    - Mock platform tools
│                     │    - Execute code
│                     │    - Check for ConstrainedControlFlow errors
│                     │    - Verify all tool calls policy-compliant
└────────┬────────────┘
         │
         ├──── Any run fails ──── ▶ REJECT (tell planner what failed)
         │
         ▼
┌─────────────────────┐
│ 6. Hash and Trust   │  Hash verified code.
│                     │  Store hash + input schemas in registry.
│                     │  Code is now trusted for these inputs.
└─────────────────────┘
```

### At Runtime

1. Code is submitted for execution
2. Platform computes hash of the code
3. If hash matches a verified entry in the trust registry:
   - CONSTRAINED inputs of the verified types are allowed
   - Runtime policy checks still run (defense-in-depth)
4. If hash does NOT match:
   - CONSTRAINED data blocked from control flow
   - Code must be re-verified

### Handling Combinatorial Explosion

If the input space exceeds the verification threshold (configurable,
default perhaps 1024 combinations), the platform rejects the code
and asks the planner to restructure.

Restructuring strategies:
- **Split across arcs**: Each arc handles one CONSTRAINED input.
  Verified independently.
- **Reduce input types**: Use `bool` or small `Enum` instead of
  `Enum` with many values.
- **Move unbounded inputs out of conditions**: Use CONSTRAINED
  strings only for content (email body, log messages), not for
  branching.

The planner arc receives a clear error message explaining why
verification failed and suggesting restructuring approaches.

---

## Per-Arc Verification and Composition

### Each Arc Verified Independently

When a planner creates multiple child arcs that each use CONSTRAINED
data for branching, each arc's code is verified independently:

```
Parent PLANNER (TRUSTED context):
  │
  ├── Child A: code reads C input from arc X → verified independently
  ├── Child B: code reads C input from arc Y → verified independently
  └── Child C: code reads C input from arcs X and Y → verified independently
```

If Child C reads CONSTRAINED state from another child arc, that state
is treated as a CONSTRAINED input to enumerate during verification.
The verification doesn't need to know how the other arc computed the
state — it just varies it over its declared schema.

### Cross-Arc Dependencies

If the verification threshold would be exceeded by considering all
cross-arc CONSTRAINED inputs together, the platform rejects and asks
the planner to split further.  This is the recursion solution: the
planner decomposes complex logic into simpler arcs, each independently
verifiable.

The platform guarantees: if each arc is independently verified, and
arcs communicate only through typed state (Pydantic schemas), the
composed workflow preserves the security properties.  This is modular
verification — the same principle used in type-safe module systems.

---

## Comparison to CaMeL's Interpreter

CaMeL achieves similar goals through a custom AST-walking interpreter
(~2400 lines) that reimplements Python evaluation with value-level
taint tracking.

Our approach differs in three ways:

| Aspect | CaMeL | Verified Flow Analysis |
|--------|-------|----------------------|
| **Mechanism** | Custom interpreter replaces Python | Tracked wrappers participate in native Python evaluation |
| **When** | Every execution (runtime enforcement) | Verification time only (then hash-and-trust) |
| **Scope** | All code | Only code that uses CONSTRAINED data in conditions |
| **Labels** | Sources + readers + dependency chains | Three labels: T, C, U |
| **Complexity** | ~2400 lines (full Python subset reimplementation) | ~750 lines estimated (wrappers + AST checker + verifier) |

The key simplification: we don't reimplement Python evaluation.  We
use Python's own dunder protocol (`__bool__`, `__eq__`, `__contains__`,
etc.) to carry labels through native evaluation.  This works because
our whitelisted subset is restricted enough that Python's native
evaluation is predictable and trackable.

CaMeL's approach is necessary when you need runtime enforcement on
every execution.  Our hash-and-trust model means verification runs
once; subsequent executions just check the hash.

---

## Implementation Estimate

| Component | Estimated size | Dependencies |
|-----------|---------------|-------------|
| AST whitelist checker | ~300 lines | `ast` (stdlib) |
| Static taint propagation | ~300 lines | `ast` (stdlib) |
| Tracked value wrappers (T/C/U) | ~400 lines | None |
| Policy-typed literal classes | ~200 lines | Pydantic |
| Dry-run executor + mock tools | ~200 lines | Existing tool backends |
| Hash registry + trust store | ~150 lines | Existing DB |
| Integration with review pipeline | ~150 lines | Existing code_manager |
| **Total** | **~1700 lines** | |

This does not include security policy configuration UI or the template
development skill — those are separate features built on top.

---

## Open Questions

### What is the right verification threshold?

1024 combinations is arbitrary.  The threshold should be based on
acceptable verification time, which depends on how fast the dry-run
executor is.  If each combination takes ~1ms (reasonable for mocked
tool calls), 1024 combinations = ~1 second.  10,000 combinations =
~10 seconds.  The threshold should be configurable.

### Should verified code be re-verified when policies change?

If the user adds an address to the email allowlist, previously
verified code that uses `Email(...)` literals might now validate
differently.  Options:
- Invalidate all verified hashes when any policy changes (safe, blunt)
- Track which policies each hash depends on and invalidate selectively
  (precise, more complex)

### How do we handle `for` loops over CONSTRAINED lists?

A `for` loop over a list with `max_length=10` and element type
`Enum[3]` has 3^10 = 59,049 combinations.  This exceeds a reasonable
threshold.  Options:
- Verify the loop body independently for each element value (3
  combinations), ignoring interaction between iterations
- Require the planner to split: one arc per list element
- Allow loop body verification if iterations are independent
  (no cross-iteration state)

### Can we extend to UNTRUSTED data?

Currently, only CONSTRAINED data (extracted through schema) can
potentially cross the boundary.  UNTRUSTED data cannot — it must
go through extraction first.  This is intentional: raw untrusted
data has no schema, so we can't enumerate its input space.  The
extraction step (U → C) is what makes verification possible.

---

## Follow-up: Remove Structural Code Auto-Approve

Once verified flow analysis is implemented, remove the `_is_structural_code`
auto-approve bypass in `review/pipeline.py`. This was added as a pragmatic
fix to avoid slow/flaky LLM reviews of trivially-safe arc+messaging code
(the bottleneck that caused S002 acceptance test timeouts). It is not
intended as a permanent security design — all submitted code should go
through LLM review, even structural workflow code. The verified flow
analysis hash-and-trust mechanism is the principled replacement.
