# Git Workflow

Best practices for git-based development workflows within Carpenter arcs and coding agents.

## Atomic Bisectable Commits

Every commit should be a single logical change that passes all tests independently.

**Separate concerns into distinct commits:**
- Renames separate from rewrites (rename first, then modify)
- Test infrastructure separate from test implementations
- Dependency changes separate from code that uses them
- Formatting/style changes separate from behavioral changes
- Schema migrations separate from code that uses new schema

**Why this matters:**
- `git bisect` can pinpoint regressions to exact changes
- Reverts are clean — one commit = one revertable unit
- Code review is tractable: each commit has a clear narrative

## Branch Discipline

- Branch from the latest main/default branch
- One feature/fix per branch — don't bundle unrelated changes
- Rebase onto main before creating a PR (linear history)
- Delete branches after merge

## Commit Messages

Use imperative mood in the subject line: "Add X", "Fix Y", "Remove Z" — not "Added", "Fixes", "Removing".

Structure:
```
Short summary (50 chars or less)

Optional body explaining WHY, not WHAT. The diff shows what changed;
the message explains the motivation. Wrap at 72 characters.
```

## Diff-Aware Review

When reviewing changes:
1. Read the diff first to understand scope
2. Check that each commit is atomic (one logical change)
3. Verify no unrelated changes smuggled in
4. Confirm test coverage for changed code paths

## Coding-Change Integration

When a coding-change arc runs the built-in coding agent:
- The agent works in an isolated git workspace
- Changes are captured via `git diff`
- The diff goes through the review pipeline before merging

For multi-step changes, prefer multiple coding-change arcs (each producing one atomic diff) over a single arc with a massive diff.
