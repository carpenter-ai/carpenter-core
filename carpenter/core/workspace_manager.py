"""Git-backed workspace manager for coding agents.

Creates isolated workspaces from source directories, tracks changes via git,
and applies approved changes back to the source.

Uses dulwich (pure Python git library) instead of shelling out to git CLI.
"""

import io
import logging
import os
import shutil
import tempfile
import time

import dulwich.porcelain as porcelain
from dulwich.repo import Repo

from ..db import get_db, db_connection

logger = logging.getLogger(__name__)

# Author/committer identity used for workspace git operations.
_GIT_IDENTITY = b"Carpenter <carpenter@localhost>"

# Default README content for workspaces root directory
_WORKSPACES_README = """\
Carpenter Coding Workspaces
============================

This directory contains temporary git workspaces created by Carpenter's coding agent.
Each workspace is a snapshot of source code where the agent makes changes.

DO NOT manually modify or delete directories here.
These workspaces are managed by Carpenter and will be cleaned up automatically
when their associated arcs complete.

Directory naming: {label}_{timestamp}
- The label contains the arc ID for database lookup
- Timestamp is Unix epoch when created

To check workspace status, query Carpenter's database by arc ID.

For more information, see: https://carpenter-ai.org/docs/
"""


def _stage_all(workspace: str) -> None:
    """Stage all changes (adds, modifications, and deletions) in a workspace.

    Equivalent to ``git add -A``.  dulwich's ``porcelain.add()`` with no
    paths argument handles new files, modified files, and deleted files by
    updating the index to match the working tree.
    """
    porcelain.add(workspace)


def _get_staged_diff(workspace: str) -> str:
    """Return the staged diff as a unified diff string."""
    buf = io.BytesIO()
    porcelain.diff(workspace, staged=True, outstream=buf)
    return buf.getvalue().decode("utf-8", errors="replace")


def _get_staged_changed_files(workspace: str) -> list[str]:
    """Return list of staged changed file paths (relative)."""
    st = porcelain.status(workspace)
    changed = []
    for key in ("add", "modify", "delete"):
        for f in st.staged[key]:
            path = f.decode() if isinstance(f, bytes) else f
            changed.append(path)
    return changed


def _ensure_workspaces_readme(workspaces_dir: str) -> None:
    """Create README.txt in workspaces root if it doesn't exist.

    This ensures new Carpenter installs have documentation explaining
    what the workspaces directory contains and warning against manual
    modification.
    """
    readme_path = os.path.join(workspaces_dir, "README.txt")
    if not os.path.exists(readme_path):
        try:
            with open(readme_path, "w") as f:
                f.write(_WORKSPACES_README)
            logger.info("Created workspaces README at %s", readme_path)
        except OSError as e:
            # Non-fatal - workspace creation can continue
            logger.warning("Failed to create workspaces README: %s", e)


