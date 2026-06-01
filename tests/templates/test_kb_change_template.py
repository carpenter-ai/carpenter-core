"""Tests for the ``kb-change`` workflow template (PR 5).

Covers:

- Loading the YAML template from disk into the workflow_templates table.
- :func:`select_workflow_for_paths` returns ``("kb-change", False)``
  for T2 ``.md`` paths under a ``kb/`` directory.
- :func:`carpenter.core.workflows.kb_format_handler.check_workspace_kb`
  rejects malformed frontmatter and accepts valid KB content.
- An invalid KB path-regex is reported as a finding.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import patch

from carpenter import config as carpenter_config
from carpenter.security import platform_paths as pp
from carpenter.core.engine import template_manager
from carpenter.core.workflows import kb_format_handler


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


def test_kb_change_template_loads(tmp_path) -> None:
    templates_dir = _copy_templates_dir(tmp_path)
    yaml_path = os.path.join(templates_dir, "kb-change.yaml")
    tid = template_manager.load_template(yaml_path)
    assert tid > 0
    template = template_manager.get_template(tid)
    assert template is not None
    assert template["name"] == "kb-change"
    step_names = {s["name"] for s in template["steps"]}
    assert "verify-kb-format" in step_names
    assert "judge-verification" in step_names
    assert "await-approval" in step_names


def test_kb_change_template_await_approval_has_activation_event(tmp_path) -> None:
    templates_dir = _copy_templates_dir(tmp_path)
    yaml_path = os.path.join(templates_dir, "kb-change.yaml")
    tid = template_manager.load_template(yaml_path)
    template = template_manager.get_template(tid)
    await_step = next(s for s in template["steps"] if s["name"] == "await-approval")
    assert await_step.get("activation_event") == "arc.manual_trigger"


# ── select_workflow_for_paths for T2 kb ──────────────────────────────────


def test_select_workflow_kb_t2(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo" / "carpenter"
    repo.mkdir(parents=True)
    (repo / "__init__.py").write_text("")
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    home = tmp_path / "user_home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))

    kb = home / "kb" / "general" / "topic.md"
    kb.parent.mkdir(parents=True)
    kb.write_text("# Note\n")

    template, force_human = pp.select_workflow_for_paths([str(kb)])
    assert template == "kb-change"
    assert force_human is False


# ── KB-format handler behaviour ──────────────────────────────────────────


class _FakeWorkspaceManager:
    def __init__(self, files):
        self._files = files

    def get_changed_files(self, _workspace_path):
        return list(self._files)


def test_kb_format_rejects_malformed_frontmatter(tmp_path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "kb" / "topic.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\nfoo: [unterminated\nbar: 1\n---\n# Body\n")
    fake = _FakeWorkspaceManager(["kb/topic.md"])
    with patch.object(kb_format_handler, "workspace_manager", fake):
        result = kb_format_handler.check_workspace_kb(str(ws))
    assert result["ok"] is False
    assert any("Frontmatter YAML parse error" in f["message"] for f in result["findings"])


def test_kb_format_accepts_valid(tmp_path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "kb" / "valid-topic.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\ntitle: hello\ndescription: a note\n---\n# Hello\n\nBody\n")
    fake = _FakeWorkspaceManager(["kb/valid-topic.md"])
    with patch.object(kb_format_handler, "workspace_manager", fake):
        result = kb_format_handler.check_workspace_kb(str(ws))
    assert result["ok"] is True, result["findings"]
    assert result["files"] == ["kb/valid-topic.md"]


def test_kb_format_rejects_invalid_path_regex(tmp_path) -> None:
    """Uppercase letters / spaces in KB path must be flagged."""
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "kb" / "Topic With Spaces.md"
    target.parent.mkdir(parents=True)
    target.write_text("# body\n")
    fake = _FakeWorkspaceManager(["kb/Topic With Spaces.md"])
    with patch.object(kb_format_handler, "workspace_manager", fake):
        result = kb_format_handler.check_workspace_kb(str(ws))
    assert result["ok"] is False
    assert any("does not match required regex" in f["message"] for f in result["findings"])


def test_kb_format_no_frontmatter_is_accepted(tmp_path) -> None:
    """KB files without frontmatter are still valid (frontmatter is optional)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "kb" / "plain.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Plain body\n\nNo frontmatter here.\n")
    fake = _FakeWorkspaceManager(["kb/plain.md"])
    with patch.object(kb_format_handler, "workspace_manager", fake):
        result = kb_format_handler.check_workspace_kb(str(ws))
    assert result["ok"] is True, result["findings"]
