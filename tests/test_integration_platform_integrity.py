"""End-to-end integration tests for the platform-integrity T1 gate (PR 6).

Exercises the runtime force-human review path through a full
coding-change ``generate-review`` step:

1. A coding-change arc whose workspace diff touches a T1 file
   (``carpenter/security/judge.py``) — even though the arc was
   spun up with no ``affected_paths`` and the agent reached the file
   inside its workspace — must end up with ``_review_mode = "human"``
   and an ``integrity.t1_change_proposed`` audit row, and the next
   work item must be a ``coding-change.approval`` waiting for
   ``arc.manual_trigger``.

2. A coding-change arc whose workspace diff only touches T2 files
   takes the standard path (no forced human review, no audit row).
"""

from __future__ import annotations

import os
from pathlib import Path

import dulwich.porcelain as porcelain
import pytest

from carpenter import config as carpenter_config
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.trust import audit as trust_audit
from carpenter.core.workflows import coding_change_handler
from carpenter.core import workspace_manager
from carpenter.db import get_db


_TEST_IDENTITY = b"Test <test@test.com>"


def _git_init(path: Path) -> None:
    porcelain.init(str(path))
    porcelain.add(str(path))
    porcelain.commit(
        str(path),
        message=b"init",
        author=_TEST_IDENTITY,
        committer=_TEST_IDENTITY,
    )


@pytest.fixture
def fake_repo(tmp_path, monkeypatch) -> Path:
    """Build a tmp_path-rooted fake carpenter repo + point ``repo_dir`` at it."""
    repo = tmp_path / "repo"
    (repo / "carpenter" / "security").mkdir(parents=True)
    (repo / "carpenter" / "__init__.py").write_text("")
    (repo / "carpenter" / "security" / "__init__.py").write_text("")
    (repo / "carpenter" / "security" / "judge.py").write_text(
        "# seed judge file\n",
    )
    monkeypatch.setitem(carpenter_config.CONFIG, "repo_dir", str(repo))
    _git_init(repo)
    return repo


@pytest.fixture
def t2_source(tmp_path) -> str:
    """A T2 source dir (under tmp_path, outside the fake repo)."""
    src = tmp_path / "user_project"
    src.mkdir()
    (src / "main.py").write_text("print('hi')\n")
    (src / "kb").mkdir()
    (src / "kb" / "foo.md").write_text("# notes\n")
    _git_init(src)
    return str(src)


def _setup_workspace(arc_id: int, source_dir: str, files: dict[str, str]) -> str:
    ws, _ = workspace_manager.create_workspace(source_dir, f"arc-{arc_id}")
    for rel, content in files.items():
        target = os.path.join(ws, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as fh:
            fh.write(content)
    coding_change_handler._set_arc_state(arc_id, "workspace_path", ws)
    coding_change_handler._set_arc_state(arc_id, "source_dir", source_dir)
    return ws


@pytest.mark.asyncio
async def test_t1_judge_diff_forces_human_review(test_db, fake_repo):
    """A coding-change arc whose workspace diff modifies
    ``carpenter/security/judge.py`` must be force-routed to human review.
    """
    source_dir = str(fake_repo)
    arc_id = arc_manager.create_arc(
        name="coding-change-t1", goal=f"changes for {source_dir}",
    )
    arc_manager.update_status(arc_id, "active")
    _setup_workspace(arc_id, source_dir, {
        "carpenter/security/judge.py": (
            "# the agent legitimately ended up patching judge\n"
        ),
    })

    await coding_change_handler.handle_generate_review(
        1, {"arc_id": arc_id},
    )

    # _review_mode is forced to human.
    assert coding_change_handler._get_arc_state(
        arc_id, "_review_mode",
    ) == "human"

    # _t1_files_detected lists the destination path for judge.py.
    detected = coding_change_handler._get_arc_state(
        arc_id, "_t1_files_detected",
    )
    assert detected is not None
    expected_dest = os.path.normpath(
        os.path.join(source_dir, "carpenter/security/judge.py"),
    )
    assert any(os.path.normpath(p) == expected_dest for p in detected)

    # Audit row recorded.
    events = trust_audit.get_trust_events(
        event_type="integrity.t1_change_proposed",
    )
    assert len(events) == 1
    audit = events[0]
    assert audit["arc_id"] == arc_id
    details = audit["details"]
    assert details["forced_human_review"] is True
    assert details["arc_id"] == arc_id
    assert any(os.path.normpath(p) == expected_dest for p in details["t1_paths"])

    # The arc is now ``waiting``; the next work item is the approval
    # step that waits for ``arc.manual_trigger`` (i.e. a human decision
    # via the review API).  We don't run the approval handler here —
    # we just assert that the arc is parked, the diff was not
    # auto-approved, and no ``arc.manual_trigger`` has fired yet.
    arc = arc_manager.get_arc(arc_id)
    assert arc["status"] == "waiting"

    db = get_db()
    try:
        # No coding-change.approval has been enqueued yet (only fires
        # after the review API processes a human decision).
        approval_rows = db.execute(
            "SELECT * FROM work_queue "
            "WHERE event_type = 'coding-change.approval' "
            "AND payload_json LIKE ?",
            (f'%"arc_id": {arc_id}%',),
        ).fetchall()
        assert approval_rows == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_t2_only_diff_takes_standard_path(test_db, t2_source):
    """A coding-change arc whose workspace diff touches only T2 files
    (e.g. ``~/carpenter/skills/foo.md``-style user content) is *not*
    force-routed; no audit row is emitted."""
    source_dir = t2_source
    arc_id = arc_manager.create_arc(
        name="coding-change-t2", goal=f"changes for {source_dir}",
    )
    arc_manager.update_status(arc_id, "active")
    _setup_workspace(arc_id, source_dir, {
        "kb/foo.md": "# revised notes\n",
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
    # No T1 event for this arc.
    assert all(e["arc_id"] != arc_id for e in events)

    arc = arc_manager.get_arc(arc_id)
    # Without verification configured, the arc parks at ``waiting`` for
    # the human review API to call back — same standard path as before
    # PR 6.
    assert arc["status"] == "waiting"
