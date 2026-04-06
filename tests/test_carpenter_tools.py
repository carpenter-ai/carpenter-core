"""Tests for the carpenter_tools executor-side package.

Tests for the HTTP callback client (_callback.py) have been removed since
the RestrictedPython executor uses the dispatch bridge instead.
"""
import os

from carpenter_tools.read import files as read_files
from carpenter_tools.act import files as act_files
from carpenter_tools.tool_meta import get_tool_meta, validate_package, build_tool_policy_map


def test_files_read_write(tmp_path):
    path = str(tmp_path / "test.txt")
    act_files.write(path, "hello from tools")
    content = read_files.read(path)
    assert content == "hello from tools"


def test_files_list_dir(tmp_path):
    listing_dir = tmp_path / "listing"
    listing_dir.mkdir()
    (listing_dir / "x.txt").write_text("x")
    (listing_dir / "y.txt").write_text("y")
    result = read_files.list_dir(str(listing_dir))
    assert sorted(result) == ["x.txt", "y.txt"]


def test_read_tools_have_safe_metadata():
    """All read/ tools should be declared local, readonly, no side_effects."""
    meta = get_tool_meta(read_files.read)
    assert meta is not None
    assert meta["local"] is True
    assert meta["readonly"] is True
    assert meta["side_effects"] is False


def test_act_tools_have_unsafe_metadata():
    """All act/ tools should be declared with at least one unsafe property."""
    meta = get_tool_meta(act_files.write)
    assert meta is not None
    assert meta["side_effects"] is True


def test_read_package_validation():
    """validate_package on read/ should find no errors."""
    import carpenter_tools.read as read_pkg
    errors = validate_package(read_pkg, expected_safe=True)
    assert errors == [], f"Unexpected errors: {errors}"


def test_act_package_validation():
    """validate_package on act/ should find no errors."""
    import carpenter_tools.act as act_pkg
    errors = validate_package(act_pkg, expected_safe=False)
    assert errors == [], f"Unexpected errors: {errors}"


# -- build_tool_policy_map tests --


def test_build_tool_policy_map_includes_web_get():
    """web.get should have both positional and keyword entries for 'url'."""
    m = build_tool_policy_map()
    assert m[("web", "get", 0)] == "url"
    assert m[("web", "get", "url")] == "url"


def test_build_tool_policy_map_includes_web_post():
    m = build_tool_policy_map()
    assert m[("web", "post", 0)] == "url"
    assert m[("web", "post", "url")] == "url"


def test_build_tool_policy_map_includes_files_write():
    m = build_tool_policy_map()
    assert m[("files", "write", 0)] == "filepath"
    assert m[("files", "write", "path")] == "filepath"


def test_build_tool_policy_map_includes_files_read():
    m = build_tool_policy_map()
    assert m[("files", "read", 0)] == "filepath"
    assert m[("files", "read", "path")] == "filepath"


def test_build_tool_policy_map_includes_list_dir():
    m = build_tool_policy_map()
    assert m[("files", "list_dir", 0)] == "filepath"
    assert m[("files", "list_dir", "directory")] == "filepath"


def test_build_tool_policy_map_no_policy_tools_excluded():
    """Tools without param_policies (e.g. messaging.send) should not appear."""
    m = build_tool_policy_map()
    messaging_keys = [k for k in m if k[0] == "messaging"]
    assert messaging_keys == []


def test_param_policies_stored_in_tool_meta():
    """param_policies should be accessible via get_tool_meta."""
    from carpenter_tools.act import web
    meta = get_tool_meta(web.get)
    assert meta is not None
    assert meta["param_policies"] == {"url": "url"}


def test_param_policies_none_when_not_set():
    """Tools without param_policies should have None."""
    from carpenter_tools.act import messaging
    meta = get_tool_meta(messaging.send)
    assert meta is not None
    assert meta["param_policies"] is None
