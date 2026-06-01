"""Workflow-template selection at arc-creation time (PR 5, Part C).

Exercises:

- :func:`carpenter.security.platform_paths.select_workflow_for_paths`
  most-restrictive ordering, ``force_human`` propagation, fallback to
  ``coding-change`` on empty input, and config-driven template
  selection.
- The chat-tool entry point
  :func:`carpenter.tool_backends.arc.handle_invoke_coding_change`
  records the chosen template, stores ``_review_mode = "human"`` on
  ``force_human``, and audits either ``integrity.workflow_selected`` or
  ``integrity.workflow_default_pending_classification`` to
  ``trust_audit_log`` so the integrity feed has the decision.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from carpenter import config as carpenter_config
from carpenter.core.engine import template_manager
from carpenter.core.trust import audit as trust_audit
from carpenter.security import platform_paths as pp


TEMPLATES_SRC = Path(__file__).resolve().parents[2] / "config_seed" / "templates"


def _set_repo_root(monkeypatch, root: str) -> None:
    monkeypatch.setitem(carpenter_config.CONFIG, "repo_dir", root)


def _set_carpenter_home(monkeypatch, home: str) -> None:
    monkeypatch.setitem(carpenter_config.CONFIG, "carpenter_home", home)


def _make_repo(tmp_path: Path) -> Path:
    """Create a minimal carpenter-repo tree under *tmp_path/repo*."""
    repo = tmp_path / "repo"
    (repo / "carpenter").mkdir(parents=True)
    (repo / "carpenter" / "__init__.py").write_text("")
    return repo


def _load_change_templates(tmp_path) -> None:
    """Copy + load the coding-change, yaml-change, kb-change templates so
    ``get_template_by_name`` can find them during the test."""
    dest = tmp_path / "templates"
    dest.mkdir(exist_ok=True)
    for name in ("coding-change.yaml", "yaml-change.yaml", "kb-change.yaml"):
        src = TEMPLATES_SRC / name
        shutil.copy(src, dest / name)
        template_manager.load_template(str(dest / name))


# ── select_workflow_for_paths combinations ──────────────────────────────


def test_select_mixed_python_yaml_picks_coding(monkeypatch, tmp_path: Path) -> None:
    """Mixed python + yaml under T2 → coding-change (python > yaml)."""
    _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    home = tmp_path / "home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))

    py = home / "thing.py"
    py.write_text("")
    yml = home / "thing.yaml"
    yml.write_text("")

    template, force_human = pp.select_workflow_for_paths([str(py), str(yml)])
    assert template == "coding-change"
    assert force_human is False


def test_select_all_yaml_picks_yaml_change(monkeypatch, tmp_path: Path) -> None:
    _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    home = tmp_path / "home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))

    files = [home / f"f{i}.yaml" for i in range(3)]
    for f in files:
        f.write_text("")

    template, force_human = pp.select_workflow_for_paths([str(f) for f in files])
    assert template == "yaml-change"
    assert force_human is False


def test_select_all_kb_picks_kb_change(monkeypatch, tmp_path: Path) -> None:
    _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    home = tmp_path / "home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))

    kb_dir = home / "kb"
    kb_dir.mkdir()
    files = [kb_dir / f"a{i}.md" for i in range(2)]
    for f in files:
        f.write_text("")

    template, force_human = pp.select_workflow_for_paths([str(f) for f in files])
    assert template == "kb-change"
    assert force_human is False


def test_select_t1_path_forces_human(monkeypatch, tmp_path: Path) -> None:
    """Any T1 path in the set must set force_human=True."""
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    home = tmp_path / "home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))

    # T1 hardcoded prefix: carpenter/ under repo root.
    t1_file = repo / "carpenter" / "thing.py"
    t1_file.write_text("")
    t2_file = home / "user.py"
    t2_file.write_text("")

    template, force_human = pp.select_workflow_for_paths([str(t1_file), str(t2_file)])
    # Python still wins category, force_human is True.
    assert template == "coding-change"
    assert force_human is True


def test_select_empty_paths_defaults_coding(monkeypatch, tmp_path: Path) -> None:
    """Empty path list → coding-change (PR-6 force-human gate covers the
    case where the agent decides files mid-loop)."""
    _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    template, force_human = pp.select_workflow_for_paths([])
    assert template == "coding-change"
    assert force_human is False


# ── Audit emission at arc-creation time ──────────────────────────────────


def _setup_for_invoke(monkeypatch, tmp_path: Path) -> Path:
    """Wire up the minimum config + filesystem for handle_invoke_coding_change."""
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    home = tmp_path / "home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))

    platform_dir = tmp_path / "source"
    platform_dir.mkdir()
    monkeypatch.setitem(carpenter_config.CONFIG, "platform_server_dir", str(platform_dir))
    _load_change_templates(tmp_path)

    # Default platform_integrity block keeps yaml→yaml-change, kb→kb-change.
    monkeypatch.setitem(
        carpenter_config.CONFIG,
        "platform_integrity",
        {
            "change_workflows": {
                "python": "coding-change",
                "yaml": "yaml-change",
                "kb": "kb-change",
                "unknown": "coding-change",
            },
            "path_overrides": [],
        },
    )
    return home


def test_handle_invoke_records_workflow_selected_audit(monkeypatch, tmp_path: Path) -> None:
    """Selecting yaml-change for T2 yaml paths emits the audit row with
    paths, tiers, categories, chosen_template, and force_human."""
    home = _setup_for_invoke(monkeypatch, tmp_path)
    p = home / "thing.yaml"
    p.write_text("")

    from carpenter.tool_backends import arc as arc_backend

    result = arc_backend.handle_invoke_coding_change({
        "source_dir": "platform",
        "prompt": "edit thing",
        "affected_paths": [str(p)],
    })
    assert "arc_id" in result

    events = trust_audit.get_trust_events(event_type="integrity.workflow_selected")
    assert events, "expected an integrity.workflow_selected audit row"
    details = events[0]["details"]
    assert details["chosen_template"] == "yaml-change"
    assert details["force_human"] is False
    assert details["paths"] == [str(p)]
    assert details["tiers"] == ["T2"]
    assert details["categories"] == ["yaml"]


def test_handle_invoke_default_pending_classification(monkeypatch, tmp_path: Path) -> None:
    """When affected_paths is empty, we default to coding-change and emit
    workflow_default_pending_classification so PR 6 can detect the case."""
    _setup_for_invoke(monkeypatch, tmp_path)
    from carpenter.tool_backends import arc as arc_backend

    result = arc_backend.handle_invoke_coding_change({
        "source_dir": "platform",
        "prompt": "do a thing, figure out files later",
    })
    assert "arc_id" in result

    events = trust_audit.get_trust_events(
        event_type="integrity.workflow_default_pending_classification",
    )
    assert events, "expected workflow_default_pending_classification audit row"
    details = events[0]["details"]
    assert details["chosen_template"] == "coding-change"
    assert details["force_human"] is False
    assert details["paths"] == []


def test_handle_invoke_force_human_stored(monkeypatch, tmp_path: Path) -> None:
    """When force_human is True the arc carries ``_review_mode = "human"`` in
    arc_state."""
    home = _setup_for_invoke(monkeypatch, tmp_path)
    # Path inside the carpenter/ T1 hardcoded prefix → forces human review.
    repo_root = carpenter_config.CONFIG["repo_dir"]
    t1 = Path(repo_root) / "carpenter" / "thing.py"
    t1.write_text("")

    from carpenter.tool_backends import arc as arc_backend
    from carpenter.core.workflows._arc_state import get_arc_state

    result = arc_backend.handle_invoke_coding_change({
        "source_dir": "platform",
        "prompt": "edit carpenter source",
        "affected_paths": [str(t1)],
    })
    arc_id = result["arc_id"]
    assert get_arc_state(arc_id, "_review_mode") == "human"
    assert get_arc_state(arc_id, "_workflow_template") == "coding-change"
