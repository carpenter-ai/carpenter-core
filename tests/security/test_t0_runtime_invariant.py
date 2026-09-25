"""No runtime that runs agent-chosen reads can see a T0 path.

T0 ("platform-invisible") is the one rule every read path must honour:
credentials and secret stores never reach an agent, whichever runtime
the agent is using.  The classifier is tested in ``test_platform_paths.py``
and the files backend's handling of the built-in T0 patterns in
``tests/tool_backends/test_files_tier.py``.  This file pins the rule
end to end for the case a deployment relies on most: a secret store
that is T0 *only* because config declares it, via
``platform_integrity.path_overrides``.  The directory is deliberately
named so that no built-in pattern (``*/credentials/*``, ``*/secrets/*``,
``*.key`` …) matches it.

Each test covers one runtime.  Adding a runtime that can read files
means adding a test here.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest

from carpenter import config as carpenter_config
from carpenter.security.platform_paths import PATH_TIER_T0, path_tier

SECRET = "s3cret-value-that-must-never-appear"


@pytest.fixture
def vault(monkeypatch, tmp_path):
    """A secret file inside a directory that only config marks T0.

    It sits under ``base_dir`` (``tmp_path``, set by the autouse
    ``test_db`` fixture), so base-dir confinement cannot be what refuses
    it — only the T0 rule can.
    """
    monkeypatch.setitem(carpenter_config.CONFIG, "repo_dir", str(tmp_path / "repo"))
    store = tmp_path / "hostvault"
    store.mkdir()
    (store / "bundle").write_text(f"TOKEN={SECRET}\n")
    monkeypatch.setitem(
        carpenter_config.CONFIG,
        "platform_integrity",
        {"path_overrides": [{"prefix": str(store), "tier": PATH_TIER_T0}]},
    )
    return store


def _dispatch_error():
    from carpenter.executor.dispatch_bridge import DispatchError
    return DispatchError


def test_store_is_t0_only_through_config(vault, monkeypatch):
    """Guard for the fixture: without the override the store is readable,
    so every refusal below is the config rule at work."""
    assert path_tier(str(vault / "bundle")) == PATH_TIER_T0
    monkeypatch.setitem(carpenter_config.CONFIG, "platform_integrity", {})
    assert path_tier(str(vault / "bundle")) != PATH_TIER_T0


# ── files.* tool backend (arc and executor dispatch) ────────────────────


def test_files_read_refused(vault):
    from carpenter.tool_backends import files
    with pytest.raises(_dispatch_error()) as exc:
        files.handle_read({"path": str(vault / "bundle")})
    assert exc.value.status_code == 403
    assert SECRET not in str(exc.value)


def test_files_read_via_symlink_refused(vault, tmp_path):
    from carpenter.tool_backends import files
    link = tmp_path / "innocent.txt"
    link.symlink_to(vault / "bundle")
    with pytest.raises(_dispatch_error()) as exc:
        files.handle_read({"path": str(link)})
    assert exc.value.status_code == 403


def test_files_list_hides_store(vault, tmp_path):
    from carpenter.tool_backends import files
    result = files.handle_list({"dir": str(tmp_path)})
    assert vault.name not in result["files"]


def test_files_list_inside_store_reveals_nothing(vault):
    from carpenter.tool_backends import files
    try:
        result = files.handle_list({"dir": str(vault)})
    except _dispatch_error():
        return
    assert "bundle" not in result["files"]


# ── Chat runtime ────────────────────────────────────────────────────────


def test_chat_provenance_check_refuses(vault):
    from carpenter.tool_backends import files
    refusal = files.chat_read_provenance_check(str(vault / "bundle"))
    assert refusal is not None
    assert SECRET not in refusal


def _load_chat_files_tool(monkeypatch):
    seed = os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..",
        "config_seed", "chat_tools", "files.py",
    ))
    loader = types.ModuleType("carpenter.chat_tool_loader")
    loader.chat_tool = lambda **_: (lambda fn: fn)
    monkeypatch.setitem(sys.modules, "carpenter.chat_tool_loader", loader)
    spec = importlib.util.spec_from_file_location("_t0_chat_files", seed)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_chat_read_file_tool_refuses(vault, monkeypatch):
    tool = _load_chat_files_tool(monkeypatch)
    out = tool.read_file({"path": str(vault / "bundle")})
    assert SECRET not in str(out)
    assert "denied" in str(out).lower()


# ── Restricted executor (agent-written code) ────────────────────────────


@pytest.mark.parametrize("code", [
    "print(open({p!r}).read())",
    "import os\nprint(os.popen('cat ' + {p!r}).read())",
    "import pathlib\nprint(pathlib.Path({p!r}).read_text())",
    "import io\nprint(io.open({p!r}).read())",
])
def test_restricted_executor_cannot_read(vault, code):
    from carpenter.executor.restricted import RestrictedExecutor
    result = RestrictedExecutor().execute(code.format(p=str(vault / "bundle")))
    assert result.exit_code != 0
    assert SECRET not in (result.output or "")
    assert SECRET not in (result.error or "")


# ── External coding agents (host subprocess) ────────────────────────────


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known gap: external coding agents run as an unsandboxed host "
        "subprocess, so the T0 rule is not enforced for them. Remove this "
        "marker when they run under a sandbox that masks T0 paths."
    ),
)
def test_external_coding_agent_cannot_read(vault, tmp_path):
    from carpenter.agent import external_coding_agent
    ws = tmp_path / "ws"
    ws.mkdir()
    profile = {"command": f"cat {vault / 'bundle'}", "timeout": 10, "env": {}}
    result = external_coding_agent.run(str(ws), "prompt", profile)
    assert SECRET not in result["stdout"] + result["stderr"]
