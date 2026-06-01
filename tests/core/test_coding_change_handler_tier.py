"""Tier-based force-human gate at the ``generate-review`` step (PR 6).

These tests pin the runtime T1 force-human gate added to
:func:`carpenter.core.workflows.coding_change_handler.handle_generate_review`.

The gate replaces the legacy ``_CONFUSION_FILES``/``_CONFUSION_PREFIXES``
heuristic with a principled path-tier check on the *destination* of each
changed workspace file.  When any destination classifies as T1, the arc
gets ``_review_mode = "human"`` and an ``integrity.t1_change_proposed``
audit row, even though the diff itself is allowed to proceed to review.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import dulwich.porcelain as porcelain
import pytest

from carpenter import config as carpenter_config
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.trust import audit as trust_audit
from carpenter.core.workflows import coding_change_handler


_TEST_IDENTITY = b"Test <test@test.com>"


def _git_init(src: Path) -> None:
    porcelain.init(str(src))
    porcelain.add(str(src))
    porcelain.commit(
        str(src),
        message=b"init",
        author=_TEST_IDENTITY,
        committer=_TEST_IDENTITY,
    )


@pytest.fixture
def fake_repo_root(tmp_path, monkeypatch) -> Path:
    """Set carpenter ``repo_dir`` to a tmp_path-rooted fake repo.

    Files under ``<repo>/carpenter/`` become T1 by the hardcoded prefix
    list; everything else under tmp_path is T2.
    """
    repo = tmp_path / "repo"
    (repo / "carpenter" / "security").mkdir(parents=True)
    (repo / "carpenter" / "security" / "judge.py").write_text(
        "# placeholder\n"
    )
    (repo / "carpenter" / "__init__.py").write_text("")
    monkeypatch.setitem(carpenter_config.CONFIG, "repo_dir", str(repo))
    return repo


def _make_source_dir(tmp_path: Path, sub: str) -> str:
    src = tmp_path / sub
    src.mkdir(parents=True)
    (src / "main.py").write_text("print('hi')\n")
    _git_init(src)
    return str(src)


def _make_t1_source_dir(repo: Path) -> str:
    """A source_dir that maps changed files into the T1 ``carpenter/``
    tree of the fake repo.

    Returns the source_dir as the repo root itself; changed files like
    ``carpenter/security/judge.py`` then resolve as T1 destinations.
    """
    # Init git inside the repo only if not already initialised so
    # workspace diffs are well-defined.
    if not (repo / ".git").exists():
        _git_init(repo)
    return str(repo)


def _seed_workspace(arc_id: int, source_dir: str, files: dict[str, str]) -> str:
    """Create a workspace under source_dir, write the given files, and
    register workspace_path + source_dir on the arc."""
    from carpenter.core import workspace_manager

    ws, _ = workspace_manager.create_workspace(source_dir, f"arc-{arc_id}")
    for rel, content in files.items():
        target = os.path.join(ws, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as fh:
            fh.write(content)
    coding_change_handler._set_arc_state(arc_id, "workspace_path", ws)
    coding_change_handler._set_arc_state(arc_id, "source_dir", source_dir)
    return ws


class TestT1GateAtGenerateReview:
    @pytest.mark.asyncio
    async def test_t2_only_diff_does_not_force_human(
        self, test_db, tmp_path, fake_repo_root,
    ):
        """A diff that only touches T2 destinations leaves ``_review_mode``
        unset and emits no ``integrity.t1_change_proposed`` audit row."""
        source_dir = _make_source_dir(tmp_path, "project")
        arc_id = arc_manager.create_arc(
            name="t2-only", goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(arc_id, "active")
        _seed_workspace(arc_id, source_dir, {
            "main.py": "print('modified')\n",
        })

        await coding_change_handler.handle_generate_review(
            1, {"arc_id": arc_id},
        )

        assert coding_change_handler._get_arc_state(
            arc_id, "_review_mode",
        ) is None
        assert coding_change_handler._get_arc_state(
            arc_id, "_t1_files_detected",
        ) is None
        events = trust_audit.get_trust_events(
            event_type="integrity.t1_change_proposed",
        )
        assert events == []

    @pytest.mark.asyncio
    async def test_single_t1_file_forces_human(
        self, test_db, fake_repo_root,
    ):
        """A diff that touches a single T1 destination forces
        ``_review_mode == 'human'`` and writes an audit row."""
        source_dir = _make_t1_source_dir(fake_repo_root)
        arc_id = arc_manager.create_arc(
            name="t1-single", goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(arc_id, "active")
        _seed_workspace(arc_id, source_dir, {
            "carpenter/security/judge.py": "# patched\n",
        })

        await coding_change_handler.handle_generate_review(
            1, {"arc_id": arc_id},
        )

        assert coding_change_handler._get_arc_state(
            arc_id, "_review_mode",
        ) == "human"
        detected = coding_change_handler._get_arc_state(
            arc_id, "_t1_files_detected",
        )
        assert detected is not None
        assert any(
            p.endswith("carpenter/security/judge.py") for p in detected
        )

        events = trust_audit.get_trust_events(
            event_type="integrity.t1_change_proposed",
        )
        assert len(events) == 1
        details = events[0]["details"]
        assert details["arc_id"] == arc_id
        assert details["forced_human_review"] is True
        assert any(
            p.endswith("carpenter/security/judge.py")
            for p in details["t1_paths"]
        )

        # Arc still proceeds to ``waiting`` — the diff is allowed,
        # await-approval is what becomes mandatory.
        arc = arc_manager.get_arc(arc_id)
        assert arc["status"] == "waiting"

    @pytest.mark.asyncio
    async def test_mixed_t1_t2_lists_only_t1_paths(
        self, test_db, fake_repo_root,
    ):
        """A diff that touches both a T1 and a T2 destination still forces
        human review; only T1 paths appear in the audit detail."""
        # Add a T2 file under a subdir that is NOT in the T1 prefix list.
        # We must do this BEFORE git-init'ing the repo so it gets included
        # in the workspace's initial commit (and our subsequent overwrite
        # shows up as a diff).
        (fake_repo_root / "user_data").mkdir(exist_ok=True)
        (fake_repo_root / "user_data" / "note.md").write_text("seed\n")

        source_dir = _make_t1_source_dir(fake_repo_root)
        arc_id = arc_manager.create_arc(
            name="t1-mixed", goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(arc_id, "active")

        _seed_workspace(arc_id, source_dir, {
            "carpenter/__init__.py": "# T1 patched\n",
            "user_data/note.md": "# T2 patched\n",
        })

        await coding_change_handler.handle_generate_review(
            1, {"arc_id": arc_id},
        )

        assert coding_change_handler._get_arc_state(
            arc_id, "_review_mode",
        ) == "human"

        events = trust_audit.get_trust_events(
            event_type="integrity.t1_change_proposed",
        )
        assert len(events) == 1
        t1_paths = events[0]["details"]["t1_paths"]
        assert any(p.endswith("carpenter/__init__.py") for p in t1_paths)
        # T2 paths must not appear in the t1_paths list.
        assert not any(p.endswith("user_data/note.md") for p in t1_paths)

    @pytest.mark.asyncio
    async def test_destination_path_resolution_uses_source_dir(
        self, test_db, fake_repo_root,
    ):
        """The classifier sees the source_dir-rooted destination, not the
        workspace path.

        We patch ``path_tier`` to capture the arguments it's called with
        and assert that the first argument is the destination path
        (``<source_dir>/<rel>``), not the workspace path.
        """
        source_dir = _make_t1_source_dir(fake_repo_root)
        arc_id = arc_manager.create_arc(
            name="t1-paths", goal=f"changes for {source_dir}",
        )
        arc_manager.update_status(arc_id, "active")
        ws = _seed_workspace(arc_id, source_dir, {
            "carpenter/security/judge.py": "# x\n",
        })

        # Spy on path_tier to capture its inputs.
        seen: list[str] = []
        from carpenter.security import platform_paths as pp_mod
        original_tier = pp_mod.path_tier

        def _spy(p):
            seen.append(p)
            return original_tier(p)

        # Patch the symbol that handler imports (late-bound at call time).
        with patch.object(pp_mod, "path_tier", side_effect=_spy):
            await coding_change_handler.handle_generate_review(
                1, {"arc_id": arc_id},
            )

        # The handler imports symbols at call time; ensure we saw the
        # destination, NOT the workspace path.
        assert any(
            os.path.normpath(p) == os.path.normpath(
                os.path.join(source_dir, "carpenter/security/judge.py"),
            )
            for p in seen
        ), f"path_tier was not called with the destination path; saw {seen!r}"
        assert not any(ws in p for p in seen), (
            f"path_tier was called with a workspace-rooted path; saw {seen!r}"
        )

    @pytest.mark.asyncio
    async def test_legacy_confusion_constants_removed(self, test_db, tmp_path):
        """The legacy ``_CONFUSION_FILES`` / ``_CONFUSION_PREFIXES``
        constants have been removed from the handler module.

        This is a contract test: nothing else in the codebase should
        import or depend on them.  The tier check has replaced them.
        """
        assert not hasattr(coding_change_handler, "_CONFUSION_FILES")
        assert not hasattr(coding_change_handler, "_CONFUSION_PREFIXES")
