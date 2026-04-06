"""Tests for carpenter.tool_backends.git and carpenter.tool_backends.forgejo_api."""
import os
from unittest.mock import patch, MagicMock, call

import dulwich.porcelain as porcelain

from carpenter.tool_backends import git
from carpenter.tool_backends import forgejo_api


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_repo(path: str) -> str:
    """Create a minimal dulwich repo with an initial commit."""
    os.makedirs(path, exist_ok=True)
    porcelain.init(path)
    with open(os.path.join(path, "README.md"), "w") as f:
        f.write("# Repo\n")
    porcelain.add(path, paths=["README.md"])
    porcelain.commit(
        path,
        message=b"init",
        author=b"Test <test@test.com>",
        committer=b"Test <test@test.com>",
    )
    return path


# ---------------------------------------------------------------------------
# handle_setup_repo
# ---------------------------------------------------------------------------


def test_setup_repo_clone(tmp_path):
    """Clone workflow: clone, rename origin->upstream, add fork as origin."""
    # Create a source repo to clone from
    source = _init_repo(str(tmp_path / "source"))
    workspace = str(tmp_path / "repo")

    result = git.handle_setup_repo({
        "repo_url": source,
        "workspace": workspace,
        "fork_url": "https://example.com/fork.git",
    })

    assert result["success"] is True
    assert result["workspace_path"] == workspace
    assert "upstream" in result["remotes"]
    assert "origin" in result["remotes"]

    # Verify upstream points to source and origin to fork
    from dulwich.repo import Repo
    r = Repo(workspace)
    cfg = r.get_config()
    upstream_url = cfg.get((b"remote", b"upstream"), b"url").decode()
    origin_url = cfg.get((b"remote", b"origin"), b"url").decode()
    assert upstream_url == source
    assert origin_url == "https://example.com/fork.git"


def test_setup_repo_existing(tmp_path):
    """Reconfigure remotes when .git already exists."""
    # Create an existing repo with old remotes
    workspace = _init_repo(str(tmp_path / "repo"))
    porcelain.remote_add(workspace, "upstream", "https://old.example.com/main.git")
    porcelain.remote_add(workspace, "origin", "https://old.example.com/fork.git")

    result = git.handle_setup_repo({
        "repo_url": "https://example.com/main.git",
        "workspace": workspace,
        "fork_url": "https://example.com/fork.git",
    })

    assert result["success"] is True

    # Verify URLs were updated
    from dulwich.repo import Repo
    r = Repo(workspace)
    cfg = r.get_config()
    upstream_url = cfg.get((b"remote", b"upstream"), b"url").decode()
    origin_url = cfg.get((b"remote", b"origin"), b"url").decode()
    assert upstream_url == "https://example.com/main.git"
    assert origin_url == "https://example.com/fork.git"


# ---------------------------------------------------------------------------
# handle_create_branch
# ---------------------------------------------------------------------------


def test_create_branch_new(tmp_path):
    """Create a new branch from main."""
    workspace = _init_repo(str(tmp_path / "repo"))

    # Rename default branch to main
    from dulwich.repo import Repo
    r = Repo(workspace)
    head_sha = r.head()
    r.refs[b"refs/heads/main"] = head_sha
    r.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")

    result = git.handle_create_branch({
        "workspace": workspace,
        "branch_name": "feature-x",
    })

    assert result["branch_name"] == "feature-x"
    assert result["created"] is True

    # Branch should exist
    assert git._branch_exists(workspace, "feature-x")


def test_create_branch_existing(tmp_path):
    """Switch to an existing branch."""
    workspace = _init_repo(str(tmp_path / "repo"))

    # Create the branch first
    porcelain.branch_create(workspace, "feature-x")

    result = git.handle_create_branch({
        "workspace": workspace,
        "branch_name": "feature-x",
    })

    assert result["branch_name"] == "feature-x"
    assert result["created"] is False


# ---------------------------------------------------------------------------
# handle_commit_and_push
# ---------------------------------------------------------------------------


