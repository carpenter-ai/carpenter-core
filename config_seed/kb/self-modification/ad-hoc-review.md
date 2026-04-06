# Ad-Hoc AI Code Review

Request an AI review of a coding-change diff using a specific model. Useful for getting a second opinion from Sonnet, Opus, or Haiku before approving or rejecting changes. Also referred to as model-specific review or requesting a review with a different model.

## How to use

```python
from carpenter_tools.act import arc

# Request Sonnet to review a pending coding-change
reviewer_arc_id = arc.request_ai_review(
    target_arc_id=42,
    model="sonnet",
    focus_areas="security, error handling",
)
```

The target arc must be a coding-change arc in `waiting` status (i.e., it has a diff ready for human review).

## What happens

1. A REVIEWER arc is created as a child of the coding-change root.
2. The reviewer examines the workspace and diff using the specified model.
3. Findings are stored as structured data in the reviewer's arc_state under `review_findings`.
4. The human sees findings on the review page and makes the final decision.

## Findings format

```json
{
  "summary": "Brief summary of the review",
  "issues": ["Issue 1", "Issue 2"],
  "recommendations": ["Recommendation 1"],
  "verdict": "approve"
}
```

Verdict is either `"approve"` or `"concerns"`.

## Constraints

- **Informational only** -- the AI review does not trigger automated acceptance or rejection. The human always makes the final call.
- The reviewer arc uses `arc_role="worker"` (not `verifier`), keeping it outside the judge pipeline.
- Multiple AI reviews can be requested for the same coding-change.

## Also available via UI

The review page includes a "Request AI Review" button with a model selector dropdown. This calls the same backend logic.

## Related

[[self-modification/coding-change]] -- The coding-change workflow that produces diffs for review.
[[arcs/planning]] -- How arcs are structured and dispatched.
