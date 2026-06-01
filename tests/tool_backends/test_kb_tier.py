"""Platform-integrity tier enforcement in the kb.* tool backend.

Covers the T0/T1 gates added by PR 4 of the platform-integrity rollout.
The classifier tests live in ``tests/security/test_platform_paths.py``
and the files.* enforcement is pinned by ``test_files_tier.py``; this
file pins the analogous wiring inside ``carpenter.tool_backends.kb``.

The autouse ``test_db`` fixture in ``tests/conftest.py`` points
``CONFIG['kb']['dir']`` at ``<tmp_path>/kb``, which by default sits
outside any T1 prefix (the repo root is the real repo).  To exercise
T1 behavior we monkeypatch the KB store's ``kb_dir`` to point inside
the T1 platform tree.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from carpenter import config as carpenter_config
from carpenter.core.trust.audit import get_trust_events
from carpenter.kb import get_store
from carpenter.tool_backends import kb as kb_backend


def _set_repo_root(monkeypatch, root: str) -> None:
    """Pin the classifier's repo root inside the per-test tmp_path."""
    monkeypatch.setitem(carpenter_config.CONFIG, "repo_dir", root)


def _get_dispatch_error_cls():
    from carpenter.executor.dispatch_bridge import DispatchError
    return DispatchError


def _audit_events_for(event_type: str):
    return get_trust_events(event_type=f"integrity.{event_type}", limit=100)


def _point_kb_at_t1(monkeypatch, tmp_path: Path) -> Path:
    """Make the KB store write into the T1 platform tree for this test.

    Creates ``<tmp_path>/repo/config_seed/kb/`` (a T1 prefix) and points
    the in-memory store at it via ``store.kb_dir``.
    """
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    kb_in_platform = tmp_path / "repo" / "config_seed" / "kb"
    kb_in_platform.mkdir(parents=True)
    store = get_store()
    monkeypatch.setattr(store, "kb_dir", str(kb_in_platform))
    return kb_in_platform


# ── T2 (user-home) path — add/edit should still proceed ─────────────────


def test_kb_edit_t2_path_proceeds(monkeypatch, tmp_path):
    """A KB edit on the default user-home (T2) kb_dir succeeds.

    Highest-risk regression test in this PR: it confirms the tier gate
    does not break ordinary KB writes.
    """
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    store = get_store()

    # Create an entry first so edit has something to modify.
    add_result = kb_backend.handle_add({
        "path": "user-note",
        "content": "# Title\nbody",
        "description": "a user note",
    })
    assert "error" not in add_result, add_result

    edit_result = kb_backend.handle_edit({
        "path": "user-note",
        "content": "# Title\nupdated body",
    })
    assert "error" not in edit_result, edit_result

    # File actually written under the T2 user-home kb dir.
    fs = Path(store.kb_dir) / "user-note.md"
    assert fs.exists()
    assert "updated body" in fs.read_text()


# ── T1 path refusal — add / edit / delete ───────────────────────────────


def test_kb_add_t1_path_refused(monkeypatch, tmp_path):
    """kb.add to a T1 (platform) path is refused with a 403 DispatchError."""
    kb_in_platform = _point_kb_at_t1(monkeypatch, tmp_path)

    DispatchError = _get_dispatch_error_cls()
    with pytest.raises(DispatchError) as exc:
        kb_backend.handle_add({
            "path": "platform-note",
            "content": "# Hi\nshould be blocked",
            "description": "platform attempt",
        })
    assert exc.value.status_code == 403
    assert (
        "platform-protected" in str(exc.value)
        or "kb-change" in str(exc.value)
    )

    # File NOT written.
    target = kb_in_platform / "platform-note.md"
    assert not target.exists()

    # Audit row recorded.
    rows = _audit_events_for("t1_write_refused")
    assert any(
        (r.get("details") or {}).get("tool") == "kb.add" for r in rows
    ), rows


def test_kb_delete_t1_path_refused(monkeypatch, tmp_path):
    """kb.delete on a T1 path is refused.

    Pre-seed the file on disk so the only reason for refusal is the
    tier gate (the handler does NOT short-circuit on missing-entry; it
    calls store.delete_entry directly).
    """
    kb_in_platform = _point_kb_at_t1(monkeypatch, tmp_path)
    seeded = kb_in_platform / "protected.md"
    seeded.write_text("# Protected\nplatform content")

    DispatchError = _get_dispatch_error_cls()
    with pytest.raises(DispatchError) as exc:
        kb_backend.handle_delete({"path": "protected"})
    assert exc.value.status_code == 403

    # File still exists.
    assert seeded.exists()

    rows = _audit_events_for("t1_write_refused")
    assert any(
        (r.get("details") or {}).get("tool") == "kb.delete" for r in rows
    ), rows


def test_kb_edit_t1_path_refused(monkeypatch, tmp_path):
    """kb.edit on an existing T1 KB file is refused.

    Pre-seed the file so the handler reaches the tier gate (which sits
    after the entry-exists check).
    """
    kb_in_platform = _point_kb_at_t1(monkeypatch, tmp_path)
    seeded = kb_in_platform / "protected.md"
    seeded.write_text("# Protected\noriginal body")

    DispatchError = _get_dispatch_error_cls()
    with pytest.raises(DispatchError) as exc:
        kb_backend.handle_edit({
            "path": "protected",
            "content": "# Protected\nmodified body",
        })
    assert exc.value.status_code == 403

    # Content unchanged.
    assert "original body" in seeded.read_text()

    rows = _audit_events_for("t1_write_refused")
    assert any(
        (r.get("details") or {}).get("tool") == "kb.edit" for r in rows
    ), rows


# ── T0 path refusal ─────────────────────────────────────────────────────


def test_kb_add_t0_path_refused(monkeypatch, tmp_path):
    """kb.add resolving to a T0 (invisible) path is refused.

    Point the KB store at a directory matching the hardcoded T0
    ``*/secrets/*`` glob so the on-disk path classifies as T0.
    """
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    secrets_kb = tmp_path / "secrets" / "kb"
    secrets_kb.mkdir(parents=True)
    store = get_store()
    monkeypatch.setattr(store, "kb_dir", str(secrets_kb))

    DispatchError = _get_dispatch_error_cls()
    with pytest.raises(DispatchError) as exc:
        kb_backend.handle_add({
            "path": "invisible-note",
            "content": "# secret kb",
            "description": "should be invisible",
        })
    assert exc.value.status_code == 403
    assert "platform-invisible" in str(exc.value)

    assert not (secrets_kb / "invisible-note.md").exists()

    rows = _audit_events_for("t0_write_refused")
    assert any(
        (r.get("details") or {}).get("tool") == "kb.add" for r in rows
    ), rows
