"""Platform-integrity tier enforcement in the files.* tool backend.

Covers the T0/T1/T2 gates added by PR 3 of the platform-integrity
rollout.  The companion classifier tests live in
``tests/security/test_platform_paths.py``; this file pins the
enforcement wiring inside ``carpenter.tool_backends.files``.

Notes on fixtures:

* The autouse ``test_db`` fixture in ``tests/conftest.py`` already
  points ``CONFIG['base_dir']`` and ``CONFIG['workspaces_dir']`` at the
  per-test ``tmp_path``, so paths under ``tmp_path`` classify as T2
  (user home).  We pin ``CONFIG['repo_dir']`` to a path inside
  ``tmp_path`` so the T1 prefix list (``carpenter/``, ``config_seed/``,
  …) maps into the temp tree without affecting the real repo.
* ``CONFIG['platform_integrity']`` is left absent so only the
  hardcoded T0/T1 floors apply — that is what the production gate uses.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from carpenter import config as carpenter_config
from carpenter.core.trust.audit import get_trust_events
from carpenter.tool_backends import files as files_backend


# ── Helpers ──────────────────────────────────────────────────────────────


def _set_repo_root(monkeypatch, root: str) -> None:
    """Pin the classifier's repo root inside the per-test tmp_path."""
    monkeypatch.setitem(carpenter_config.CONFIG, "repo_dir", root)


def _get_dispatch_error_cls():
    from carpenter.executor.dispatch_bridge import DispatchError
    return DispatchError


def _audit_events_for(event_type: str):
    """Return all audit rows with the ``integrity.<event_type>`` prefix."""
    return get_trust_events(event_type=f"integrity.{event_type}", limit=100)


def _make_trusted_arc(create_arc) -> int:
    """Create a trusted arc (default integrity_level)."""
    return create_arc("trusted-caller")


def _make_untrusted_arc() -> int:
    """Create an untrusted arc by direct insertion (bypasses public guard)."""
    from carpenter.core.arcs import manager as arc_manager
    return arc_manager._insert_arc(
        name="untrusted-caller",
        parent_id=None,
        integrity_level="untrusted",
    )


# ── T0 read refusal ──────────────────────────────────────────────────────


def test_t0_read_via_handle_read_raises_403(monkeypatch, tmp_path):
    """A T0 .env path is refused by handle_read with a 403 DispatchError."""
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=hunter2")

    DispatchError = _get_dispatch_error_cls()
    with pytest.raises(DispatchError) as exc:
        files_backend.handle_read({"path": str(env_file)})
    assert exc.value.status_code == 403
    assert "T0" in str(exc.value) or "platform-invisible" in str(exc.value)

    rows = _audit_events_for("t0_read_refused")
    assert len(rows) >= 1
    assert rows[0]["details"]["tool"] == "files.read"


def test_t0_read_via_chat_returns_denial(monkeypatch, tmp_path):
    """``chat_read_provenance_check`` denies T0 without echoing bytes."""
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=hunter2")

    refusal = files_backend.chat_read_provenance_check(str(env_file))
    assert refusal is not None
    assert "hunter2" not in refusal
    assert "denied" in refusal.lower() or "invisible" in refusal.lower()


# ── T0 / T1 write refusal ────────────────────────────────────────────────


def test_t0_write_trusted_caller_refused(monkeypatch, tmp_path, create_arc):
    """Trusted callers cannot write to T0 paths via files.write."""
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    arc_id = _make_trusted_arc(create_arc)
    target = tmp_path / ".env"  # T0

    DispatchError = _get_dispatch_error_cls()
    with pytest.raises(DispatchError) as exc:
        files_backend.handle_write({
            "path": str(target),
            "content": "x",
            "_caller_arc_id": arc_id,
        })
    assert exc.value.status_code == 403
    assert "T0" in str(exc.value) or "platform-invisible" in str(exc.value)
    assert not target.exists()


def test_t0_write_untrusted_caller_refused(monkeypatch, tmp_path):
    """Untrusted callers also see a T0 denial (the workspace allowlist
    catches them first, but T0 paths are never workspace paths so this
    is the visible signal)."""
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    arc_id = _make_untrusted_arc()
    target = tmp_path / ".env"  # T0 and not inside the workspace

    DispatchError = _get_dispatch_error_cls()
    with pytest.raises(DispatchError) as exc:
        files_backend.handle_write({
            "path": str(target),
            "content": "x",
            "_caller_arc_id": arc_id,
        })
    assert exc.value.status_code == 403
    # The workspace-allowlist message is "non-trusted arcs may only
    # write inside their own workspace"; the T0 message is "platform-
    # invisible".  Either order is acceptable so long as the write
    # is refused and the file is not written.
    assert not target.exists()


def test_t1_write_trusted_caller_refused(monkeypatch, tmp_path, create_arc):
    """T1 writes by trusted callers are gated — the new I12 enforcement."""
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    repo = tmp_path / "repo" / "carpenter"
    repo.mkdir(parents=True)
    arc_id = _make_trusted_arc(create_arc)
    target = repo / "patched.py"

    DispatchError = _get_dispatch_error_cls()
    with pytest.raises(DispatchError) as exc:
        files_backend.handle_write({
            "path": str(target),
            "content": "x",
            "_caller_arc_id": arc_id,
        })
    assert exc.value.status_code == 403
    assert "T1" in str(exc.value) or "coding-change" in str(exc.value)
    assert not target.exists()

    rows = _audit_events_for("t1_write_refused")
    assert len(rows) >= 1


