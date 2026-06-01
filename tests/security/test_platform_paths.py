"""Tests for ``carpenter.security.platform_paths``.

Covers hardcoded T0/T1/T2 classification, symlink escape resolution,
tier tie-breaking, user-config overrides (add but never demote), the
fail-closed exception path, change-category mapping, and workflow
selection.

All filesystem-touching tests use ``tmp_path``.  CONFIG is monkeypatched
in-place via ``monkeypatch.setitem`` so live re-reads (which is how the
module looks up overrides) pick up the changes.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from carpenter import config as carpenter_config
from carpenter.security import platform_paths as pp


# ── Helpers ──────────────────────────────────────────────────────────────


def _set_repo_root(monkeypatch, root: str) -> None:
    """Point the classifier at *root* as the carpenter repo root."""
    monkeypatch.setitem(carpenter_config.CONFIG, "repo_dir", root)


def _set_carpenter_home(monkeypatch, home: str) -> None:
    monkeypatch.setitem(carpenter_config.CONFIG, "carpenter_home", home)


def _set_overrides(monkeypatch, overrides) -> None:
    block = dict(carpenter_config.CONFIG.get("platform_integrity") or {})
    block["path_overrides"] = overrides
    monkeypatch.setitem(carpenter_config.CONFIG, "platform_integrity", block)


# ── Hardcoded T0 patterns ────────────────────────────────────────────────


@pytest.mark.parametrize("name", [
    ".env",
    ".env.local",
    "platform.db",
    "platform.db-wal",
    "platform.db-shm",
])
def test_t0_filename_patterns(monkeypatch, tmp_path: Path, name: str) -> None:
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    f = tmp_path / name
    f.write_text("")
    assert pp.path_tier(str(f)) == pp.PATH_TIER_T0


def test_t0_credentials_directory(monkeypatch, tmp_path: Path) -> None:
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    d = tmp_path / "credentials"
    d.mkdir()
    f = d / "anthropic.txt"
    f.write_text("")
    assert pp.path_tier(str(f)) == pp.PATH_TIER_T0


def test_t0_secrets_directory(monkeypatch, tmp_path: Path) -> None:
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    d = tmp_path / "secrets"
    d.mkdir()
    f = d / "service.json"
    f.write_text("")
    assert pp.path_tier(str(f)) == pp.PATH_TIER_T0


def test_t0_key_file_extension(monkeypatch, tmp_path: Path) -> None:
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    f = tmp_path / "rsa.key"
    f.write_text("")
    assert pp.path_tier(str(f)) == pp.PATH_TIER_T0


def test_t0_pem_file_extension(monkeypatch, tmp_path: Path) -> None:
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    f = tmp_path / "cert.pem"
    f.write_text("")
    assert pp.path_tier(str(f)) == pp.PATH_TIER_T0


def test_t0_token_suffix(monkeypatch, tmp_path: Path) -> None:
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    f = tmp_path / "api_token"
    f.write_text("")
    assert pp.path_tier(str(f)) == pp.PATH_TIER_T0


def test_t0_review_keys(monkeypatch, tmp_path: Path) -> None:
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    d = tmp_path / "review_keys"
    d.mkdir()
    f = d / "key.bin"
    f.write_text("")
    assert pp.path_tier(str(f)) == pp.PATH_TIER_T0


# ── Hardcoded T1 prefixes ────────────────────────────────────────────────


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


@pytest.mark.parametrize("relpath", [
    "carpenter/foo.py",
    "carpenter/subpkg/bar.py",
    "config_seed/something.yaml",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    ".github/workflows/ci.yml",
    ".forgejo/workflows/ci.yml",
    "tests/security/test_x.py",
    "tests/test_taint_invariants.py",
    "docs/coding-invariants.md",
    "docs/trust-invariants.md",
    "docs/security-model.md",
    "schema.sql",
    "db_migrations.py",
])
def test_t1_hardcoded_prefixes(monkeypatch, tmp_path: Path, relpath: str) -> None:
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("")
    assert pp.path_tier(str(target)) == pp.PATH_TIER_T1


# ── T2 fallback ──────────────────────────────────────────────────────────


def test_t2_carpenter_home_skill(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    home = tmp_path / "user_home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))
    f = home / "skills" / "foo.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("")
    assert pp.path_tier(str(f)) == pp.PATH_TIER_T2


def test_t2_unrelated_path(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    home = tmp_path / "user_home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))
    f = tmp_path / "elsewhere" / "note.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("")
    assert pp.path_tier(str(f)) == pp.PATH_TIER_T2


# ── Symlink escape ───────────────────────────────────────────────────────


def test_symlink_escape_resolves_to_t1(monkeypatch, tmp_path: Path) -> None:
    """A T2 symlink pointing into the carpenter T1 dir → T1."""
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    target = repo / "carpenter" / "platform.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("")
    home = tmp_path / "user_home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))
    link = home / "backdoor.py"
    os.symlink(str(target), str(link))
    assert pp.path_tier(str(link)) == pp.PATH_TIER_T1


# ── Tie-break: T0 > T1 ───────────────────────────────────────────────────


def test_t0_beats_t1(monkeypatch, tmp_path: Path) -> None:
    """A hypothetical ``<repo>/carpenter/.env`` should classify T0."""
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    f = repo / "carpenter" / ".env"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("")
    assert pp.path_tier(str(f)) == pp.PATH_TIER_T0


# ── User-config additions ────────────────────────────────────────────────


def test_user_override_adds_t1(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    extra = tmp_path / "extra_protected"
    extra.mkdir()
    f = extra / "secret_logic.py"
    f.write_text("")
    _set_overrides(monkeypatch, [{"prefix": str(extra), "tier": "T1"}])
    assert pp.path_tier(str(f)) == pp.PATH_TIER_T1


def test_user_override_adds_t0(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    extra = tmp_path / "secret_store"
    extra.mkdir()
    f = extra / "data.bin"
    f.write_text("")
    _set_overrides(monkeypatch, [{"prefix": str(extra), "tier": "T0"}])
    assert pp.path_tier(str(f)) == pp.PATH_TIER_T0


def test_user_override_cannot_demote_t1(monkeypatch, tmp_path: Path) -> None:
    """A user T2 override over hardcoded T1 must NOT demote — T1 wins."""
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    pkg_file = repo / "carpenter" / "core.py"
    pkg_file.parent.mkdir(parents=True, exist_ok=True)
    pkg_file.write_text("")
    _set_overrides(
        monkeypatch,
        [{"prefix": str(repo / "carpenter"), "tier": "T2"}],
    )
    assert pp.path_tier(str(pkg_file)) == pp.PATH_TIER_T1


def test_user_override_invalid_entries_ignored(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    home = tmp_path / "user_home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))
    f = home / "ok.txt"
    f.write_text("")
    _set_overrides(monkeypatch, [
        {"prefix": "/no/tier"},
        {"tier": "T1"},
        "not-a-dict",
        {"prefix": str(home), "tier": "T9"},  # bogus tier
    ])
    # All invalid entries dropped; classification falls back to T2.
    assert pp.path_tier(str(f)) == pp.PATH_TIER_T2


# ── Fail-closed on exception ─────────────────────────────────────────────


def test_realpath_exception_returns_t1(monkeypatch) -> None:
    """If realpath() raises, classification fails-closed to T1."""
    def _boom(_):
        raise OSError("synthetic")
    monkeypatch.setattr(os.path, "realpath", _boom)
    assert pp.path_tier("/anything") == pp.PATH_TIER_T1


# ── Category mapping ─────────────────────────────────────────────────────


def test_category_python() -> None:
    assert pp.change_category("/anywhere/foo.py") == "python"


def test_category_yaml() -> None:
    assert pp.change_category("/anywhere/foo.yaml") == "yaml"


def test_category_yml() -> None:
    assert pp.change_category("/anywhere/foo.yml") == "yaml"


def test_category_kb_md(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    # Path with /kb/ segment
    assert pp.change_category("/home/user/kb/article.md") == "kb"


def test_category_docs_md(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    f = repo / "docs" / "guide.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("")
    assert pp.change_category(str(f)) == "kb"


def test_category_unknown_json() -> None:
    assert pp.change_category("/anywhere/data.json") == "unknown"


def test_category_unknown_no_extension() -> None:
    assert pp.change_category("/anywhere/Makefile") == "unknown"


# ── is_invisible ─────────────────────────────────────────────────────────


def test_is_invisible_true_for_t0(monkeypatch, tmp_path: Path) -> None:
    _set_repo_root(monkeypatch, str(tmp_path / "repo"))
    f = tmp_path / ".env"
    f.write_text("")
    assert pp.is_invisible(str(f)) is True


def test_is_invisible_false_for_t1(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    f = repo / "carpenter" / "x.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("")
    assert pp.is_invisible(str(f)) is False


def test_is_invisible_false_for_t2(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    home = tmp_path / "user_home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))
    f = home / "doc.txt"
    f.write_text("")
    assert pp.is_invisible(str(f)) is False


# ── select_workflow_for_paths ────────────────────────────────────────────


def test_workflow_all_python_t2(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    home = tmp_path / "user_home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))
    p1 = home / "a.py"
    p2 = home / "b.py"
    for p in (p1, p2):
        p.write_text("")
    template, force_human = pp.select_workflow_for_paths([str(p1), str(p2)])
    assert template == "coding-change"
    assert force_human is False


def test_workflow_mixed_python_yaml_t2(monkeypatch, tmp_path: Path) -> None:
    """python wins over yaml in most-restrictive order → coding-change."""
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    home = tmp_path / "user_home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))
    p1 = home / "a.py"
    p2 = home / "b.yaml"
    for p in (p1, p2):
        p.write_text("")
    template, force_human = pp.select_workflow_for_paths([str(p1), str(p2)])
    assert template == "coding-change"
    assert force_human is False


def test_workflow_t1_python_forces_human(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    f = repo / "carpenter" / "x.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("")
    template, force_human = pp.select_workflow_for_paths([str(f)])
    assert template == "coding-change"
    assert force_human is True


def test_workflow_all_yaml_t2(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    home = tmp_path / "user_home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))
    p = home / "config.yaml"
    p.write_text("")
    template, force_human = pp.select_workflow_for_paths([str(p)])
    assert template == "yaml-change"
    assert force_human is False


def test_workflow_all_kb_t2(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    home = tmp_path / "user_home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))
    p = home / "kb" / "article.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    template, force_human = pp.select_workflow_for_paths([str(p)])
    assert template == "kb-change"
    assert force_human is False


def test_workflow_unknown_t2_maps_to_coding_change(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    home = tmp_path / "user_home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))
    p = home / "data.json"
    p.write_text("")
    template, force_human = pp.select_workflow_for_paths([str(p)])
    assert template == "coding-change"
    assert force_human is False


def test_workflow_config_override_template_name(monkeypatch, tmp_path: Path) -> None:
    """User config can rename the template for a category."""
    repo = _make_repo(tmp_path)
    _set_repo_root(monkeypatch, str(repo))
    home = tmp_path / "user_home"
    home.mkdir()
    _set_carpenter_home(monkeypatch, str(home))
    block = dict(carpenter_config.CONFIG.get("platform_integrity") or {})
    workflows = dict(block.get("change_workflows") or {})
    workflows["yaml"] = "my-custom-yaml-flow"
    block["change_workflows"] = workflows
    monkeypatch.setitem(carpenter_config.CONFIG, "platform_integrity", block)
    p = home / "config.yaml"
    p.write_text("")
    template, force_human = pp.select_workflow_for_paths([str(p)])
    assert template == "my-custom-yaml-flow"


# ── Audit wrapper (best-effort, never crashes caller) ────────────────────


def test_audit_path_decision_swallows_import_errors(monkeypatch) -> None:
    """If the audit module raises, audit_path_decision logs and continues."""
    def _raise(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")
    # Patch the audit module attribute directly.
    monkeypatch.setattr(
        "carpenter.core.trust.audit.log_trust_event",
        _raise,
    )
    # Should not raise.
    pp.audit_path_decision(None, "test_event", "/some/path", {"k": "v"})


def test_audit_path_decision_namespace_prefix(monkeypatch) -> None:
    """Event types get an ``integrity.`` prefix if absent."""
    seen: list[tuple] = []
    def _capture(arc_id, event_type, details):
        seen.append((arc_id, event_type, details))
        return 1
    monkeypatch.setattr(
        "carpenter.core.trust.audit.log_trust_event",
        _capture,
    )
    pp.audit_path_decision(42, "tier_decision", "/x/y", {"tier": "T1"})
    assert len(seen) == 1
    arc_id, evt, details = seen[0]
    assert arc_id == 42
    assert evt == "integrity.tier_decision"
    assert details.get("path") == "/x/y"
    assert details.get("tier") == "T1"


def test_audit_path_decision_keeps_existing_namespace(monkeypatch) -> None:
    """An event already prefixed with ``integrity.`` is not double-prefixed."""
    seen: list[tuple] = []
    def _capture(arc_id, event_type, details):
        seen.append((arc_id, event_type, details))
        return 1
    monkeypatch.setattr(
        "carpenter.core.trust.audit.log_trust_event",
        _capture,
    )
    pp.audit_path_decision(None, "integrity.already", "/x", {})
    assert seen[0][1] == "integrity.already"
