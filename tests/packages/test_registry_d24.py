"""Tests for D24 stage 3a registry behaviour: hash verification and shim."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter.packages.registry import PackageRegistry, default_search_paths
from carpenter.packages.installer import (
    ensure_installer_tables,
    install_package,
    get_install_record,
)


HELLO_TOOL_PY = """\
from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description="Test hello tool.",
    input_schema={"type": "object", "properties": {}, "required": []},
    capabilities=["pure"],
)
def pkg_d24_hello(tool_input, **kwargs):
    return "hello!"
"""


def _make_source_pkg(root: Path, name: str) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.yaml").write_text(dedent(f"""\
        name: {name}
        version: "0.1.0"
        description: Reference no-op package.
        chat_tools:
          - tools.py
    """))
    (pkg / "tools.py").write_text(HELLO_TOOL_PY)
    return pkg


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_installer_tables(conn)
    yield conn
    conn.close()


class TestVerifyOnLoad:
    def test_hash_match_loads(self, tmp_path, db_conn):
        src = _make_source_pkg(tmp_path / "src", "hello")
        dest_root = tmp_path / "installed"
        dest = dest_root / "hello"
        install_package(src, dest, conn=db_conn)
        registry = PackageRegistry()
        loaded = registry.discover_and_register(
            search_paths=[dest_root], db_conn=db_conn,
        )
        assert len(loaded) == 1
        assert loaded[0].manifest.name == "hello"

    def test_hash_mismatch_skips(self, tmp_path, db_conn):
        src = _make_source_pkg(tmp_path / "src", "hello")
        dest_root = tmp_path / "installed"
        dest = dest_root / "hello"
        install_package(src, dest, conn=db_conn)
        # Tamper.
        (dest / "tools.py").write_text("# tampered\n")
        registry = PackageRegistry()
        loaded = registry.discover_and_register(
            search_paths=[dest_root], db_conn=db_conn,
        )
        # The tampered package was refused.
        assert loaded == []


class TestDefaultSearchPaths:
    def test_default_search_paths_contains_install_dir(self):
        paths = default_search_paths()
        # Either a base_dir-derived path or the canonical
        # ~/carpenter/packages/ path must be present.
        from carpenter.packages.registry import default_install_paths
        install = [p.resolve() if p.exists() else p
                   for p in default_install_paths()]
        all_resolved = [p.resolve() if p.exists() else p for p in paths]
        # All install paths are in the default search list.
        for ip in install:
            assert ip in all_resolved
