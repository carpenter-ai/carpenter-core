"""Shared helpers for capability-package tests.

Each test gets its own scratch directory with a ``packages/<name>/``
layout written from inline strings.  This keeps fixtures readable
inside individual tests and avoids spreading YAML files across the
tree.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest


@pytest.fixture
def package_root(tmp_path: Path) -> Path:
    """Return an empty ``packages/`` directory; tests populate it."""
    root = tmp_path / "packages"
    root.mkdir()
    return root


def write_package(
    package_root: Path,
    *,
    name: str,
    manifest_yaml: str,
    files: dict[str, str] | None = None,
) -> Path:
    """Create a package directory with a manifest and optional files.

    Args:
        package_root: ``packages/`` directory (from the fixture).
        name: Package directory name (typically the manifest's ``name``).
        manifest_yaml: Raw YAML content for ``manifest.yaml``.
        files: Mapping of relative path -> file contents to write
            inside the package directory (e.g. chat-tool modules).

    Returns:
        The package directory path.
    """
    pkg_dir = package_root / name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "manifest.yaml").write_text(dedent(manifest_yaml))
    for rel, content in (files or {}).items():
        target = pkg_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dedent(content))
    return pkg_dir


@pytest.fixture
def make_package(package_root: Path):
    """Return a callable that writes a package into ``package_root``."""
    def _make(
        name: str,
        manifest_yaml: str,
        files: dict[str, str] | None = None,
    ) -> Path:
        return write_package(
            package_root, name=name,
            manifest_yaml=manifest_yaml, files=files,
        )
    return _make


@pytest.fixture(autouse=True)
def reset_package_registry():
    """Reset the global package registry between tests.

    The package registry holds a process-wide dict of loaded packages;
    leaking state between tests would cause cross-contamination.  We
    also reset chat_tool_loader's loaded tools — extension tool
    registration mutates module state.

    Package modules are loaded package-aware via
    ``loaders._import_package_module``, which caches each module in
    ``sys.modules`` under ``_carpenter_pkg_.<name>.<dotted>`` and returns
    the cached object on subsequent loads.  Without purging those entries
    between tests, two tests that reuse a package name (e.g. ``hello`` or
    ``evil``) with different module bodies would silently get the FIRST
    test's module back, cross-contaminating the second.  Drop the whole
    synthetic namespace so every test loads its own fixture cleanly.
    """
    import sys

    from carpenter.packages.registry import get_registry
    from carpenter import chat_tool_loader

    def _purge_pkg_namespace() -> None:
        for mod_name in [
            m for m in sys.modules
            if m == "_carpenter_pkg_" or m.startswith("_carpenter_pkg_.")
        ]:
            sys.modules.pop(mod_name, None)

    saved_tools = dict(chat_tool_loader._loaded_tools)
    _purge_pkg_namespace()
    get_registry().reset()
    yield
    get_registry().reset()
    _purge_pkg_namespace()
    # Restore loaded tools so other tests aren't affected.
    chat_tool_loader._loaded_tools.clear()
    chat_tool_loader._loaded_tools.update(saved_tools)