def test_commit_and_push_commit_only(tmp_path):
    """Commit stages and commits files (push mocked since no real remote)."""
    workspace = _init_repo(str(tmp_path / "repo"))

    # Create files to stage
    with open(os.path.join(workspace, "widget.py"), "w") as f:
        f.write("# widget\n")

    # Mock push and fetch since we don't have a real remote
    with patch("carpenter.tool_backends.git.porcelain.fetch"), \
         patch("carpenter.tool_backends.git.porcelain.push"), \
         patch("carpenter.tool_backends.git.porcelain.rebase"):
        result = git.handle_commit_and_push({
            "workspace": workspace,
            "branch_name": "feature-x",
            "commit_message": "Add widget",
            "files": ["widget.py"],
        })

    assert result["pushed"] is True
    assert result["branch_name"] == "feature-x"
    assert len(result["commit_sha"]) == 40

    # Verify the file was committed
    from dulwich.repo import Repo
    r = Repo(workspace)
    head = r[r.head()]
    tree = r[head.tree]
    assert b"widget.py" in [entry.path for entry in tree.items()]


# ---------------------------------------------------------------------------
# handle_create_pr
# ---------------------------------------------------------------------------


def test_create_pr():
    """Create PR via Forgejo API."""
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {
        "number": 42,
        "html_url": "https://forge.example.com/owner/repo/pulls/42",
    }

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.post.return_value = mock_response
        result = forgejo_api.handle_create_pr({
            "repo_owner": "owner",
            "repo_name": "repo",
            "branch_name": "feature-x",
            "pr_title": "Add widget",
            "pr_body": "This adds a widget.",
            "fork_user": "bot-user",
        })

    assert result["pr_number"] == 42
    assert "42" in result["pr_url"]

    # Verify API call
    call_args = mock_httpx.post.call_args
    assert "/repos/owner/repo/pulls" in call_args[0][0]
    payload = call_args[1]["json"]
    assert payload["title"] == "Add widget"
    assert payload["head"] == "bot-user:feature-x"
    assert payload["base"] == "main"


# ---------------------------------------------------------------------------
# handle_list_prs
# ---------------------------------------------------------------------------


def test_list_prs():
    """List PRs via Forgejo API."""
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {
            "number": 1,
            "title": "Fix bug",
            "html_url": "https://forge.example.com/owner/repo/pulls/1",
            "state": "open",
            "head": {"ref": "fix-bug"},
        },
        {
            "number": 2,
            "title": "Add feature",
            "html_url": "https://forge.example.com/owner/repo/pulls/2",
            "state": "open",
            "head": {"ref": "add-feature"},
        },
    ]

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.get.return_value = mock_response
        result = forgejo_api.handle_list_prs({
            "repo_owner": "owner",
            "repo_name": "repo",
        })

    assert len(result["prs"]) == 2
    assert result["prs"][0]["number"] == 1
    assert result["prs"][0]["title"] == "Fix bug"
    assert result["prs"][0]["head_branch"] == "fix-bug"
    assert result["prs"][1]["number"] == 2

    # Verify state param defaulted to "open"
    call_args = mock_httpx.get.call_args
    assert call_args[1]["params"]["state"] == "open"


# ---------------------------------------------------------------------------
# handle_merge_pr
# ---------------------------------------------------------------------------


