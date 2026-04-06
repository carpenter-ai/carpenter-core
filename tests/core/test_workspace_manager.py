"""Tests for workspace_manager: git-backed workspace lifecycle."""

import os

import dulwich.porcelain as porcelain
from dulwich.repo import Repo
import pytest

from carpenter.core import workspace_manager
from carpenter.core.arcs import manager as arc_manager

# Author identity for test commits.
_TEST_IDENTITY = b"Test <test@test.com>"


def _git_init(path: str):
    """Initialize a git repo with an initial commit using dulwich."""
    porcelain.init(path)
    porcelain.add(path)
    porcelain.commit(
        path,
        message=b"init",
        author=_TEST_IDENTITY,
        committer=_TEST_IDENTITY,
    )


class TestCreateWorkspace:
    def test_creates_workspace_directory(self, tmp_path):
        """Workspace directory is created from source."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "hello.py").write_text("print('hello')\n")

        ws, base_sha = workspace_manager.create_workspace(str(source), "test-ws")
        assert os.path.isdir(ws)
        assert os.path.isfile(os.path.join(ws, "hello.py"))

    def test_returns_base_sha_for_git_repo(self, tmp_path):
        """create_workspace returns (path, sha) when source is a git repo."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "a.txt").write_text("content")
        _git_init(str(source))

        ws, base_sha = workspace_manager.create_workspace(str(source), "sha-test")
        assert base_sha is not None
        assert len(base_sha) == 40  # Full SHA

    def test_returns_none_sha_for_non_git(self, tmp_path):
        """create_workspace returns (path, None) when source is not a git repo."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "a.txt").write_text("content")

        ws, base_sha = workspace_manager.create_workspace(str(source), "no-git")
        assert base_sha is None

    def test_workspace_has_git_repo(self, tmp_path):
        """Workspace is a valid git repo with initial commit."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "a.txt").write_text("content")

        ws, _ = workspace_manager.create_workspace(str(source), "test-ws")
        assert os.path.isdir(os.path.join(ws, ".git"))

        # Verify initial commit exists using dulwich
        r = Repo(ws)
        head_sha = r.head()
        head_commit = r[head_sha]
        assert b"initial state" in head_commit.message

    def test_preserves_directory_structure(self, tmp_path):
        """Nested directories are preserved in workspace."""
        source = tmp_path / "source"
        (source / "sub" / "dir").mkdir(parents=True)
        (source / "sub" / "dir" / "file.txt").write_text("nested")

        ws, _ = workspace_manager.create_workspace(str(source), "nested-test")
        assert os.path.isfile(os.path.join(ws, "sub", "dir", "file.txt"))

    def test_source_not_found(self, tmp_path):
        """Raises FileNotFoundError for missing source."""
        import pytest
        with pytest.raises(FileNotFoundError):
            workspace_manager.create_workspace("/nonexistent/path", "bad")

    def test_strips_existing_git_dir(self, tmp_path):
        """Existing .git in source is removed, fresh repo created."""
        source = tmp_path / "source"
        source.mkdir()
        (source / ".git").mkdir()
        (source / ".git" / "marker").write_text("old")
        (source / "code.py").write_text("x = 1")

        ws, _ = workspace_manager.create_workspace(str(source), "strip-git")
        # Should have a valid .git, not the old marker
        assert not os.path.isfile(os.path.join(ws, ".git", "marker"))
        # Verify repo is valid using dulwich
        r = Repo(ws)
        assert r.head() is not None


