"""Tests for carpenter.kb.autogen — auto-generation pipeline + change processing."""

import os
from pathlib import Path
from unittest.mock import patch

from carpenter.kb.autogen import (
    _load_theme_map,
    get_theme_map,
    _BUILTIN_THEME_MAP,
    _scan_tool_dir,
    scan_tools,
    scan_config,
    scan_templates,
    run_autogen,
    check_source_hashes,
    process_change_queue,
    _mtime_poll_and_process,
    _mtime_cache,
    _update_source_hash,
)
from carpenter.kb.store import KBStore
from carpenter.db import get_db


class TestGetThemeMap:
    def test_returns_dict(self):
        result = get_theme_map()
        assert isinstance(result, dict)
        assert "scheduling" in result
        assert result["scheduling"] == "scheduling/tools"

    def test_backwards_compat_alias(self):
        """_load_theme_map still works as an alias for get_theme_map."""
        result = _load_theme_map()
        assert isinstance(result, dict)
        assert "scheduling" in result

    def test_builtin_map_contains_expected_keys(self):
        """The built-in map should have all known tool modules."""
        assert "scheduling" in _BUILTIN_THEME_MAP
        assert "git" in _BUILTIN_THEME_MAP
        assert "kb" in _BUILTIN_THEME_MAP
        assert "credentials" in _BUILTIN_THEME_MAP

    def test_fallback_to_defaults(self, tmp_path):
        """When YAML file missing, returns built-in defaults."""
        with patch("carpenter.kb.autogen.Path") as mock_path:
            # Make the yaml_path point to non-existent file
            mock_path.return_value.__truediv__ = lambda self, x: tmp_path / "nonexistent.yaml"
            # Should still work (falls back to built-in)
            result = get_theme_map()
            assert isinstance(result, dict)

    def test_config_overrides_builtin(self):
        """Config kb.theme_map overrides built-in values."""
        override = {"scheduling": "custom/scheduling", "new_module": "custom/new"}
        with patch("carpenter.kb.autogen.config") as mock_config:
            mock_config.CONFIG = {"kb": {"theme_map": override}}
            result = get_theme_map()
            assert result["scheduling"] == "custom/scheduling"
            assert result["new_module"] == "custom/new"
            # Non-overridden keys still present from built-in
            assert result["git"] == "git/tools"

    def test_empty_config_returns_builtins(self):
        """Empty config override dict changes nothing."""
        with patch("carpenter.kb.autogen.config") as mock_config:
            mock_config.CONFIG = {"kb": {"theme_map": {}}}
            result = get_theme_map()
            assert result == _BUILTIN_THEME_MAP


class TestScanToolDir:
    def test_scan_real_act_dir(self):
        """Scan the actual act/ directory — should find tools."""
        from carpenter.kb.autogen import _REPO_ROOT
        act_dir = _REPO_ROOT / "carpenter_tools" / "act"
        tools = _scan_tool_dir(act_dir)
        assert len(tools) > 0
        # Should find scheduling.add_once
        names = {(t["module"], t["name"]) for t in tools}
        assert ("scheduling", "add_once") in names

    def test_scan_real_read_dir(self):
        """Scan the actual read/ directory — should find tools."""
        from carpenter.kb.autogen import _REPO_ROOT
        read_dir = _REPO_ROOT / "carpenter_tools" / "read"
        tools = _scan_tool_dir(read_dir)
        assert len(tools) > 0

    def test_scan_nonexistent_dir(self, tmp_path):
        result = _scan_tool_dir(tmp_path / "nonexistent")
        assert result == []

    def test_tool_has_expected_fields(self):
        from carpenter.kb.autogen import _REPO_ROOT
        act_dir = _REPO_ROOT / "carpenter_tools" / "act"
        tools = _scan_tool_dir(act_dir)
        for tool in tools:
            assert "module" in tool
            assert "name" in tool
            assert "args" in tool
            assert "docline" in tool
            assert "source_file" in tool


class TestScanTools:
    def test_returns_kb_paths(self):
        result = scan_tools()
        assert isinstance(result, dict)
        # Should have entries for known tool modules
        assert any("scheduling" in k for k in result.keys())

    def test_entries_are_markdown(self):
        result = scan_tools()
        for kb_path, content in result.items():
            assert content.startswith("#"), f"{kb_path} should start with heading"

    def test_groups_by_theme(self):
        result = scan_tools()
        # scheduling module maps to scheduling/tools
        assert "scheduling/tools" in result


class TestScanConfig:
    def test_returns_config_reference(self):
        result = scan_config()
        assert "self-modification/config-reference" in result
        content = result["self-modification/config-reference"]
        assert "Configuration Reference" in content
        assert "base_dir" in content


class TestScanTemplates:
    def test_returns_template_entry(self):
        result = scan_templates()
        assert "arcs/templates" in result
        content = result["arcs/templates"]
        assert "Workflow Templates" in content
        # Should list the coding-change template
        assert "coding-change" in content


class TestRunAutogen:
    def test_first_run_generates_entries(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)

        result = run_autogen(store)
        assert result["generated"] > 0

        # Verify entries were written
        entry = store.get_entry("scheduling/tools")
        assert entry is not None

    def test_second_run_skips(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)

        result1 = run_autogen(store)
        assert result1["generated"] > 0

        result2 = run_autogen(store)
        assert result2["skipped"] > 0
        assert result2["generated"] == 0

    def test_marks_auto_source(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)

        run_autogen(store)

        db = get_db()
        try:
            row = db.execute(
                "SELECT auto_source FROM kb_entries WHERE path = ?",
                ("scheduling/tools",),
            ).fetchone()
            assert row is not None
            assert row["auto_source"] == "autogen"
        finally:
            db.close()


class TestCheckSourceHashes:
    def test_detects_changes(self, tmp_path):
        # Write a file
        f = tmp_path / "test.py"
        f.write_text("# original")

        # Store its hash
        from carpenter.kb.autogen import _hash_file
        _update_source_hash(str(f), _hash_file(str(f)))

        # No changes yet
        changed = check_source_hashes([str(f)])
        assert len(changed) == 0

        # Modify the file
        f.write_text("# modified")
        changed = check_source_hashes([str(f)])
        assert str(f) in changed

    def test_new_file_detected(self, tmp_path):
        f = tmp_path / "new.py"
        f.write_text("# new")
        changed = check_source_hashes([str(f)])
        assert str(f) in changed


class TestProcessChangeQueue:
    def test_processes_queued_changes(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)

        # First generate so entries exist
        run_autogen(store)

        # Queue a change for a known source file
        from carpenter.kb.autogen import _REPO_ROOT
        source = str(_REPO_ROOT / "carpenter_tools" / "act" / "scheduling.py")
        store.queue_change(source, "modified")

        count = process_change_queue(store)
        assert count == 1

    def test_empty_queue_returns_zero(self, tmp_path):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        assert process_change_queue(store) == 0


class TestMtimePollAndProcess:
    def test_initializes_mtime_cache(self, tmp_path):
        """First run should populate _mtime_cache without queuing changes."""
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        _mtime_cache.clear()

        _mtime_poll_and_process()

        # Cache should have some entries
        assert len(_mtime_cache) > 0
