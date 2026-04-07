"""Unit tests for filesystem path restriction in the files chat tool seed.

The read_file / list_files / file_count chat tools must reject any path
outside the Carpenter base directory.  These tests load the seed module
directly and exercise the _check_path helper with a mocked CONFIG.
"""

import os
import importlib.util
import types

import pytest


def _load_files_seed(monkeypatch, base_dir: str):
    """Import config_seed/chat_tools/files.py with CONFIG patched to base_dir."""
    seed_path = os.path.join(
        os.path.dirname(__file__),
        "..", "config_seed", "chat_tools", "files.py",
    )
    seed_path = os.path.normpath(seed_path)

    # Provide a minimal stub for carpenter.chat_tool_loader so the decorator
    # doesn't require the full Carpenter stack to be initialised.
    stub_loader = types.ModuleType("carpenter.chat_tool_loader")

    def chat_tool(**kwargs):
        def decorator(fn):
            fn._chat_tool_meta = kwargs
            return fn
        return decorator

    stub_loader.chat_tool = chat_tool

    # Provide a stub CONFIG that returns base_dir
    stub_config_mod = types.ModuleType("carpenter.config")
    stub_config = {"base_dir": base_dir}
    stub_config_mod.CONFIG = stub_config

    monkeypatch.setitem(
        importlib.import_module("sys").modules,
        "carpenter.chat_tool_loader",
        stub_loader,
    )
    monkeypatch.setitem(
        importlib.import_module("sys").modules,
        "carpenter.config",
        stub_config_mod,
    )

    spec = importlib.util.spec_from_file_location("_files_seed_under_test", seed_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCheckPath:

    def test_exact_base_dir_allowed(self, tmp_path, monkeypatch):
        mod = _load_files_seed(monkeypatch, str(tmp_path))
        assert mod._check_path(str(tmp_path)) is None

    def test_subpath_allowed(self, tmp_path, monkeypatch):
        mod = _load_files_seed(monkeypatch, str(tmp_path))
        subdir = str(tmp_path / "config" / "chat_tools")
        assert mod._check_path(subdir) is None

    def test_outside_base_denied(self, tmp_path, monkeypatch):
        mod = _load_files_seed(monkeypatch, str(tmp_path))
        error = mod._check_path("/etc/hostname")
        assert error is not None
        assert "access denied" in error.lower()

    def test_root_denied(self, tmp_path, monkeypatch):
        mod = _load_files_seed(monkeypatch, str(tmp_path))
        error = mod._check_path("/")
        assert error is not None

    def test_parent_traversal_denied(self, tmp_path, monkeypatch):
        """A path using .. to escape the base dir must be denied."""
        mod = _load_files_seed(monkeypatch, str(tmp_path))
        # Construct a path that goes up out of tmp_path via ..
        parent = str(tmp_path.parent)
        escaped = str(tmp_path / ".." / "escape")
        error = mod._check_path(escaped)
        assert error is not None, (
            f"Path {escaped!r} (resolves to {os.path.realpath(escaped)!r}) "
            f"should be outside base {str(tmp_path)!r}"
        )

    def test_prefix_trick_denied(self, tmp_path, monkeypatch):
        """A sibling dir whose name starts with the base dir name must be denied."""
        mod = _load_files_seed(monkeypatch, str(tmp_path))
        # e.g. base=/tmp/pytest-abc/test0, sibling=/tmp/pytest-abc/test0-evil
        sibling = str(tmp_path) + "-evil"
        error = mod._check_path(sibling)
        assert error is not None, (
            f"Sibling path {sibling!r} should be denied — it shares a prefix "
            f"with the base dir but is NOT inside it"
        )