class TestGetDiff:
    def test_no_changes(self, tmp_path):
        """No changes returns empty diff."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("original")

        ws, _ = workspace_manager.create_workspace(str(source), "no-diff")
        diff = workspace_manager.get_diff(ws)
        assert diff.strip() == ""

    def test_modified_file(self, tmp_path):
        """Modified file shows in diff."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("line1\n")

        ws, _ = workspace_manager.create_workspace(str(source), "mod-diff")

        # Modify a file
        with open(os.path.join(ws, "file.txt"), "w") as f:
            f.write("line1\nline2\n")

        diff = workspace_manager.get_diff(ws)
        assert "+line2" in diff

    def test_new_file(self, tmp_path):
        """New file shows in diff."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "existing.txt").write_text("exists")

        ws, _ = workspace_manager.create_workspace(str(source), "new-file")

        with open(os.path.join(ws, "new.txt"), "w") as f:
            f.write("brand new\n")

        diff = workspace_manager.get_diff(ws)
        assert "new.txt" in diff
        assert "+brand new" in diff


class TestGetChangedFiles:
    def test_no_changes(self, tmp_path):
        """No changes returns empty list."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("original")

        ws, _ = workspace_manager.create_workspace(str(source), "no-changes")
        assert workspace_manager.get_changed_files(ws) == []

    def test_lists_changed_files(self, tmp_path):
        """Changed files are listed."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "a.txt").write_text("a")
        (source / "b.txt").write_text("b")

        ws, _ = workspace_manager.create_workspace(str(source), "changes")

        with open(os.path.join(ws, "a.txt"), "w") as f:
            f.write("modified a")

        files = workspace_manager.get_changed_files(ws)
        assert "a.txt" in files
        assert "b.txt" not in files


class TestApplyToSource:
    def test_applies_changes(self, tmp_path):
        """Changed files are applied back to source."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("original\n")
        _git_init(str(source))

        ws, _ = workspace_manager.create_workspace(str(source), "apply")

        with open(os.path.join(ws, "file.txt"), "w") as f:
            f.write("modified\n")

        applied = workspace_manager.apply_to_source(ws, str(source))
        assert "file.txt" in applied
        assert (source / "file.txt").read_text() == "modified\n"

    def test_applies_new_files(self, tmp_path):
        """New files created in workspace are applied to source."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "existing.txt").write_text("exists")
        _git_init(str(source))

        ws, _ = workspace_manager.create_workspace(str(source), "apply-new")

        os.makedirs(os.path.join(ws, "new_dir"))
        with open(os.path.join(ws, "new_dir", "new.txt"), "w") as f:
            f.write("new content\n")

        applied = workspace_manager.apply_to_source(ws, str(source))
        assert "new_dir/new.txt" in applied
        assert (source / "new_dir" / "new.txt").read_text() == "new content\n"

    def test_no_changes_returns_empty(self, tmp_path):
        """No changes in workspace returns empty list."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("unchanged\n")
        _git_init(str(source))

        ws, _ = workspace_manager.create_workspace(str(source), "no-change")
        applied = workspace_manager.apply_to_source(ws, str(source))
        assert applied == []


