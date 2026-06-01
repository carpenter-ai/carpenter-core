"""Smoke test for the list_packages chat tool."""

from __future__ import annotations

from carpenter.packages.registry import PackageRegistry


HELLO_TOOL_PY = """\
from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description="Test hello tool.",
    input_schema={"type": "object", "properties": {}, "required": []},
    capabilities=["pure"],
)
def pkg_test_listing_hello(tool_input, **kwargs):
    return "hi"
"""


HELLO_MANIFEST = """\
name: hello
version: "0.1.0"
description: Reference no-op package.
chat_tools:
  - tools.py
"""


def _import_list_packages_tool():
    """Load the list_packages tool function via the seed module."""
    import importlib.util
    from pathlib import Path
    seed = Path(__file__).resolve().parents[2] / "config_seed" / "chat_tools" / "capability_packages.py"
    spec = importlib.util.spec_from_file_location(
        "_test_capability_packages", str(seed),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.list_packages


def test_list_packages_empty():
    list_packages = _import_list_packages_tool()
    out = list_packages({})
    assert "No capability packages loaded" in out


def test_list_packages_after_register(package_root, make_package, monkeypatch):
    list_packages = _import_list_packages_tool()
    make_package("hello", HELLO_MANIFEST, files={"tools.py": HELLO_TOOL_PY})

    # Use the singleton registry so the tool sees what we registered.
    from carpenter.packages.registry import get_registry
    get_registry().discover_and_register(search_paths=[package_root.parent])

    out = list_packages({})
    assert "hello" in out
    assert "0.1.0" in out
    assert "Reference no-op package" in out


def test_list_packages_metadata_marks_it_read_only():
    """The tool must be chat-boundary, read-only — defense in depth for I10."""
    list_packages = _import_list_packages_tool()
    meta = list_packages._chat_tool_meta
    assert meta["trust_boundary"] == "chat"
    assert meta["capabilities"] == ["config_read"]
    assert meta["always_available"] is False