def test_merge_pr():
    """Merge PR via Forgejo API."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"merged": True}
    mock_response.text = ""

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.post.return_value = mock_response
        result = forgejo_api.handle_merge_pr({
            "repo_owner": "owner",
            "repo_name": "repo",
            "pr_number": 42,
        })

    assert result["merged"] is True

    call_args = mock_httpx.post.call_args
    assert "/pulls/42/merge" in call_args[0][0]
    assert call_args[1]["json"]["Do"] == "merge"


# ---------------------------------------------------------------------------
# handle_close_pr
# ---------------------------------------------------------------------------


def test_close_pr():
    """Close PR without merging via Forgejo API."""
    mock_patch_response = MagicMock()
    mock_patch_response.json.return_value = {"state": "closed"}

    mock_comment_response = MagicMock()
    mock_comment_response.status_code = 201

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.post.return_value = mock_comment_response
        mock_httpx.patch.return_value = mock_patch_response
        result = forgejo_api.handle_close_pr({
            "repo_owner": "owner",
            "repo_name": "repo",
            "pr_number": 7,
            "comment": "Closing: no longer needed",
        })

    assert result["closed"] is True

    # Verify comment was posted
    comment_call = mock_httpx.post.call_args
    assert "/issues/7/comments" in comment_call[0][0]
    assert comment_call[1]["json"]["body"] == "Closing: no longer needed"

    # Verify PATCH to close
    patch_call = mock_httpx.patch.call_args
    assert "/pulls/7" in patch_call[0][0]
    assert patch_call[1]["json"]["state"] == "closed"


# ---------------------------------------------------------------------------
# handle_get_pr
# ---------------------------------------------------------------------------


def test_get_pr():
    """Get PR metadata via Forgejo API."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "number": 5,
        "title": "Add widget",
        "body": "This adds a widget.",
        "state": "open",
        "head": {"ref": "feature-widget"},
        "base": {"ref": "main"},
        "user": {"login": "bot-user"},
        "html_url": "https://forge.example.com/owner/repo/pulls/5",
    }

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.get.return_value = mock_response
        result = forgejo_api.handle_get_pr({
            "repo_owner": "owner",
            "repo_name": "repo",
            "pr_number": 5,
        })

    assert result["number"] == 5
    assert result["title"] == "Add widget"
    assert result["body"] == "This adds a widget."
    assert result["state"] == "open"
    assert result["head_branch"] == "feature-widget"
    assert result["base_branch"] == "main"
    assert result["user"] == "bot-user"

    call_args = mock_httpx.get.call_args
    assert "/repos/owner/repo/pulls/5" in call_args[0][0]


def test_get_pr_not_found():
    """Get PR returns error for non-existent PR."""
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not Found"
    mock_response.json.return_value = {"message": "pull request not found"}

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.get.return_value = mock_response
        result = forgejo_api.handle_get_pr({
            "repo_owner": "owner",
            "repo_name": "repo",
            "pr_number": 999,
        })

    assert "error" in result


# ---------------------------------------------------------------------------
# handle_get_pr_diff
# ---------------------------------------------------------------------------


def test_get_pr_diff():
    """Get PR diff as unified text."""
    diff_text = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1,2 @@\n"
        " # Project\n"
        "+New line\n"
    )
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = diff_text

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.get.return_value = mock_response
        result = forgejo_api.handle_get_pr_diff({
            "repo_owner": "owner",
            "repo_name": "repo",
            "pr_number": 5,
        })

    assert result["diff"] == diff_text

    call_args = mock_httpx.get.call_args
    assert "/repos/owner/repo/pulls/5.diff" in call_args[0][0]


def test_get_pr_diff_error():
    """Get PR diff returns error on failure."""
    mock_response = MagicMock()
    mock_response.status_code = 404

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.get.return_value = mock_response
        result = forgejo_api.handle_get_pr_diff({
            "repo_owner": "owner",
            "repo_name": "repo",
            "pr_number": 999,
        })

    assert "error" in result


# ---------------------------------------------------------------------------
# handle_post_pr_review
# ---------------------------------------------------------------------------