def test_t1_write_chat_context_refused(monkeypatch, tmp_path):
    """Chat-context writes (no caller arc) also fail on T1.  Before this
    PR these were allowed — this test pins the regression boundary."""
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    repo = tmp_path / "repo" / "carpenter"
    repo.mkdir(parents=True)
    target = repo / "patched.py"

    DispatchError = _get_dispatch_error_cls()
    with pytest.raises(DispatchError) as exc:
        files_backend.handle_write({"path": str(target), "content": "x"})
    assert exc.value.status_code == 403
    assert not target.exists()


# ── T2 write still works ────────────────────────────────────────────────


def test_t2_write_trusted_caller_proceeds(monkeypatch, tmp_path, create_arc):
    """T2 writes by trusted callers still work — the gate must not regress
    ordinary file ops.  This is the highest-risk test in this PR."""
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    arc_id = _make_trusted_arc(create_arc)
    target = tmp_path / "user-doc.txt"

    result = files_backend.handle_write({
        "path": str(target),
        "content": "hello",
        "_caller_arc_id": arc_id,
    })
    assert result == {"success": True}
    assert target.read_text() == "hello"


def test_t2_read_trusted_caller_proceeds(monkeypatch, tmp_path, create_arc):
    """A trusted-caller read of a normal T2 file under tmp_path succeeds."""
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    arc_id = _make_trusted_arc(create_arc)
    target = tmp_path / "user-doc.txt"
    target.write_text("hello world")

    result = files_backend.handle_read({
        "path": str(target),
        "_caller_arc_id": arc_id,
    })
    assert result == {"content": "hello world"}


# ── Symlink hardening ────────────────────────────────────────────────────


def test_symlink_into_t0_refused(monkeypatch, tmp_path):
    """A T2 symlink pointing at a real .env file is refused.  Tier
    classification uses realpath() so the symlink target tier wins."""
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    real_env = tmp_path / ".env"
    real_env.write_text("SECRET=hunter2")
    link = tmp_path / "innocuous.txt"
    link.symlink_to(real_env)

    DispatchError = _get_dispatch_error_cls()
    with pytest.raises(DispatchError) as exc:
        files_backend.handle_read({"path": str(link)})
    assert exc.value.status_code == 403


def test_symlink_into_t1_write_refused(monkeypatch, tmp_path, create_arc):
    """A T2 symlink pointing into the platform tree is refused on write —
    realpath resolution makes the target T1."""
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    repo_carp = tmp_path / "repo" / "carpenter"
    repo_carp.mkdir(parents=True)
    # The symlink target need not pre-exist; handle_write resolves the
    # realpath through the parent, which IS the platform tree.
    arc_id = _make_trusted_arc(create_arc)
    link_target = repo_carp / "victim.py"
    link = tmp_path / "innocuous.py"
    link.symlink_to(link_target)

    DispatchError = _get_dispatch_error_cls()
    with pytest.raises(DispatchError) as exc:
        files_backend.handle_write({
            "path": str(link),
            "content": "x",
            "_caller_arc_id": arc_id,
        })
    assert exc.value.status_code == 403
    assert not link_target.exists()


# ── Listing filters T0 entries and does not leak names ──────────────────


def test_list_strips_t0_entries(monkeypatch, tmp_path):
    """handle_list filters out T0 entries (.env etc) from a directory."""
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    listing = tmp_path / "listing"
    listing.mkdir()
    (listing / "visible.txt").write_text("a")
    (listing / ".env").write_text("SECRET=hunter2")

    result = files_backend.handle_list({"dir": str(listing)})
    assert "visible.txt" in result["files"]
    assert ".env" not in result["files"]


def test_list_audit_row_does_not_leak_names(monkeypatch, tmp_path):
    """The listing_filtered audit row must NOT include the filtered
    filenames — leaking them defeats the point of T0 invisibility."""
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    listing = tmp_path / "listing"
    listing.mkdir()
    (listing / "visible.txt").write_text("a")
    (listing / ".env").write_text("SECRET=hunter2")
    secret_name = ".env"

    files_backend.handle_list({"dir": str(listing)})

    rows = _audit_events_for("listing_filtered")
    assert len(rows) >= 1
    for row in rows:
        details = row.get("details") or {}
        # No key or value in details may contain the secret filename.
        for k, v in details.items():
            assert secret_name not in str(k), (
                f"audit key {k!r} leaks secret filename"
            )
            # `path` is the dir itself, which might end in "/" + a
            # parent named .env in pathological setups — but our tmp
            # parent is not named .env, so this is safe.
            if isinstance(v, str):
                # The `dir` field is the directory path, which is
                # tmp_path; it must not contain the secret filename.
                assert secret_name not in v.split(os.sep)[-1:], (
                    f"audit value {v!r} leaks secret filename basename"
                )


def test_file_count_excludes_t0(monkeypatch, tmp_path):
    """handle_file_count counts post-filter entries only."""
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    # Use a fresh subdir so we don't see the autouse test_db fixture's
    # template database file mixed into the listing.
    listing = tmp_path / "listing"
    listing.mkdir()
    (listing / "a.txt").write_text("a")
    (listing / "b.txt").write_text("b")
    (listing / ".env").write_text("SECRET=hunter2")

    result = files_backend.handle_file_count({"directory": str(listing)})
    assert result["file_count"] == 2  # .env excluded
