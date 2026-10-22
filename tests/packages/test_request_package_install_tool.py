"""The ``request_package_install`` chat tool.

Loaded from the seed module the way the platform loads it, and driven
with a fetcher that never touches the network.  What matters here is
that a success ends the turn with a URL, that every refusal is an
ordinary string the model can explain, and that no package code is
imported on the way to either.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter import config
from carpenter.chat_tool_loader import FinalReply
from carpenter.db import db_connection, get_db
from carpenter.packages import install_requests as ir
from carpenter.packages import pending_install as pi
from carpenter.packages.fetch import FetchResult
from carpenter.packages.installer import ensure_installer_tables

SEED = (
    Path(__file__).resolve().parents[2]
    / "config_seed" / "chat_tools" / "packages_install_request.py"
)

MANIFEST = """\
name: hello
version: "1.2.3"
description: A reference package.
chat_tools:
  - tools.py
"""

TOOLS = """\
from carpenter.chat_tool_loader import chat_tool

open("/tmp/wp5a-tool-import-marker", "w").write("imported")  # never runs


@chat_tool(
    description="Write a thing.",
    input_schema={"type": "object", "properties": {}},
    capabilities=["database_write"],
)
def hello_write(tool_input, **kwargs):
    return "done"
"""

CAPS_MANIFEST = """\
name: mailer
version: "0.2.0"
description: Fetches mail.
platform_capabilities:
  - verb: imap.fetch
    kind: egress
    module: handlers.imap
    handler: fetch
    grant:
      protocol: imap
      host_from: IMAP_HOST
      port: 993
      credential_ref: MAILER
credential_requirements:
  - kind: env
    provider: imap_smtp
    env_key_prefix: MAILER
    required_keys:
      - IMAP_HOST
      - PASSWORD
"""

CAPS_FILES = {"handlers/imap.py": "def fetch(ctx, **kwargs):\n    return {}\n"}

EVIL_MANIFEST = """\
name: evil
version: "0.1.0"
description: Reaches outside its own tree.
chat_tools:
  - ../../outside.py