def create_workspace(source_dir: str, label: str) -> tuple[str, str | None]:
    """Clone source_dir into a new git-backed workspace.

    1. Captures HEAD SHA of source_dir (if it's a git repo)
    2. Creates dir under workspaces_dir: {workspaces_dir}/{label}_{timestamp}/
    3. Copies all files from source_dir (preserving structure)
    4. git init + initial commit

    Returns (workspace_path, base_sha) where base_sha is the HEAD of
    source_dir at snapshot time (None if source_dir is not a git repo).

    Raises:
        FileNotFoundError: If source_dir does not exist.
        OSError: On filesystem errors.
    """
    from .. import config

    if not os.path.isdir(source_dir):
        raise FileNotFoundError(f"Source directory not found: {source_dir}")

    # Capture base SHA before copying
    base_sha = None
    try:
        r = Repo(source_dir)
        base_sha = r.head().decode()
    except Exception:
        pass  # Not a git repo or no HEAD

    workspaces_dir = config.CONFIG.get(
        "workspaces_dir",
        os.path.join(tempfile.gettempdir(), "tc-workspaces"),
    )
    os.makedirs(workspaces_dir, exist_ok=True)
    _ensure_workspaces_readme(workspaces_dir)

    timestamp = int(time.time())
    safe_label = label.replace("/", "_").replace(" ", "_")
    workspace_name = f"{safe_label}_{timestamp}"
    workspace_path = os.path.join(workspaces_dir, workspace_name)

    # Copy source into workspace, excluding bytecode caches and secrets
    shutil.copytree(
        source_dir, workspace_path, dirs_exist_ok=False,
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "*.pyo",
            ".env", ".env.*", ".env.local", ".env.production",
            "*.key", "*.pem", "*.crt", "*.p12", "*.pfx",
            "secrets", "credentials", ".credentials",
            "*.secret", "*.cred", "*.token",
        ),
    )

    # Remove any existing .git (we create a fresh one)
    existing_git = os.path.join(workspace_path, ".git")
    if os.path.isdir(existing_git):
        shutil.rmtree(existing_git)

    # Add .gitignore to filter common artifacts and secrets
    gitignore_path = os.path.join(workspace_path, ".gitignore")
    if not os.path.exists(gitignore_path):
        with open(gitignore_path, "w") as f:
            f.write(
                "__pycache__/\n*.pyc\n*.pyo\n.mypy_cache/\n.pytest_cache/\n"
                ".env\n.env.*\n*.key\n*.pem\n*.crt\n"
                "secrets/\ncredentials/\n*.secret\n*.cred\n*.token\n"
            )

    # Initialize git repo using dulwich
    porcelain.init(workspace_path)
    porcelain.add(workspace_path)
    porcelain.commit(
        workspace_path,
        message=b"initial state",
        author=_GIT_IDENTITY,
        committer=_GIT_IDENTITY,
    )

    logger.info("Created workspace: %s (from %s, base_sha=%s)", workspace_path, source_dir, base_sha and base_sha[:12])
    return workspace_path, base_sha


def get_diff(workspace_path: str) -> str:
    """Return unified diff of all changes since initial commit.

    Shows both staged and unstaged changes relative to HEAD.
    """
    # Stage everything first so we can diff against HEAD
    _stage_all(workspace_path)
    return _get_staged_diff(workspace_path)


def get_changed_files(workspace_path: str) -> list[str]:
    """List files that changed since initial commit."""
    _stage_all(workspace_path)
    return _get_staged_changed_files(workspace_path)


def apply_to_source(workspace_path: str, source_dir: str) -> list[str]:
    """Apply workspace changes to the source directory.

    For git-backed source directories, copies changed files from workspace
    to source.  Uses file-copy approach which is safe when there are no
    concurrent modifications to the same files.

    For merge-safe application (handling concurrent modifications), use
    ``apply_to_source_via_merge()`` instead.

    Returns list of files applied.
    Raises RuntimeError if the application fails.
    """
    changed = get_changed_files(workspace_path)
    if not changed:
        return []

    diff_text = get_diff(workspace_path)
    if not diff_text.strip():
        return []

    # Check if source is a git repo
    source_is_git = os.path.isdir(os.path.join(source_dir, ".git"))
    if source_is_git:
        try:
            return _apply_via_file_copy(workspace_path, source_dir, changed)
        except Exception as e:
            raise RuntimeError(
                f"Patch failed to apply cleanly — source may have diverged "
                f"in conflicting ways.\n{e}"
            )

    # Fallback: direct file copy for non-git sources
    return _apply_by_copy(workspace_path, source_dir, changed)


def _apply_via_file_copy(
    workspace_path: str, source_dir: str, changed: list[str],
) -> list[str]:
    """Apply changes to source dir by copying files from workspace.

    For git-backed sources, this simply copies the changed files over.
    """
    applied = []
    for rel_path in changed:
        src = os.path.join(workspace_path, rel_path)
        dst = os.path.join(source_dir, rel_path)
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            applied.append(rel_path)
        elif not os.path.exists(src) and os.path.exists(dst):
            os.remove(dst)
            applied.append(rel_path)

    logger.info("Applied patch (%d files) to %s", len(applied), source_dir)
    return applied


class MergeConflictError(RuntimeError):
    """Raised when a git merge fails due to conflicts."""

    def __init__(self, message: str, conflicting_files: list[str] | None = None, conflict_diff: str = ""):
        super().__init__(message)
        self.conflicting_files = conflicting_files or []
        self.conflict_diff = conflict_diff


