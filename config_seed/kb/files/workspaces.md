# Workspaces

Each arc can have an isolated disk workspace for file operations.

## How it works
- Workspaces are created under `workspaces_dir` config path
- Each workspace is a directory named after the arc
- Files written in submitted code go to the workspace
- Workspace path is stored in arc state as `workspace_path`

## Retention
- `workspace_retention_days` — Days to keep completed workspaces (default: 14)
- `workspace_retention_count` — Max workspaces to retain (default: 100)

## Related
[[files/tools]] · [[arcs/state-tools]]
