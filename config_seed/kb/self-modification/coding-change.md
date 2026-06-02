# Coding Change Workflow

Modify source code through a structured workflow with human review.

## How to use
```python
from carpenter_tools.act import arc

# Modify platform source (config_seed, carpenter_tools, etc.)
arc_id = arc.invoke_coding_change(
    source_dir="platform",
    prompt="Describe the changes needed: add/modify/remove X, Y, Z"
)

# Modify an external repository
arc_id = arc.invoke_coding_change(
    source_dir="/path/to/repo",
    prompt="Describe the changes needed"
)
```

## source_dir values
- `"platform"` (default) — targets the platform server directory (config_seed/, carpenter_tools/, etc.)
- Absolute path — targets any local git repository

## `affected_paths` (optional)
When you already know which files the change touches (e.g. the user said
"edit `config_seed/templates/foo.yaml`" or "update the `kb/notes/x.md`
article"), pass them as a list:

```python
arc_id = arc.invoke_coding_change(
    source_dir="platform",
    prompt="Fix the typo in the heading",
    affected_paths=["config_seed/kb/notes/x.md"],
)
```

This routes the arc through a workflow specialized for the file kind:
- YAML-only changes use the `yaml-change` workflow (deterministic YAML
  lint instead of test execution).
- KB-only changes use the `kb-change` workflow (deterministic KB-format
  check instead of test execution).
- Anything else (or mixed) uses the default `coding-change` workflow.

Tier-1 paths (platform source) additionally force human review.

**Do not guess paths the coding agent will discover.** If you do not
know which files the change will touch, leave `affected_paths` unset.
The platform still applies a tier-1 safety gate at review time, so
omitting the list is safe — passing it earlier just selects a more
specialized workflow.

## What happens
1. The coding agent runs in an isolated workspace (copy of source_dir)
2. It generates a diff for the requested changes
3. The diff goes through human review
4. On approval, changes are applied to the source directory

## Notes
- The workflow is asynchronous -- the user is notified when the diff is ready
- Changes are applied via file copy with conflict detection
- For complex tasks requiring stronger reasoning, use the `escalate_current_arc` tool

## Related
[[security/review-pipeline]] -- [[arcs/tools]] -- [[self-modification/ad-hoc-review]]
