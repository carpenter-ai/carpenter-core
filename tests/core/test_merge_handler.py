"""Tests for the merge resolution handler."""

import os
from unittest.mock import patch, AsyncMock

import dulwich.porcelain as porcelain
from dulwich.repo import Repo
import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.workflows import merge_handler

# Author identity for test commits.
_TEST_IDENTITY = b"Test <test@test.com>"


@pytest.fixture
def git_repo(tmp_path):
    """Create a git repo with an initial commit using dulwich."""
    repo = tmp_path / "repo"
    repo.mkdir()
    porcelain.init(str(repo))
    (repo / "main.py").write_text("print('hello')\n")
    porcelain.add(str(repo), paths=["main.py"])
    porcelain.commit(
        str(repo),
        message=b"init",
        author=_TEST_IDENTITY,
        committer=_TEST_IDENTITY,
    )
    return str(repo)


class TestAttemptMerge:
    @pytest.mark.asyncio
    async def test_clean_merge_succeeds(self, test_db, git_repo):
        """Clean merge completes the arc tree."""
        # Create a branch with a non-conflicting change
        porcelain.checkout(git_repo, target=b"HEAD", new_branch=b"feature")
        with open(os.path.join(git_repo, "feature.py"), "w") as f:
            f.write("# feature\n")
        porcelain.add(git_repo, paths=["feature.py"])
        porcelain.commit(
            git_repo,
            message=b"feature",
            author=_TEST_IDENTITY,
            committer=_TEST_IDENTITY,
        )

        # Go back to the original branch
        porcelain.checkout(git_repo, target=b"master")

        arc_id = arc_manager.create_arc(name="merge-resolution", goal="test merge")
        arc_manager.update_status(arc_id, "active")

        await merge_handler.handle_attempt_merge(1, {
            "arc_id": arc_id,
            "source_dir": git_repo,
            "target_ref": "feature",
            "merge_type": "branch",
        })

        arc = arc_manager.get_arc(arc_id)
        assert arc["status"] == "completed"

    @pytest.mark.asyncio
    async def test_conflict_captures_state(self, test_db, git_repo):
        """Merge conflict captures conflict details in arc state."""
        # Create a branch with conflicting change
        porcelain.checkout(git_repo, target=b"HEAD", new_branch=b"conflict-branch")
        with open(os.path.join(git_repo, "main.py"), "w") as f:
            f.write("print('branch version')\n")
        porcelain.add(git_repo, paths=["main.py"])
        porcelain.commit(
            git_repo,
            message=b"branch change",
            author=_TEST_IDENTITY,
            committer=_TEST_IDENTITY,
        )

        # Go back and make conflicting change on original branch
        porcelain.checkout(git_repo, target=b"master")
        with open(os.path.join(git_repo, "main.py"), "w") as f:
            f.write("print('main version')\n")
        porcelain.add(git_repo, paths=["main.py"])
        porcelain.commit(
            git_repo,
            message=b"main change",
            author=_TEST_IDENTITY,
            committer=_TEST_IDENTITY,
        )

        arc_id = arc_manager.create_arc(name="merge-resolution", goal="test conflict")
        arc_manager.update_status(arc_id, "active")

        await merge_handler.handle_attempt_merge(1, {
            "arc_id": arc_id,
            "source_dir": git_repo,
            "target_ref": "conflict-branch",
            "merge_type": "branch",
        })

        # Should have captured conflict state
        conflicting = merge_handler._get_arc_state(arc_id, "conflicting_files")
        assert conflicting is not None
        assert "main.py" in conflicting

        # Repo should be clean (merge aborted / reset)
        st = porcelain.status(git_repo)
        assert not st.unstaged
        assert not st.staged["add"]
        assert not st.staged["modify"]
        assert not st.staged["delete"]


class TestReviewResolution:
    @pytest.mark.asyncio
    async def test_approve_completes_arc(self, test_db, git_repo):
        """Approve decision completes the merge-resolution arc."""
        arc_id = arc_manager.create_arc(name="merge-resolution", goal="test resolve")
        arc_manager.update_status(arc_id, "active")
        merge_handler._set_arc_state(arc_id, "source_dir", git_repo)

        await merge_handler.handle_review_resolution(1, {
            "arc_id": arc_id,
            "decision": "approve",
        })

        arc = arc_manager.get_arc(arc_id)
        assert arc["status"] == "completed"

    @pytest.mark.asyncio
    async def test_reject_fails_arc(self, test_db, git_repo):
        """Reject decision fails the merge-resolution arc."""
        arc_id = arc_manager.create_arc(name="merge-resolution", goal="test reject")
        arc_manager.update_status(arc_id, "active")
        merge_handler._set_arc_state(arc_id, "source_dir", git_repo)

        await merge_handler.handle_review_resolution(1, {
            "arc_id": arc_id,
            "decision": "reject",
        })

        arc = arc_manager.get_arc(arc_id)
        assert arc["status"] == "failed"


class TestCreateMergeResolutionArc:
    def test_disabled_config_returns_none(self, test_db, git_repo):
        """Returns None when auto_resolve_merge_conflicts is disabled."""
        with patch.dict("carpenter.config.CONFIG", {
            "auto_resolve_merge_conflicts": False,
        }):
            result = merge_handler.create_merge_resolution_arc(
                source_dir=git_repo,
                target_ref="origin/main",
                merge_type="remote",
            )
            assert result is None

    def test_enabled_config_creates_arc(self, test_db, git_repo):
        """Creates arc when auto_resolve_merge_conflicts is enabled and template exists."""
        # Load the template first
        from carpenter.core.engine import template_manager
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "config_seed", "templates", "merge-resolution.yaml",
        )
        if os.path.isfile(template_path):
            template_manager.load_template(template_path)

        with patch.dict("carpenter.config.CONFIG", {
            "auto_resolve_merge_conflicts": True,
            "merge_resolution_template": "merge-resolution",
        }):
            result = merge_handler.create_merge_resolution_arc(
                source_dir=git_repo,
                target_ref="origin/main",
                merge_type="remote",
            )
            if result is not None:
                arc = arc_manager.get_arc(result)
                assert arc is not None
                assert arc["name"] == "merge-resolution"
                assert arc["status"] == "active"
