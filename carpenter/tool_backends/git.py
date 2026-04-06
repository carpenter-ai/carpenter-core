"""Git tool backend — pure-git operations (clone, branch, commit, push).

This is a Tier 1 (callback) backend: credentials stay on the platform side.
Executors invoke these handlers via HTTP POST callbacks.

Uses dulwich (pure Python git library) instead of shelling out to git CLI.
Forgejo-specific API handlers (PRs, webhooks, reviews) live in forgejo_api.py.
"""
import io
import logging
import os

import dulwich.porcelain as porcelain
from dulwich.repo import Repo

from .. import config

logger = logging.getLogger(__name__)

# Defaults kept as module-level fallbacks; runtime values come from config.
_DEFAULT_GIT_API_TIMEOUT = 30.0
_DEFAULT_GIT_API_LONG_TIMEOUT = 60.0

# Author/committer identity used for automated commits.
_GIT_IDENTITY = b"Carpenter <carpenter@localhost>"


def _git_api_timeout() -> float:
    """Return the default git server API HTTP timeout (seconds)."""
    return config.get_config("git_api_timeout", _DEFAULT_GIT_API_TIMEOUT)


def _git_api_long_timeout() -> float:
    """Return the git server API timeout for large responses (seconds)."""
    return config.get_config("git_api_long_timeout", _DEFAULT_GIT_API_LONG_TIMEOUT)


def _git_server_url() -> str:
    """Return the base git server API URL."""
    base = config.CONFIG.get("git_server_url", "")
    return base.rstrip("/") + "/api/v1"


def _git_token() -> str:
    """Return the git server API token from config."""
    return config.CONFIG.get("git_token", "")


def _git_server_headers() -> dict:
    """Return common headers for git server API requests."""
    return {
        "Authorization": f"token {_git_token()}",
        "Content-Type": "application/json",
    }


def _remote_exists(repo_path: str, name: str) -> bool:
    """Check whether a named remote exists in the repo config."""
    r = Repo(repo_path)
    cfg = r.get_config()
    try:
        cfg.get((b"remote", name.encode()), b"url")
        return True
    except KeyError:
        return False


def _set_remote_url(repo_path: str, name: str, url: str) -> None:
    """Set the URL for a named remote (create or update)."""
    r = Repo(repo_path)
    cfg = r.get_config()
    cfg.set((b"remote", name.encode()), b"url", url.encode())
    cfg.write_to_path()


def _get_remotes_info(repo_path: str) -> str:
    """Return a string listing all remotes and their URLs (like git remote -v)."""
    r = Repo(repo_path)
    cfg = r.get_config()
    lines = []
    for section_key in cfg.sections():
        if len(section_key) == 2 and section_key[0] == b"remote":
            name = section_key[1].decode()
            url = cfg.get(section_key, b"url").decode()
            lines.append(f"{name}\t{url} (fetch)")
            lines.append(f"{name}\t{url} (push)")
    return "\n".join(lines)


def _branch_exists(repo_path: str, branch_name: str) -> bool:
    """Check whether a local branch exists."""
    r = Repo(repo_path)
    ref = f"refs/heads/{branch_name}".encode()
    return ref in r.refs


def _run_dulwich(func, *args, **kwargs):
    """Run a dulwich porcelain function, suppressing stderr output."""
    # Dulwich writes progress to stderr by default; redirect to devnull.
    null = io.BytesIO()
    try:
        if hasattr(func, "__code__") and "errstream" in func.__code__.co_varnames:
            kwargs.setdefault("errstream", null)
    except (AttributeError, TypeError):
        pass
    return func(*args, **kwargs)


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------