class TestCleanupWorkspace:
    def test_removes_directory(self, tmp_path):
        """cleanup_workspace removes the workspace directory."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "file.txt").write_text("data")

        workspace_manager.cleanup_workspace(str(ws))
        assert not os.path.isdir(str(ws))

    def test_noop_for_nonexistent(self, tmp_path):
        """cleanup_workspace is a no-op for nonexistent paths."""
        workspace_manager.cleanup_workspace(str(tmp_path / "nonexistent"))

    def test_logs_traceback(self, tmp_path, caplog):
        """cleanup_workspace logs caller traceback."""
        import logging
        ws = tmp_path / "workspace"
        ws.mkdir()

        with caplog.at_level(logging.INFO, logger="carpenter.core.workspace_manager"):
            workspace_manager.cleanup_workspace(str(ws))

        assert "Cleaned up workspace" in caplog.text
        assert "Called by:" in caplog.text
        # Should include the test function name in the traceback
        assert "test_logs_traceback" in caplog.text


class TestApplyToSourceViaMerge:
    def test_merge_clean_succeeds(self, tmp_path):
        """Merge-based apply works for clean changes."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("original\n")
        _git_init(str(source))

        ws, base_sha = workspace_manager.create_workspace(str(source), "merge-clean")
        assert base_sha is not None

        with open(os.path.join(ws, "file.txt"), "w") as f:
            f.write("modified\n")

        applied = workspace_manager.apply_to_source_via_merge(
            ws, str(source), base_sha, arc_id="test1",
        )
        assert "file.txt" in applied
        assert (source / "file.txt").read_text() == "modified\n"

    def test_merge_non_conflicting_divergence(self, tmp_path):
        """Merge succeeds when different files changed in source vs workspace."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "a.txt").write_text("aaa\n")
        (source / "b.txt").write_text("bbb\n")
        _git_init(str(source))

        ws, base_sha = workspace_manager.create_workspace(str(source), "diverge-ok")

        # Agent modifies a.txt in workspace
        with open(os.path.join(ws, "a.txt"), "w") as f:
            f.write("aaa-agent\n")

        # Source modifies b.txt (non-conflicting)
        (source / "b.txt").write_text("bbb-human\n")
        porcelain.add(str(source), paths=["b.txt"])
        porcelain.commit(
            str(source),
            message=b"human edit",
            author=_TEST_IDENTITY,
            committer=_TEST_IDENTITY,
        )

        applied = workspace_manager.apply_to_source_via_merge(
            ws, str(source), base_sha, arc_id="test2",
        )
        assert "a.txt" in applied
        assert (source / "a.txt").read_text() == "aaa-agent\n"
        assert (source / "b.txt").read_text() == "bbb-human\n"

    def test_merge_conflict_aborts_cleanly(self, tmp_path):
        """Same lines changed in both -> MergeConflictError, source unchanged."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("line1\nline2\nline3\n")
        _git_init(str(source))

        ws, base_sha = workspace_manager.create_workspace(str(source), "conflict-merge")

        # Agent modifies line 2
        with open(os.path.join(ws, "file.txt"), "w") as f:
            f.write("line1\nline2-agent\nline3\n")

        # Source also modifies line 2
        (source / "file.txt").write_text("line1\nline2-human\nline3\n")
        porcelain.add(str(source), paths=["file.txt"])
        porcelain.commit(
            str(source),
            message=b"human edit",
            author=_TEST_IDENTITY,
            committer=_TEST_IDENTITY,
        )

        with pytest.raises(workspace_manager.MergeConflictError) as exc_info:
            workspace_manager.apply_to_source_via_merge(
                ws, str(source), base_sha, arc_id="test3",
            )

        assert len(exc_info.value.conflicting_files) > 0
        # Source should still have the human version
        assert "line2-human" in (source / "file.txt").read_text()

    def test_temp_branch_cleaned_up_on_success(self, tmp_path):
        """No _carpenter_merge_* branches remain after success."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("original\n")
        _git_init(str(source))

        ws, base_sha = workspace_manager.create_workspace(str(source), "cleanup-ok")

        with open(os.path.join(ws, "file.txt"), "w") as f:
            f.write("changed\n")

        workspace_manager.apply_to_source_via_merge(
            ws, str(source), base_sha, arc_id="clean1",
        )

        branches = porcelain.branch_list(str(source))
        branch_names = [b.decode() if isinstance(b, bytes) else str(b) for b in branches]
        assert not any("_carpenter_merge_" in b for b in branch_names)

    def test_temp_branch_cleaned_up_on_conflict(self, tmp_path):
        """No _carpenter_merge_* branches remain after conflict."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("line1\nline2\nline3\n")
        _git_init(str(source))

        ws, base_sha = workspace_manager.create_workspace(str(source), "cleanup-conflict")

        with open(os.path.join(ws, "file.txt"), "w") as f:
            f.write("line1\nline2-agent\nline3\n")

        (source / "file.txt").write_text("line1\nline2-human\nline3\n")
        porcelain.add(str(source), paths=["file.txt"])
        porcelain.commit(
            str(source),
            message=b"human",
            author=_TEST_IDENTITY,
            committer=_TEST_IDENTITY,
        )

        with pytest.raises(workspace_manager.MergeConflictError):
            workspace_manager.apply_to_source_via_merge(
                ws, str(source), base_sha, arc_id="clean2",
            )

        branches = porcelain.branch_list(str(source))
        branch_names = [b.decode() if isinstance(b, bytes) else str(b) for b in branches]
        assert not any("_carpenter_merge_" in b for b in branch_names)

    def test_no_changes_returns_empty(self, tmp_path):
        """No changes in workspace returns empty list."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("unchanged\n")
        _git_init(str(source))

        ws, base_sha = workspace_manager.create_workspace(str(source), "merge-nochange")
        applied = workspace_manager.apply_to_source_via_merge(
            ws, str(source), base_sha, arc_id="noop",
        )
        assert applied == []


class TestHasActiveChangeset:
    def test_no_active_changesets(self, test_db):
        """Returns False when no coding-change arcs exist."""
        assert not workspace_manager.has_active_changeset("/some/dir")

    def test_active_changeset_exists(self, test_db):
        """Returns True when an active coding-change arc exists for source dir."""
        arc_id = arc_manager.create_arc(
            name="coding-change-test",
            goal="changes for /some/dir",
        )
        arc_manager.update_status(arc_id, "active")

        assert workspace_manager.has_active_changeset("/some/dir")

    def test_completed_changeset_not_counted(self, test_db):
        """Completed arcs don't count as active changesets."""
        arc_id = arc_manager.create_arc(
            name="coding-change-test",
            goal="changes for /some/dir",
        )
        arc_manager.update_status(arc_id, "active")
        arc_manager.update_status(arc_id, "completed")

        assert not workspace_manager.has_active_changeset("/some/dir")


class TestCleanupWorkspace:
    def test_removes_directory(self, tmp_path):
        """Workspace directory is removed."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("data")

        ws, _ = workspace_manager.create_workspace(str(source), "cleanup")
        assert os.path.isdir(ws)

        workspace_manager.cleanup_workspace(ws)
        assert not os.path.isdir(ws)

    def test_missing_directory_noop(self, tmp_path):
        """Cleaning up nonexistent directory doesn't raise."""
        workspace_manager.cleanup_workspace(str(tmp_path / "nonexistent"))