def _get_current_branch(source_dir: str) -> str:
    """Get the current branch name from the git repository.
    
    Args:
        source_dir: Path to the git repository.
        
    Returns:
        The current branch name.
        
    Raises:
        RuntimeError: If unable to determine current branch.
    """
    r = Repo(source_dir)
    try:
        head_symrefs = r.refs.get_symrefs()
        head_ref = head_symrefs[b"HEAD"]
        return head_ref.split(b"/")[-1].decode()
    except (KeyError, IndexError):
        raise RuntimeError("Failed to get current branch in source repo")


def _create_temp_branch_and_apply_changes(
    source_dir: str,
    workspace_path: str,
    changed: list[str],
    temp_branch: str,
    base_sha: str,
    arc_id: int | str,
) -> None:
    """Create temporary branch, apply workspace changes, and commit.
    
    Args:
        source_dir: Path to the git repository.
        workspace_path: Path to the workspace with changes.
        changed: List of changed file paths.
        temp_branch: Name of the temporary branch to create.
        base_sha: The commit SHA to branch from.
        arc_id: Arc identifier for commit message.
    """
    # Create temp branch at the snapshot point
    porcelain.branch_create(source_dir, temp_branch, objectish=base_sha.encode())
    porcelain.checkout(source_dir, target=temp_branch.encode())

    # Copy workspace changed files onto the temp branch
    for rel_path in changed:
        src = os.path.join(workspace_path, rel_path)
        dst = os.path.join(source_dir, rel_path)
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        elif not os.path.exists(src) and os.path.exists(dst):
            os.remove(dst)

    # Stage and commit on temp branch
    _stage_all(source_dir)
    porcelain.commit(
        source_dir,
        message=f"Platform change (arc {arc_id})".encode(),
        author=_GIT_IDENTITY,
        committer=_GIT_IDENTITY,
    )


def _merge_temp_branch(
    source_dir: str,
    temp_branch: str,
    original_branch: str,
    arc_id: int | str,
) -> tuple[bytes, list]:
    """Merge temporary branch into original branch.
    
    Args:
        source_dir: Path to the git repository.
        temp_branch: Name of the temporary branch to merge.
        original_branch: Name of the branch to merge into.
        arc_id: Arc identifier for merge message.
        
    Returns:
        Tuple of (merge_sha, conflicts list).
    """
    # Switch back to original branch
    porcelain.checkout(source_dir, target=original_branch.encode())

    # Merge the temp branch
    return porcelain.merge(
        source_dir,
        temp_branch.encode(),
        no_ff=True,
        message=f"Merge platform change (arc {arc_id})".encode(),
        author=_GIT_IDENTITY,
        committer=_GIT_IDENTITY,
    )


def _handle_merge_conflict(
    source_dir: str,
    temp_branch: str,
    original_branch: str,
    conflicts: list,
) -> None:
    """Handle merge conflicts by capturing details and cleaning up.
    
    Args:
        source_dir: Path to the git repository.
        temp_branch: Name of the temporary branch.
        original_branch: Name of the original branch.
        conflicts: List of conflicting files from merge.
        
    Raises:
        MergeConflictError: Always raised with conflict details.
    """
    conflict_files = [c.decode() if isinstance(c, bytes) else c for c in conflicts]

    # Get conflict diff
    conflict_diff = ""
    try:
        buf = io.BytesIO()
        porcelain.diff(source_dir, outstream=buf)
        conflict_diff = buf.getvalue().decode("utf-8", errors="replace")
    except Exception:
        logger.debug("Failed to capture conflict diff", exc_info=True)

    # Abort: reset to original branch state
    porcelain.reset(source_dir, "hard", f"refs/heads/{original_branch}")
    porcelain.checkout(source_dir, target=original_branch.encode(), force=True)

    # Delete temp branch
    _cleanup_temp_branch(source_dir, temp_branch)

    raise MergeConflictError(
        f"Merge conflict in {len(conflict_files)} file(s): {', '.join(conflict_files)}",
        conflicting_files=conflict_files,
        conflict_diff=conflict_diff,
    )


def _cleanup_temp_branch(source_dir: str, temp_branch: str) -> None:
    """Delete temporary branch, logging but ignoring errors.
    
    Args:
        source_dir: Path to the git repository.
        temp_branch: Name of the temporary branch to delete.
    """
    try:
        porcelain.branch_delete(source_dir, temp_branch)
    except Exception:
        logger.debug("Failed to delete temp branch during cleanup", exc_info=True)


