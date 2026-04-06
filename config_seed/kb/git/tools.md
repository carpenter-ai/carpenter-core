# Git Tools

Exact function signatures for git operations. Use these — do NOT guess parameter names.

## CRITICAL: Never hardcode Forgejo URLs

NEVER hardcode Forgejo URLs (like git.harack.us, git.magi.systems, etc.) in code OR in arc goals. ALWAYS read the URL from config at runtime:
```python
from carpenter_tools.read import config
server_url = config.get_value('git_server_url')['value']
```

When creating child arcs for git workflows, do NOT put URLs in the goal. Instead write:
- "Clone the repository owner/repo using config.get_value('git_server_url') and git.setup_repo()"

## Function signatures

### git.setup_repo(repo_url, workspace, fork_url, branch=None)
Clone a repository. All three positional args required:
```python
from carpenter_tools.act import git
from carpenter_tools.read import config
server_url = config.get_value('git_server_url')['value']
workspace = '/home/pi/carpenter/data/workspaces/my-task'
git.setup_repo(
    repo_url=f'{server_url}/owner/repo.git',
    workspace=workspace,
    fork_url=f'{server_url}/owner/repo.git'
)
```

### git.create_branch(workspace, branch_name)
```python
git.create_branch(workspace=workspace, branch_name='feature-branch')
```

### git.commit_and_push(workspace, branch_name, commit_message, files=None)
```python
git.commit_and_push(
    workspace=workspace,
    branch_name='feature-branch',
    commit_message='Add new file'
)
```

### git.create_pr(repo_owner, repo_name, branch_name, pr_title, pr_body=None, fork_user=None)
All four positional args required:
```python
git.create_pr(
    repo_owner='ben-harack',
    repo_name='my-repo',
    branch_name='feature-branch',
    pr_title='Add new file',
    pr_body='Description here'
)
```

### files.write(path, content)
Always use absolute paths:
```python
from carpenter_tools.act import files
files.write(path=f'{workspace}/filename.md', content='File content here')
```

### Cross-arc state
Store state for child arcs to read:
```python
from carpenter_tools.act import state as state_act
state_act.set(key='workspace_path', value=workspace)
```
Read state from a specific arc:
```python
from carpenter_tools.read import state
workspace = state.get('workspace_path', arc_id=parent_arc_id)
```

## Complete git workflow (single arc)
For simple file additions, do everything in ONE submit_code call:
```python
from carpenter_tools.act import git, files
from carpenter_tools.read import config
server_url = config.get_value('git_server_url')['value']
workspace = '/home/pi/carpenter/data/workspaces/my-task'
git.setup_repo(
    repo_url=f'{server_url}/owner/repo.git',
    workspace=workspace,
    fork_url=f'{server_url}/owner/repo.git'
)
git.create_branch(workspace=workspace, branch_name='add-file')
files.write(path=f'{workspace}/myfile.md', content='# Title\nContent')
git.commit_and_push(workspace=workspace, branch_name='add-file', commit_message='Add myfile.md')
git.create_pr(repo_owner='owner', repo_name='repo', branch_name='add-file', pr_title='Add myfile.md')
```

## Related
[[git/pr-review]] · [[arcs/planning]] · [[credentials/intake]]
