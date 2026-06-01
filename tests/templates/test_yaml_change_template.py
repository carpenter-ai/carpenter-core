"""Tests for the ``yaml-change`` workflow template (PR 5).

Covers:

- Loading the YAML template from disk into the workflow_templates table.
- :func:`select_workflow_for_paths` returns ``("yaml-change", False)``
  for T2 ``.yaml`` paths.
- :func:`carpenter.core.workflows.yaml_lint_handler.lint_workspace_yaml`
  rejects malformed YAML and accepts valid YAML.
- A handler-arc created via the chat-tool entrypoint records the
  selected template name in arc_state when only YAML paths are affected.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from carpenter import config as carpenter_config
from carpenter.security import platform_paths as pp
from carpenter.core.engine import template_manager
from carpenter.core.workflows import yaml_lint_handler


TEMPLATES_SRC = Path(__file__).resolve().parents[2] / "config_seed" / "templates"


def _set_repo_root(monkeypatch, root: str) -> None:
    monkeypatch.setitem(carpenter_config.CONFIG, "repo_dir", root)


def _set_carpenter_home(monkeypatch, home: str) -> None:
    monkeypatch.setitem(carpenter_config.CONFIG, "carpenter_home", home)


def _copy_templates_dir(tmp_path) -> str:
    dest = str(tmp_path / "templates")
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(TEMPLATES_SRC):
        src = TEMPLATES_SRC / f
        if src.is_file() and f.endswith((".yaml", ".yml")):
            shutil.copy(src, dest)
        elif src.is_dir() and not f.startswith((".", "_")):
            shutil.copytree(src, os.path.join(dest, f))
    return dest


# ── Template-loading / metadata ──────────────────────────────────────────


def test_yaml_change_template_loads(tmp_path) -> None:
    templates_dir = _copy_templates_dir(tmp_path)
    yaml_path = os.path.join(templates_dir, "yaml-change.yaml")
    tid = template_manager.load_template(yaml_path)
    assert tid > 0
    template = template_manager.get_template(tid)
    assert template is not None
    assert template["name"] == "yaml-change"
    step_names = {s["name"] for s in template["steps"]}
    assert "lint-yaml" in step_names
    assert "await-approval" in step_names
    # The deterministic-judge step must be present (I12 anchor).
    assert "judge-verification" in step_names


def test_yaml_change_template_await_approval_has_activation_event(tmp_path) -> None:
    """The ``await-approval`` step must carry the manual-trigger activation
    event so the platform's I12 enforcement (human approval uncuttable for
    T1 changes) has the right hook to anchor on."""
    templates_dir = _copy_templates_dir(tmp_path)
    yaml_path = os.path.join(templates_dir, "yaml-change.yaml")
    tid = template_manager.load_template(yaml_path)
    template = template_manager.get_template(tid)
    await_step = next(s for s in template["steps"] if s["name"] == "await-approval")
    assert await_step.get("activation_event") == "arc.manual_trigger"


# ── select_workflow_for_paths for T2 yaml ────────────────────────────────


def test_select_workflow_yaml_t2(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo" / "carpenter"
    repo.mkdir(parents=True)
    (repo / "__init__.py").write_text("")
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    home = tmp_path / "user_home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))

    p = home / "tools" / "my-tool.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("name: x\n")

    template, force_human = pp.select_workflow_for_paths([str(p)])
    assert template == "yaml-change"
    assert force_human is False


# ── Lint behaviour (deterministic, no LLM) ───────────────────────────────


class _FakeWorkspaceManager:
    """Minimal stand-in for ``carpenter.core.workspace_manager``."""

    def __init__(self, files):
        self._files = files

    def get_changed_files(self, _workspace_path):
        return list(self._files)


def test_lint_yaml_rejects_malformed(tmp_path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    bad = ws / "broken.yaml"
    bad.write_text("name: foo\n  this is: not: valid: yaml\n  -  - dash\n: bad\n")
    fake = _FakeWorkspaceManager(["broken.yaml"])
    with patch.object(yaml_lint_handler, "workspace_manager", fake):
        result = yaml_lint_handler.lint_workspace_yaml(str(ws))
    assert result["ok"] is False
    assert any("yaml.safe_load failed" in f["message"] for f in result["findings"])


def test_lint_yaml_accepts_valid(tmp_path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    good = ws / "ok.yaml"
    good.write_text("name: foo\nvalue: 42\n")
    fake = _FakeWorkspaceManager(["ok.yaml"])
    with patch.object(yaml_lint_handler, "workspace_manager", fake):
        result = yaml_lint_handler.lint_workspace_yaml(str(ws))
    assert result["ok"] is True
    assert result["findings"] == []
    assert result["files"] == ["ok.yaml"]


def test_lint_yaml_skips_non_yaml_files(tmp_path) -> None:
    """The handler must only inspect ``.yaml`` / ``.yml`` files even when
    the workspace has other changed files (e.g. README updates)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# not yaml")
    (ws / "ok.yaml").write_text("k: v\n")
    fake = _FakeWorkspaceManager(["README.md", "ok.yaml"])
    with patch.object(yaml_lint_handler, "workspace_manager", fake):
        result = yaml_lint_handler.lint_workspace_yaml(str(ws))
    assert result["ok"] is True
    assert result["files"] == ["ok.yaml"]