"""

SPEC = "git+https://forge.invalid/org/repo.git"


def _load_tool():
    """Load the seed module and return its handler, as the loader does."""
    spec = importlib.util.spec_from_file_location(
        "_test_packages_install_request", str(SEED),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.request_package_install


@pytest.fixture
def tool():
    return _load_tool()


@pytest.fixture
def installer_tables():
    db = get_db()
    try:
        ensure_installer_tables(db)
    finally:
        db.close()


@pytest.fixture
def fake_fetch(monkeypatch):
    """Materialise a fixture tree instead of fetching one."""
    trees: dict[str, tuple[str, dict]] = {}
    calls: list[str] = []

    def register(fragment, manifest_yaml, files=None):
        trees[fragment] = (manifest_yaml, files or {})

    def fake_fetch_source(spec, *, limits=None, staging_root=None):
        calls.append(spec.raw)
        matching = [f for f in trees if f in spec.raw]
        if not matching:
            raise AssertionError(f"no fixture for {spec.raw!r}")
        manifest_yaml, files = trees[max(matching, key=len)]
        quarantine = Path(staging_root) / f"_fetch-{len(calls)}"
        quarantine.mkdir(parents=True)
        tree = quarantine / "pkg"
        tree.mkdir()
        (tree / "manifest.yaml").write_text(dedent(manifest_yaml))
        for rel, content in files.items():
            target = tree / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(dedent(content))
        return FetchResult(
            tree=tree, staging=quarantine, resolved="d" * 40,
            bytes_fetched=2048, files_fetched=2,
        )

    monkeypatch.setattr(ir, "fetch_source", fake_fetch_source)
    fake_fetch_source.register = register  # type: ignore[attr-defined]
    fake_fetch_source.calls = calls  # type: ignore[attr-defined]
    return fake_fetch_source


def _hello(fake_fetch):
    fake_fetch.register("forge.invalid", MANIFEST, {"tools.py": TOOLS})


def _rows():
    with db_connection() as db:
        return pi.list_requests(db)


# ── the declaration ─────────────────────────────────────────────────

def test_the_seed_module_loads_and_validates():
    """Exactly how the platform loads it, including validate_tool_defs.

    ``load_chat_tools`` raises if a platform-boundary tool is not in
    ``PLATFORM_TOOLS``, so this test is also what catches the seed file
    and the allowlist entry drifting apart.
    """
    from carpenter.chat_tool_loader import load_chat_tools

    with tempfile.TemporaryDirectory() as d:
        shutil.copy(SEED, d)
        tools = load_chat_tools(d)
    loaded = tools["request_package_install"]
    assert loaded.trust_boundary == "platform"
    assert loaded.requires_user_confirm is False
    assert loaded.always_available is False
    assert set(loaded.capabilities) == {"filesystem_write", "database_write"}
    assert loaded.input_schema["properties"]["sources"]["maxItems"] == 5


def test_the_tool_is_chat_only_and_not_operator_only():
    from carpenter.chat_tool_registry import (
        CHAT_ONLY_TOOLS, OPERATOR_ONLY_TOOLS, PLATFORM_TOOLS,
    )
    assert "request_package_install" in CHAT_ONLY_TOOLS
    assert "request_package_install" in PLATFORM_TOOLS
    assert "request_package_install" not in OPERATOR_ONLY_TOOLS


# ── success ─────────────────────────────────────────────────────────

def test_a_request_ends_the_turn_with_the_link(
    tool, installer_tables, fake_fetch,
):
    _hello(fake_fetch)
    out = tool({"sources": [SPEC]})

    assert isinstance(out, FinalReply)
    rows = _rows()
    assert len(rows) == 1
    record = rows[0]
    assert f"/api/package-install/{record.token}" in out
    assert "Nothing has been installed" in out
    assert "hello v1.2.3" in out
    assert record.status == pi.STATUS_REQUESTED
    assert record.sources == (SPEC,)


def test_the_reply_says_the_link_is_relative(
    tool, installer_tables, fake_fetch,
):
    """An unset public_base_url must not look like a working link."""
    _hello(fake_fetch)
    out = tool({"sources": [SPEC]})
    assert "public_base_url" in out
    assert "a path, not a full URL" in out


def test_a_configured_base_url_gives_a_whole_link(
    tool, installer_tables, fake_fetch, monkeypatch,
):
    monkeypatch.setitem(
        config.CONFIG, "public_base_url", "https://platform.invalid",
    )
    _hello(fake_fetch)
    out = tool({"sources": [SPEC]})
    assert "https://platform.invalid/api/package-install/" in out
    assert "a path, not a full URL" not in out


def test_declared_capabilities_and_credentials_are_called_out(
    tool, installer_tables, fake_fetch,
):
    fake_fetch.register("forge.invalid", CAPS_MANIFEST, CAPS_FILES)
    out = tool({"sources": [SPEC]})
    assert "PLATFORM-LEVEL capability" in out and "imap.fetch" in out
    assert "MAILER_PASSWORD" in out


def test_write_capable_chat_tools_are_called_out(
    tool, installer_tables, fake_fetch,
):
    _hello(fake_fetch)
    assert "write-capable chat tools" in tool({"sources": [SPEC]})


def test_no_package_code_is_imported(tool, installer_tables, fake_fetch):
    """The preview is a parse.  ``tools.py`` writes a file if imported."""
    marker = Path("/tmp/wp5a-tool-import-marker")
    if marker.exists():  # pragma: no cover — only if an earlier run leaked
        marker.unlink()
    _hello(fake_fetch)
    before = {m for m in sys.modules if m.startswith("_carpenter_pkg_")}
    tool({"sources": [SPEC]})
    assert {m for m in sys.modules if m.startswith("_carpenter_pkg_")} == before
    assert not marker.exists()


def test_the_conversation_id_is_recorded(tool, installer_tables, fake_fetch):
    from carpenter.agent import conversation

    conv_id = conversation.create_conversation()
    conversation.add_message(conv_id, "user", "install hello for me")
    _hello(fake_fetch)
    tool({"sources": [SPEC]}, conversation_id=conv_id)
    record = _rows()[0]
    assert record.conversation_id == conv_id
    assert record.request_text == "install hello for me"


# ── refusals ────────────────────────────────────────────────────────

def test_an_arc_agent_is_refused_before_any_fetch(
    tool, installer_tables, fake_fetch,
):
    _hello(fake_fetch)
    out = tool({"sources": [SPEC]}, executor_arc_id=7, conversation_id=3)
    assert not isinstance(out, FinalReply)
    assert "arc agent" in out
    assert fake_fetch.calls == []
    assert _rows() == []


@pytest.mark.parametrize("bad", [
    {}, {"sources": []}, {"sources": "a-string"}, {"sources": [""]},
    {"sources": [None]}, {"sources": ["a"] * 6},
])
def test_bad_input_is_refused_as_a_plain_string(
    tool, installer_tables, fake_fetch, bad,
):
    out = tool(bad)
    assert not isinstance(out, FinalReply)
    assert out.startswith("Package install request refused")
    assert _rows() == []


def test_a_security_rejected_tree_leaves_no_row(
    tool, installer_tables, fake_fetch,
):
    fake_fetch.register("forge.invalid", EVIL_MANIFEST)
    out = tool({"sources": [SPEC]})
    assert not isinstance(out, FinalReply)
    assert "security guards" in out
    assert _rows() == []
    staging = Path(config.CONFIG["base_dir"]) / "cache" / "package-staging"
    assert list(staging.glob("req-*")) == []


def test_a_local_path_is_refused(tool, installer_tables, tmp_path):
    out = tool({"sources": [str(tmp_path)]})
    assert "local path" in out and _rows() == []


def test_the_rate_limit_is_reported_not_raised(
    tool, installer_tables, fake_fetch, monkeypatch,
):
    monkeypatch.setitem(
        config.CONFIG, "package_install", {"max_open_requests": 1},
    )
    _hello(fake_fetch)
    assert isinstance(tool({"sources": [SPEC]}), FinalReply)
    out = tool({"sources": ["git+https://forge.invalid/org/other.git"]})
    assert not isinstance(out, FinalReply)
    assert "waiting for the operator" in out
    assert len(_rows()) == 1


def test_an_unexpected_failure_is_still_a_string(
    tool, installer_tables, monkeypatch,
):
    """A tool must never raise into the invocation loop."""
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(ir, "create_install_request", boom)
    out = tool({"sources": [SPEC]})
    assert not isinstance(out, FinalReply)
    assert "disk on fire" in out
