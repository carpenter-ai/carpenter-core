"""Template-aware verification-arc creation (PR 7 close-out).

When the implementation arc was created via a yaml-change or kb-change
workflow, the correctness REVIEWER arc is swapped for a deterministic
Python-only verifier (``lint-yaml`` or ``verify-kb-format``).  The
default (``coding-change`` or missing) preserves the legacy
``verify-correctness`` REVIEWER arc.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest

from carpenter import config
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.arcs import verification as verification_arcs
from carpenter.core.workflows._arc_state import (
    get_arc_state, set_arc_state,
)


@pytest.fixture(autouse=True)
def _enable_verification(monkeypatch):
    monkeypatch.setitem(config.CONFIG, "verification", {"enabled": True})
    yield


def _make_impl_arc(name: str = "coding-change: do thing") -> int:
    parent = arc_manager.create_arc(name="parent", goal="parent")
    return arc_manager.create_arc(
        name=name,
        goal="do the thing",
        parent_id=parent,
        agent_type="EXECUTOR",
        integrity_level="trusted",
    )


# ── Workflow swap ────────────────────────────────────────────────────────


def test_yaml_change_swaps_correctness_for_lint_yaml():
    impl_id = _make_impl_arc()
    set_arc_state(impl_id, "_workflow_template", "yaml-change")

    v_ids = verification_arcs.create_verification_arcs(
        impl_id, require_completed=False,
    )
    names = sorted(arc_manager.get_arc(vid)["name"] for vid in v_ids)
    assert "lint-yaml" in names
    assert "verify-correctness" not in names


def test_kb_change_swaps_correctness_for_verify_kb_format():
    impl_id = _make_impl_arc()
    set_arc_state(impl_id, "_workflow_template", "kb-change")

    v_ids = verification_arcs.create_verification_arcs(
        impl_id, require_completed=False,
    )
    names = sorted(arc_manager.get_arc(vid)["name"] for vid in v_ids)
    assert "verify-kb-format" in names
    assert "verify-correctness" not in names


def test_coding_change_default_keeps_verify_correctness():
    impl_id = _make_impl_arc()
    # No _workflow_template set → default coding-change.

    v_ids = verification_arcs.create_verification_arcs(
        impl_id, require_completed=False,
    )
    names = sorted(arc_manager.get_arc(vid)["name"] for vid in v_ids)
    assert "verify-correctness" in names
    assert "lint-yaml" not in names
    assert "verify-kb-format" not in names


def test_explicit_coding_change_keeps_verify_correctness():
    impl_id = _make_impl_arc()
    set_arc_state(impl_id, "_workflow_template", "coding-change")

    v_ids = verification_arcs.create_verification_arcs(
        impl_id, require_completed=False,
    )
    names = sorted(arc_manager.get_arc(vid)["name"] for vid in v_ids)
    assert "verify-correctness" in names


def test_yaml_change_swapped_arc_has_expected_step_role():
    impl_id = _make_impl_arc()
    set_arc_state(impl_id, "_workflow_template", "yaml-change")

    v_ids = verification_arcs.create_verification_arcs(
        impl_id, require_completed=False,
    )
    lint_arcs = [
        arc_manager.get_arc(vid)
        for vid in v_ids
        if arc_manager.get_arc(vid)["name"] == "lint-yaml"
    ]
    assert len(lint_arcs) == 1
    assert lint_arcs[0]["step_role"] == "verifier-lint-yaml"
    assert lint_arcs[0]["arc_role"] == "verifier"


# ── Handler wrappers ─────────────────────────────────────────────────────


def _run(handler, arc_id):
    arc_info = arc_manager.get_arc(arc_id)
    asyncio.run(handler(arc_id, arc_info))


def test_handle_lint_yaml_step_passes_on_valid_yaml(tmp_path, monkeypatch):
    from carpenter.core.workflows import yaml_lint_handler

    # Stub workspace_manager.get_changed_files so we don't need a real
    # git workspace.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "good.yaml").write_text("a: 1\nb:\n  c: 2\n")

    from carpenter.core import workspace_manager
    monkeypatch.setattr(
        workspace_manager, "get_changed_files",
        lambda _ws: ["good.yaml"],
    )

    impl_id = _make_impl_arc()
    set_arc_state(impl_id, "workspace_path", str(workspace))

    # Build a verifier sibling pointing at impl_id.
    verifier_id = arc_manager.create_arc(
        name="lint-yaml",
        goal="lint",
        parent_id=arc_manager.get_arc(impl_id)["parent_id"],
        step_role="verifier-lint-yaml",
        arc_role="verifier",
        verification_target_id=impl_id,
        agent_type="EXECUTOR",
        integrity_level="trusted",
    )
    arc_manager.update_status(verifier_id, "active")

    _run(yaml_lint_handler.handle_lint_yaml_step, verifier_id)

    arc = arc_manager.get_arc(verifier_id)
    assert arc["status"] in ("completed", "frozen")
    result = get_arc_state(verifier_id, "_lint_yaml_result")
    assert result["ok"] is True


def test_handle_lint_yaml_step_fails_on_bad_yaml(tmp_path, monkeypatch):
    from carpenter.core.workflows import yaml_lint_handler

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "bad.yaml").write_text("a: [unterminated\n")

    from carpenter.core import workspace_manager
    monkeypatch.setattr(
        workspace_manager, "get_changed_files",
        lambda _ws: ["bad.yaml"],
    )

    impl_id = _make_impl_arc()
    set_arc_state(impl_id, "workspace_path", str(workspace))

    verifier_id = arc_manager.create_arc(
        name="lint-yaml",
        goal="lint",
        parent_id=arc_manager.get_arc(impl_id)["parent_id"],
        step_role="verifier-lint-yaml",
        arc_role="verifier",
        verification_target_id=impl_id,
        agent_type="EXECUTOR",
        integrity_level="trusted",
    )
    arc_manager.update_status(verifier_id, "active")

    _run(yaml_lint_handler.handle_lint_yaml_step, verifier_id)

    arc = arc_manager.get_arc(verifier_id)
    assert arc["status"] == "failed"
    result = get_arc_state(verifier_id, "_lint_yaml_result")
    assert result["ok"] is False


def test_handle_verify_kb_format_step_passes_on_valid_kb(tmp_path, monkeypatch):
    from carpenter.core.workflows import kb_format_handler

    workspace = tmp_path / "ws"
    (workspace / "kb").mkdir(parents=True)
    (workspace / "kb" / "valid-path.md").write_text("# Title\n\nbody\n")

    from carpenter.core import workspace_manager
    monkeypatch.setattr(
        workspace_manager, "get_changed_files",
        lambda _ws: ["kb/valid-path.md"],
    )

    impl_id = _make_impl_arc()
    set_arc_state(impl_id, "workspace_path", str(workspace))

    verifier_id = arc_manager.create_arc(
        name="verify-kb-format",
        goal="verify",
        parent_id=arc_manager.get_arc(impl_id)["parent_id"],
        step_role="verifier-kb-format",
        arc_role="verifier",
        verification_target_id=impl_id,
        agent_type="EXECUTOR",
        integrity_level="trusted",
    )
    arc_manager.update_status(verifier_id, "active")

    _run(kb_format_handler.handle_verify_kb_format_step, verifier_id)

    arc = arc_manager.get_arc(verifier_id)
    assert arc["status"] in ("completed", "frozen")
    result = get_arc_state(verifier_id, "_kb_format_result")
    assert result["ok"] is True


def test_handle_verify_kb_format_step_fails_on_bad_frontmatter(tmp_path, monkeypatch):
    from carpenter.core.workflows import kb_format_handler

    workspace = tmp_path / "ws"
    (workspace / "kb").mkdir(parents=True)
    # Bad frontmatter: opened but never closed.
    (workspace / "kb" / "notes.md").write_text(
        "---\nfoo: [unterminated\nstill no close\n"
    )

    from carpenter.core import workspace_manager
    monkeypatch.setattr(
        workspace_manager, "get_changed_files",
        lambda _ws: ["kb/notes.md"],
    )

    impl_id = _make_impl_arc()
    set_arc_state(impl_id, "workspace_path", str(workspace))

    verifier_id = arc_manager.create_arc(
        name="verify-kb-format",
        goal="verify",
        parent_id=arc_manager.get_arc(impl_id)["parent_id"],
        step_role="verifier-kb-format",
        arc_role="verifier",
        verification_target_id=impl_id,
        agent_type="EXECUTOR",
        integrity_level="trusted",
    )
    arc_manager.update_status(verifier_id, "active")

    _run(kb_format_handler.handle_verify_kb_format_step, verifier_id)

    arc = arc_manager.get_arc(verifier_id)
    assert arc["status"] == "failed"


def test_handle_step_handles_missing_workspace_gracefully(tmp_path, monkeypatch):
    from carpenter.core.workflows import yaml_lint_handler

    impl_id = _make_impl_arc()
    # workspace_path not set on impl arc.

    verifier_id = arc_manager.create_arc(
        name="lint-yaml",
        goal="lint",
        parent_id=arc_manager.get_arc(impl_id)["parent_id"],
        step_role="verifier-lint-yaml",
        arc_role="verifier",
        verification_target_id=impl_id,
        agent_type="EXECUTOR",
        integrity_level="trusted",
    )
    arc_manager.update_status(verifier_id, "active")

    _run(yaml_lint_handler.handle_lint_yaml_step, verifier_id)

    arc = arc_manager.get_arc(verifier_id)
    assert arc["status"] == "failed"
    result = get_arc_state(verifier_id, "_lint_yaml_result")
    assert result["ok"] is False


def test_handle_step_handles_missing_target_gracefully(tmp_path):
    from carpenter.core.workflows import yaml_lint_handler

    impl_id = _make_impl_arc()
    parent_id = arc_manager.get_arc(impl_id)["parent_id"]
    # No verification_target_id passed.
    verifier_id = arc_manager.create_arc(
        name="lint-yaml",
        goal="lint",
        parent_id=parent_id,
        step_role="verifier-lint-yaml",
        arc_role="verifier",
        agent_type="EXECUTOR",
        integrity_level="trusted",
    )
    arc_manager.update_status(verifier_id, "active")

    _run(yaml_lint_handler.handle_lint_yaml_step, verifier_id)

    arc = arc_manager.get_arc(verifier_id)
    assert arc["status"] == "failed"
