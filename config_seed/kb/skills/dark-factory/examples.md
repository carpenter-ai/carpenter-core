# Dark Factory Example: Word Frequency Counter

## Setup

User request: "Build a word frequency counter that handles punctuation and case."

## Step 1: Spec Refinement

The CHAT agent refines the request into a structured spec:

```
DevelopmentSpec:
  description: "Word frequency counter"
  requirements:
    - Accept string input
    - Return dict of word -> count
    - Handle punctuation and case-insensitivity
  acceptance_criteria:
    - Counts words in simple sentences
    - Handles mixed case (Hello == hello)
    - Strips punctuation before counting
  constraints:
    - No external dependencies
  language: python
```

Stored as `state.set_typed("development_spec", spec)` on the spec-refinement arc.

## Step 2: Scenario Generation

The root PLANNER reads the spec from spec-refinement:
```
spec_data = state.get("development_spec", arc_id=spec_arc_id)
```

Then activates scenario-generation with the spec context. The EXECUTOR generates:

```
TestSuite:
  scenarios:  # Visible to implementation
    - basic_count: "hello world hello" -> {"hello": 2, "world": 1}
    - case_insensitive: "Hello hello HELLO" -> {"hello": 3}
    - with_punctuation: "hello, world! hello." -> {"hello": 2, "world": 1}
  holdout_scenarios:  # Hidden until completion gate
    - empty_string: "" -> {}
    - numbers_mixed: "test 123 test" -> {"test": 2, "123": 1}
```

## Step 3: Implementation Loop

### Iteration 1

The implementation-loop PLANNER creates:
- `impl-1`: Produces naive `text.split()` counter
- `validate-1`: Runs scenarios, gets pass_rate=0.33 (only basic_count passes)

PLANNER reads result: `state.get("validation_result", arc_id=validate_1_id)`

Decision: **continue** (0.33 < 0.95 threshold)

Feedback generated:
- "Case handling: need .lower() before counting"
- "Punctuation: need to strip before splitting"

### Iteration 2

PLANNER creates:
- `impl-2`: Adds `.lower()` and `str.translate()` for punctuation
- `validate-2`: All scenarios pass, pass_rate=1.0

Decision: **done** (1.0 >= 0.95 threshold)

## Step 4: Completion Gate

Root PLANNER marks implementation-loop as completed, signals completion-gate.

The JUDGE runs holdout scenarios (empty_string, numbers_mixed) against the implementation.

Result: Both holdout scenarios pass (pass_rate=1.0).

Final output:
```
DarkFactoryResult:
  status: "success"
  iterations_used: 2
  final_pass_rate: 1.0
  holdout_pass_rate: 1.0
```

## Arc Tree Summary

```
dark-factory-run (PLANNER, completed)
|-- spec-refinement (CHAT, completed)
|-- scenario-generation (EXECUTOR, completed)
|-- implementation-loop (PLANNER, completed, mutable)
|   |-- impl-1 (EXECUTOR, completed)
|   |-- validate-1 (EXECUTOR, completed)
|   |-- impl-2 (EXECUTOR, completed)
|   +-- validate-2 (EXECUTOR, completed)
+-- completion-gate (JUDGE, completed)
```

Total: 8 arcs, 2 iterations, 2 API-calling agents per iteration.