def test_post_pr_review():
    """Submit a PR review via Forgejo API."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "id": 101,
        "state": "APPROVED",
    }

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.post.return_value = mock_response
        result = forgejo_api.handle_post_pr_review({
            "repo_owner": "owner",
            "repo_name": "repo",
            "pr_number": 5,
            "body": "LGTM!",
            "event": "APPROVED",
        })

    assert result["review_id"] == 101
    assert result["state"] == "APPROVED"

    call_args = mock_httpx.post.call_args
    assert "/repos/owner/repo/pulls/5/reviews" in call_args[0][0]
    payload = call_args[1]["json"]
    assert payload["body"] == "LGTM!"
    assert payload["event"] == "APPROVED"


def test_post_pr_review_with_line_comments():
    """Submit a PR review with line-level comments."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": 102, "state": "REQUEST_CHANGES"}

    comments = [
        {"path": "src/widget.py", "body": "Consider renaming", "new_position": 10},
    ]

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.post.return_value = mock_response
        result = forgejo_api.handle_post_pr_review({
            "repo_owner": "owner",
            "repo_name": "repo",
            "pr_number": 5,
            "body": "Some issues found",
            "event": "REQUEST_CHANGES",
            "comments": comments,
        })

    assert result["review_id"] == 102
    payload = mock_httpx.post.call_args[1]["json"]
    assert payload["comments"] == comments


def test_post_pr_review_error():
    """Submit PR review returns error on failure."""
    mock_response = MagicMock()
    mock_response.status_code = 422
    mock_response.json.return_value = {"message": "invalid event"}

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.post.return_value = mock_response
        result = forgejo_api.handle_post_pr_review({
            "repo_owner": "owner",
            "repo_name": "repo",
            "pr_number": 5,
            "body": "test",
            "event": "INVALID",
        })

    assert "error" in result


# ---------------------------------------------------------------------------
# handle_create_repo_webhook
# ---------------------------------------------------------------------------


def test_create_repo_webhook():
    """Register a webhook on a repo via Forgejo API."""
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {
        "id": 77,
        "active": True,
    }

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.post.return_value = mock_response
        result = forgejo_api.handle_create_repo_webhook({
            "repo_owner": "owner",
            "repo_name": "repo",
            "target_url": "https://tc.example.com/api/webhooks/abc123",
            "events": ["pull_request"],
            "secret": "s3cret",
        })

    assert result["hook_id"] == 77
    assert result["active"] is True

    call_args = mock_httpx.post.call_args
    assert "/repos/owner/repo/hooks" in call_args[0][0]
    payload = call_args[1]["json"]
    assert payload["type"] == "forgejo"
    assert payload["active"] is True
    assert payload["events"] == ["pull_request"]
    assert payload["config"]["url"] == "https://tc.example.com/api/webhooks/abc123"
    assert payload["config"]["secret"] == "s3cret"


def test_create_repo_webhook_error():
    """Create webhook returns error on failure."""
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.json.return_value = {"message": "forbidden"}

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.post.return_value = mock_response
        result = forgejo_api.handle_create_repo_webhook({
            "repo_owner": "owner",
            "repo_name": "repo",
            "target_url": "https://tc.example.com/api/webhooks/abc123",
        })

    assert "error" in result


# ---------------------------------------------------------------------------
# handle_delete_repo_webhook
# ---------------------------------------------------------------------------


def test_delete_repo_webhook():
    """Delete a webhook from a repo via Forgejo API."""
    mock_response = MagicMock()
    mock_response.status_code = 204
    mock_response.text = ""

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.delete.return_value = mock_response
        result = forgejo_api.handle_delete_repo_webhook({
            "repo_owner": "owner",
            "repo_name": "repo",
            "hook_id": 77,
        })

    assert result["deleted"] is True

    call_args = mock_httpx.delete.call_args
    assert "/repos/owner/repo/hooks/77" in call_args[0][0]


def test_delete_repo_webhook_error():
    """Delete webhook returns error on failure."""
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not Found"
    mock_response.json.return_value = {"message": "hook not found"}

    with patch("carpenter.tool_backends.forgejo_api.httpx") as mock_httpx:
        mock_httpx.delete.return_value = mock_response
        result = forgejo_api.handle_delete_repo_webhook({
            "repo_owner": "owner",
            "repo_name": "repo",
            "hook_id": 999,
        })

    assert result["deleted"] is False
    assert "error" in result