def handle_setup_repo(params: dict) -> dict:
    """Clone and configure repository with fork-based workflow.

    params: {repo_url, workspace, fork_url, branch (opt)}
    Returns: {success, workspace_path, remotes}
    """
    repo_url = params["repo_url"]
    workspace = params["workspace"]
    fork_url = params["fork_url"]
    branch = params.get("branch")

    git_dir = os.path.join(workspace, ".git")

    if os.path.isdir(git_dir):
        # Reconfigure remotes on existing repo
        logger.info("Reconfiguring remotes in existing repo: %s", workspace)

        # Set upstream
        if _remote_exists(workspace, "upstream"):
            _set_remote_url(workspace, "upstream", repo_url)
        else:
            porcelain.remote_add(workspace, "upstream", repo_url)

        # Set origin (fork)
        if _remote_exists(workspace, "origin"):
            _set_remote_url(workspace, "origin", fork_url)
        else:
            porcelain.remote_add(workspace, "origin", fork_url)
    else:
        # Fresh clone
        logger.info("Cloning %s to %s", repo_url, workspace)
        try:
            _run_dulwich(porcelain.clone, repo_url, workspace)
        except Exception as e:
            return {"success": False, "error": str(e)}

        # Rename origin -> upstream: copy URL, remove origin, add upstream
        r = Repo(workspace)
        cfg = r.get_config()
        try:
            origin_url = cfg.get((b"remote", b"origin"), b"url")
        except KeyError:
            origin_url = repo_url.encode()
        porcelain.remote_remove(r, "origin")
        porcelain.remote_add(workspace, "upstream", origin_url.decode())
        porcelain.remote_add(workspace, "origin", fork_url)

    # Checkout branch if specified
    if branch:
        try:
            porcelain.checkout(workspace, target=branch.encode())
        except Exception:
            logger.warning("Could not checkout branch %s", branch)

    # Collect remote info
    remotes = _get_remotes_info(workspace)

    return {
        "success": True,
        "workspace_path": workspace,
        "remotes": remotes,
    }


def handle_create_branch(params: dict) -> dict:
    """Create or switch to a feature branch.

    params: {workspace, branch_name}
    Returns: {branch_name, created: bool}
    """
    workspace = params["workspace"]
    branch_name = params["branch_name"]

    # Fetch upstream
    try:
        _run_dulwich(porcelain.fetch, workspace, "upstream")
    except Exception as e:
        logger.warning("Failed to fetch upstream: %s", e)

    if _branch_exists(workspace, branch_name):
        # Branch exists, just check it out
        porcelain.checkout(workspace, target=branch_name.encode())
        return {"branch_name": branch_name, "created": False}
    else:
        # Create new branch from upstream/main
        porcelain.checkout(workspace, target=b"main")

        # Fast-forward main to upstream/main
        r = Repo(workspace)
        upstream_main_ref = b"refs/remotes/upstream/main"
        if upstream_main_ref in r.refs:
            upstream_sha = r.refs[upstream_main_ref]
            # Update main to upstream/main (ff-only equivalent)
            r.refs[b"refs/heads/main"] = upstream_sha
            porcelain.reset(workspace, "hard", upstream_sha)

        # Create and checkout new branch
        porcelain.checkout(workspace, target=b"HEAD", new_branch=branch_name.encode())
        return {"branch_name": branch_name, "created": True}


def handle_commit_and_push(params: dict) -> dict:
    """Commit changes, rebase on upstream/main, push to fork.

    params: {workspace, branch_name, commit_message, files (list)}
    Returns: {pushed: bool, branch_name, commit_sha}
    """
    workspace = params["workspace"]
    branch_name = params["branch_name"]
    commit_message = params["commit_message"]
    files = params.get("files", [])

    # Stage files (default to all changes if none specified)
    if files:
        porcelain.add(workspace, paths=files)
    else:
        porcelain.add(workspace)

    # Commit
    try:
        porcelain.commit(
            workspace,
            message=commit_message.encode(),
            author=_GIT_IDENTITY,
            committer=_GIT_IDENTITY,
        )
    except Exception as e:
        return {"pushed": False, "error": str(e)}

    # Fetch and rebase
    try:
        _run_dulwich(porcelain.fetch, workspace, "upstream")
    except Exception as e:
        logger.warning("Failed to fetch upstream: %s", e)

    try:
        r = Repo(workspace)
        upstream_main_ref = b"refs/remotes/upstream/main"
        if upstream_main_ref in r.refs:
            porcelain.rebase(workspace, upstream="upstream/main")
    except Exception as e:
        return {"pushed": False, "error": str(e)}

    # Push to fork (force to handle rebased history)
    try:
        refspec = f"refs/heads/{branch_name}:refs/heads/{branch_name}"
        _run_dulwich(
            porcelain.push,
            workspace,
            "origin",
            refspecs=[refspec],
            force=True,
        )
    except Exception as e:
        return {"pushed": False, "error": str(e)}

    # Get commit SHA
    commit_sha = porcelain.rev_parse(workspace, "HEAD").decode()

    return {
        "pushed": True,
        "branch_name": branch_name,
        "commit_sha": commit_sha,
    }