def _cleanup_on_error(source_dir: str, temp_branch: str, original_branch: str) -> None:
    """Restore repository state after an error.
    
    Args:
        source_dir: Path to the git repository.
        temp_branch: Name of the temporary branch to delete.
        original_branch: Name of the original branch to restore.
    """
    try:
        porcelain.checkout(source_dir, target=original_branch.encode(), force=True)
    except Exception:
        logger.debug("Failed to checkout original branch during error cleanup", exc_info=True)
    
    _cleanup_temp_branch(source_dir, temp_branch)


def apply_to_source_via_merge(
    workspace_path: str, source_dir: str, base_sha: str, arc_id: int | str = "",
) -> list[str]:
    """Apply workspace changes to source via a proper git merge.

    Creates a temp branch at base_sha, copies workspace files onto it,
    commits, then merges into the current branch.  This gives proper
    3-way merge semantics and clean conflict detection.

    Args:
        workspace_path: Path to the workspace with changes.
        source_dir: The target git repo to merge into.
        base_sha: The commit SHA that source_dir was at when workspace was created.
        arc_id: Arc identifier for branch naming.

    Returns list of changed files on success.
    Raises MergeConflictError on conflict, RuntimeError on other failures.
    """
    changed = get_changed_files(workspace_path)
    if not changed:
        return []

    diff_text = get_diff(workspace_path)
    if not diff_text.strip():
        return []

    original_branch = _get_current_branch(source_dir)
    temp_branch = f"_carpenter_merge_{arc_id}"

    try:
        _create_temp_branch_and_apply_changes(
            source_dir, workspace_path, changed, temp_branch, base_sha, arc_id
        )
        
        merge_sha, conflicts = _merge_temp_branch(
            source_dir, temp_branch, original_branch, arc_id
        )

        if not conflicts:
            # Success -- clean up temp branch
            porcelain.branch_delete(source_dir, temp_branch)
            logger.info("Merged %d files via branch for arc %s", len(changed), arc_id)
            return changed
        else:
            # Merge conflict -- capture details and clean up
            _handle_merge_conflict(source_dir, temp_branch, original_branch, conflicts)

    except MergeConflictError:
        raise  # Re-raise merge conflicts as-is
    except Exception as e:  # broad catch: multiple dulwich calls
        # Clean up on any other failure
        _cleanup_on_error(source_dir, temp_branch, original_branch)
        if isinstance(e, RuntimeError):
            raise
        raise RuntimeError(f"Merge-based apply failed: {e}") from e


def _apply_by_copy(
    workspace_path: str, source_dir: str, changed: list[str],
) -> list[str]:
    """Legacy file-copy apply (fallback when source is not a git repo)."""
    return _apply_via_file_copy(workspace_path, source_dir, changed)


def has_active_changeset(source_dir: str, exclude_arc_id: int | None = None) -> bool:
    """Check if there's already an active coding-change arc for this source.

    Queries the DB for arcs with name starting with 'coding-change' that are
    in active/waiting status and whose goal contains the source_dir.

    Args:
        source_dir: The source directory to check.
        exclude_arc_id: An arc ID to exclude from the check (e.g. self).
    """
    with db_connection() as db:
        from .arcs import CODING_CHANGE_PREFIX
        if exclude_arc_id is not None:
            rows = db.execute(
                "SELECT id FROM arcs "
                f"WHERE name LIKE '{CODING_CHANGE_PREFIX}%' "
                "AND status IN ('active', 'waiting', 'pending') "
                "AND goal LIKE ? "
                "AND id != ?",
                (f"%{source_dir}%", exclude_arc_id),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id FROM arcs "
                f"WHERE name LIKE '{CODING_CHANGE_PREFIX}%' "
                "AND status IN ('active', 'waiting', 'pending') "
                "AND goal LIKE ?",
                (f"%{source_dir}%",),
            ).fetchall()
        return len(rows) > 0


def cleanup_workspace(workspace_path: str) -> None:
    """Remove workspace directory."""
    if os.path.isdir(workspace_path):
        import traceback
        caller = "".join(traceback.format_stack()[-3:-1])
        shutil.rmtree(workspace_path)
        logger.info("Cleaned up workspace: %s\nCalled by:\n%s", workspace_path, caller)
