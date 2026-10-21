"""Tests for operator-gated WRITE chat tools (security invariant I10 relaxation).

The chat agent is read-only BY DEFAULT.  A capability package's
write-capable chat-boundary tools (those declaring ``arc_create`` /
``external_effect`` / ``database_write`` / ``filesystem_write``) must
NOT register unless the operator explicitly opted the package in at
install time (``write_chat_tools_allowed``).  When gated, the package's
read-only chat tools still register and the write tools are gracefully
skipped (surfaced as gated, not a fatal error).  Platform-boundary
package tools remain a hard refusal regardless of the opt-in.
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter import chat_tool_loader
from carpenter.packages.registry import PackageRegistry
from carpenter.packages.installer import (
    classify_package_chat_tools,
    ensure_installer_tables,
    get_install_record,
    install_package,
    write_chat_tools_allowed_for_package,
)
from carpenter.packages.manifest import load_manifest


def _local_spec_resolver(resolve):
    """A ``cli_packages._spec_for`` stand-in naming a local tree.

    The CLI resolves a source spec once and uses the result both as the
    thing to fetch and as the provenance to record, so a test that wants
    the install pointed at its own directory substitutes that one
    resolution.  ``kind="local"`` is what keeps these tests on the
    read-in-place path with no fetch.
    """
    from carpenter.packages.sources import SourceSpec

    def _spec_for(source_name, path):
        return SourceSpec(
            kind="local", raw=str(source_name),
            path=Path(resolve(source_name)),
        )

    return _spec_for


# A package with BOTH a read-only chat tool and a write-capable
# (arc_create) chat tool.
MIXED_TOOLS_PY = """\
from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description="Read-only lister.",
    input_schema={"type": "object", "properties": {}, "required": []},
    capabilities=["database_read"],
)
def pkg_mixed_list(tool_input, **kwargs):
    return "listed"


@chat_tool(
    description="Write-capable sender (creates an arc).",
    input_schema={"type": "object", "properties": {}, "required": []},
    capabilities=["arc_create", "external_effect"],
    requires_user_confirm=True,
)
def pkg_mixed_send(tool_input, **kwargs):
    return "sent"
"""

MIXED_MANIFEST = """\
name: mixed
version: "0.1.0"
description: Package with a read-only and a write-capable chat tool.
chat_tools:
  - tools.py
"""


# A package whose chat tool declares trust_boundary='platform' — must be
# hard-refused regardless of any opt-in.
PLATFORM_BOUNDARY_TOOL_PY = """\
from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description="Tries to be platform.",
    input_schema={"type": "object", "properties": {}, "required": []},
    capabilities=["pure"],
    trust_boundary="platform",
)
def pkg_evil_platform(tool_input, **kwargs):
    return "should not register"
"""

PLATFORM_BOUNDARY_MANIFEST = """\
name: evilplat
version: "0.1.0"
description: Declares a platform-boundary chat tool.
chat_tools:
  - tools.py
"""


# A package whose chat tool builds its capability list at runtime, so a
# static read cannot settle what it declares.  Previewing it without
# running it must fail closed rather than assume read-only.
DYNCAPS_TOOLS_PY = """\
from carpenter.chat_tool_loader import chat_tool

CAPS = ["database_read"]


@chat_tool(
    description="Capabilities come from a module variable.",
    input_schema={"type": "object", "properties": {}, "required": []},
    capabilities=CAPS,
)
def pkg_dyncaps(tool_input, **kwargs):
    return "ok"
"""

DYNCAPS_MANIFEST = """\
name: dyncaps
version: "0.1.0"
description: Declares its chat-tool capabilities indirectly.
chat_tools:
  - tools.py
"""

# A read-only tool beside a module that cannot be parsed at all.
READ_TOOL_PY = """\
from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description="Read-only lister.",
    input_schema={"type": "object", "properties": {}, "required": []},
    capabilities=["database_read"],
)
def pkg_halfread_list(tool_input, **kwargs):
    return "listed"
"""

HALFREAD_MANIFEST = """\
name: halfread
version: "0.1.0"
description: One readable chat-tool module and one that is not.
chat_tools:
  - tools.py
  - broken.py
"""


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_installer_tables(conn)
    yield conn
    conn.close()


def _make_source(root: Path, name: str, manifest: str, tools: str) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.yaml").write_text(dedent(manifest))
    (pkg / "tools.py").write_text(dedent(tools))
    return pkg


# ── schema / install record ─────────────────────────────────────────


class TestInstallRecordFlag:
    def test_default_install_records_gated_off(self, tmp_path, db_conn):
        src = _make_source(
            tmp_path / "src", "mixed", MIXED_MANIFEST, MIXED_TOOLS_PY,
        )
        dest = tmp_path / "installed" / "mixed"
        result = install_package(src, dest, conn=db_conn)
        assert result.write_chat_tools_allowed is False
        record = get_install_record(db_conn, "mixed")
        assert record is not None
        assert record["write_chat_tools_allowed"] is False
        assert write_chat_tools_allowed_for_package(db_conn, "mixed") is False

    def test_opt_in_records_allowed(self, tmp_path, db_conn):
        src = _make_source(
            tmp_path / "src", "mixed", MIXED_MANIFEST, MIXED_TOOLS_PY,
        )
        dest = tmp_path / "installed" / "mixed"
        result = install_package(
            src, dest, conn=db_conn, allow_write_chat_tools=True,
        )
        assert result.write_chat_tools_allowed is True
        assert write_chat_tools_allowed_for_package(db_conn, "mixed") is True

    def test_unknown_package_is_gated_off(self, db_conn):
        # Fail-closed: a package with no install record is read-only.
        assert write_chat_tools_allowed_for_package(db_conn, "nope") is False


# ── classification helper ───────────────────────────────────────────


class TestClassifyChatTools:
    def test_splits_read_only_and_write(self, tmp_path):
        src = _make_source(
            tmp_path / "src", "mixed", MIXED_MANIFEST, MIXED_TOOLS_PY,
        )
        manifest = load_manifest(src / "manifest.yaml")
        read_only, write_caps = classify_package_chat_tools(manifest)
        ro_names = {t["name"] for t in read_only}
        wr_names = {t["name"] for t in write_caps}
        assert ro_names == {"pkg_mixed_list"}
        assert wr_names == {"pkg_mixed_send"}
        send = write_caps[0]
        assert set(send["write_capabilities"]) == {
            "arc_create", "external_effect",
        }
        assert send["requires_user_confirm"] is True


# ── registry gating behaviour ───────────────────────────────────────


class TestRegistryGating:
    def test_default_skips_write_tool_registers_read_only(
        self, tmp_path, db_conn,
    ):
        src = _make_source(
            tmp_path / "src", "mixed", MIXED_MANIFEST, MIXED_TOOLS_PY,
        )
        dest_root = tmp_path / "installed"
        install_package(src, dest_root / "mixed", conn=db_conn)
        registry = PackageRegistry()
        loaded = registry.discover_and_register(
            search_paths=[dest_root], db_conn=db_conn,
        )
        assert len(loaded) == 1
        pkg = loaded[0]
        # Read-only registers; write tool is gated (skipped, not error).
        assert pkg.chat_tool_names == ("pkg_mixed_list",)
        assert pkg.gated_chat_tool_names == ("pkg_mixed_send",)
        assert pkg.load_errors == ()
        tools = chat_tool_loader.get_loaded_tools()
        assert "pkg_mixed_list" in tools
        assert "pkg_mixed_send" not in tools

    def test_opt_in_registers_both(self, tmp_path, db_conn):
        src = _make_source(
            tmp_path / "src", "mixed", MIXED_MANIFEST, MIXED_TOOLS_PY,
        )
        dest_root = tmp_path / "installed"
        install_package(
            src, dest_root / "mixed", conn=db_conn,
            allow_write_chat_tools=True,
        )
        registry = PackageRegistry()
        loaded = registry.discover_and_register(
            search_paths=[dest_root], db_conn=db_conn,
        )
        assert len(loaded) == 1
        pkg = loaded[0]
        assert set(pkg.chat_tool_names) == {"pkg_mixed_list", "pkg_mixed_send"}
        assert pkg.gated_chat_tool_names == ()
        assert pkg.load_errors == ()
        tools = chat_tool_loader.get_loaded_tools()
        assert "pkg_mixed_list" in tools
        assert "pkg_mixed_send" in tools

    def test_no_db_conn_gates_off(self, tmp_path):
        # Without a DB connection the registry cannot prove consent, so
        # it fails closed (write tools gated off).
        src = _make_source(
            tmp_path / "src", "mixed", MIXED_MANIFEST, MIXED_TOOLS_PY,
        )
        dest_root = tmp_path / "installed"
        # Materialize a plain copy (no hash verification without db_conn).
        import shutil
        (dest_root).mkdir(parents=True)
        shutil.copytree(src, dest_root / "mixed")
        registry = PackageRegistry()
        loaded = registry.discover_and_register(search_paths=[dest_root])
        assert len(loaded) == 1
        pkg = loaded[0]
        assert pkg.chat_tool_names == ("pkg_mixed_list",)
        assert pkg.gated_chat_tool_names == ("pkg_mixed_send",)

    def test_platform_boundary_still_hard_refused(self, tmp_path, db_conn):
        src = _make_source(
            tmp_path / "src", "evilplat",
            PLATFORM_BOUNDARY_MANIFEST, PLATFORM_BOUNDARY_TOOL_PY,
        )
        dest_root = tmp_path / "installed"
        # Opt in to write chat tools — must NOT relax the platform refusal.
        install_package(
            src, dest_root / "evilplat", conn=db_conn,
            allow_write_chat_tools=True,
        )
        registry = PackageRegistry()
        loaded = registry.discover_and_register(
            search_paths=[dest_root], db_conn=db_conn,
        )
        assert len(loaded) == 1
        pkg = loaded[0]
        assert pkg.chat_tool_names == ()
        # Hard error (not a graceful gate).
        assert any("platform" in e for e in pkg.load_errors)
        assert pkg.gated_chat_tool_names == ()
        assert "pkg_evil_platform" not in chat_tool_loader.get_loaded_tools()


# ── CLI install preview + opt-in ────────────────────────────────────


class TestCliInstallPreview:
    @pytest.fixture(autouse=True)
    def _patch_db(self, db_conn, monkeypatch):
        class _Ctx:
            def __enter__(self_inner):
                return db_conn

            def __exit__(self_inner, *a):
                return False

        import carpenter.db as carpenter_db
        monkeypatch.setattr(carpenter_db, "db_transaction", lambda: _Ctx())
        return db_conn

    def _src_root(self, tmp_path):
        src_root = tmp_path / "src_root"
        _make_source(src_root, "mixed", MIXED_MANIFEST, MIXED_TOOLS_PY)
        return src_root

    def test_preview_lists_write_tools_by_capability(
        self, tmp_path, monkeypatch, capsys,
    ):
        from carpenter import cli_packages

        src_root = self._src_root(tmp_path)
        monkeypatch.setattr(
            cli_packages, "_spec_for",
            _local_spec_resolver(lambda n: src_root / n),
        )
        monkeypatch.setattr(
            cli_packages, "_install_destination_for",
            lambda n: tmp_path / "installed" / n,
        )
        # Non-interactive (no tty), no flag → write tools gated off, but
        # the preview must still enumerate them by capability.
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(""),
        )
        rc = cli_packages._cmd_install(["mixed"])
        assert rc == 0
        err = capsys.readouterr().err
        # Read-only group lists the read tool.
        assert "read-only (register by default)" in err
        assert "pkg_mixed_list" in err
        # Write group lists the write tool + its write caps.
        assert "write / effectful" in err
        assert "pkg_mixed_send" in err
        assert "arc_create" in err
        # Gated note shown.
        assert "GATED" in err or "gated" in err

    def test_flag_opts_in_and_records(
        self, tmp_path, monkeypatch, db_conn,
    ):
        from carpenter import cli_packages

        src_root = self._src_root(tmp_path)
        monkeypatch.setattr(
            cli_packages, "_spec_for",
            _local_spec_resolver(lambda n: src_root / n),
        )
        monkeypatch.setattr(
            cli_packages, "_install_destination_for",
            lambda n: tmp_path / "installed" / n,
        )
        rc = cli_packages._cmd_install(["mixed", "--allow-write-chat-tools"])
        assert rc == 0
        assert write_chat_tools_allowed_for_package(db_conn, "mixed") is True

    def test_no_flag_records_gated_off(
        self, tmp_path, monkeypatch, db_conn,
    ):
        from carpenter import cli_packages

        src_root = self._src_root(tmp_path)
        monkeypatch.setattr(
            cli_packages, "_spec_for",
            _local_spec_resolver(lambda n: src_root / n),
        )
        monkeypatch.setattr(
            cli_packages, "_install_destination_for",
            lambda n: tmp_path / "installed" / n,
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        rc = cli_packages._cmd_install(["mixed"])
        assert rc == 0
        # Package installed, but write chat tools gated off.
        assert get_install_record(db_conn, "mixed") is not None
        assert write_chat_tools_allowed_for_package(db_conn, "mixed") is False

    # ── the preview is built without running the package's code ─────
    #
    # The write opt-in is decided from a STATIC read of the source.
    # Importing the tree to build this preview would run a package the
    # operator has not yet agreed to install.

    def test_preview_never_imports_the_package(
        self, tmp_path, monkeypatch, capsys, db_conn,
    ):
        from carpenter import cli_packages
        from carpenter.packages.static_preview import NOT_EXECUTED_NOTICE

        marker = tmp_path / "executed.marker"
        monkeypatch.setenv("CARPENTER_TEST_PREVIEW_MARKER", str(marker))
        fixture = (
            Path(__file__).parent / "fixtures" / "hostile_preview_pkg"
        )
        monkeypatch.setattr(
            cli_packages, "_spec_for", _local_spec_resolver(lambda n: fixture),
        )
        monkeypatch.setattr(
            cli_packages, "_install_destination_for",
            lambda n: tmp_path / "installed" / n,
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(""))

        rc = cli_packages._cmd_install(["hostile_preview"])

        assert rc == 0
        assert not marker.exists(), (
            "the install preview executed the package's module-level code"
        )
        err = capsys.readouterr().err
        assert NOT_EXECUTED_NOTICE in err
        # The tool is still described, and still gated.
        assert "hostile_tool" in err
        assert "filesystem_write" in err
        assert write_chat_tools_allowed_for_package(
            db_conn, "hostile_preview",
        ) is False

    def test_unreadable_capabilities_require_the_write_opt_in(
        self, tmp_path, monkeypatch, capsys, db_conn,
    ):
        # Fail closed: the capability list is not a literal, so the
        # static read cannot prove the package is read-only.
        from carpenter import cli_packages

        src_root = tmp_path / "dyn_root"
        _make_source(src_root, "dyncaps", DYNCAPS_MANIFEST, DYNCAPS_TOOLS_PY)
        monkeypatch.setattr(
            cli_packages, "_spec_for",
            _local_spec_resolver(lambda n: src_root / n),
        )
        monkeypatch.setattr(
            cli_packages, "_install_destination_for",
            lambda n: tmp_path / "installed" / n,
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(""))

        rc = cli_packages._cmd_install(["dyncaps"])

        assert rc == 0
        err = capsys.readouterr().err
        assert "NOT DETERMINED" in err
        assert "could NOT be determined" in err
        # An unreadable capability list makes the tool itself count as
        # write-capable, so it is named in the gated group.
        assert "pkg_dyncaps" in err
        assert "WRITE-capable chat tool(s)" in err
        assert write_chat_tools_allowed_for_package(db_conn, "dyncaps") is False

    def test_an_unparseable_module_alone_forces_the_write_opt_in(
        self, tmp_path, monkeypatch, capsys, db_conn,
    ):
        # Nothing readable is write-capable here: the only declared tool
        # is read-only.  The second module cannot be parsed, so the
        # package still cannot be proven read-only and the gate applies.
        from carpenter import cli_packages

        src_root = tmp_path / "broken_root"
        pkg = _make_source(
            src_root, "halfread", HALFREAD_MANIFEST, READ_TOOL_PY,
        )
        (pkg / "broken.py").write_text("def nope(:\n    pass\n")
        monkeypatch.setattr(
            cli_packages, "_spec_for",
            _local_spec_resolver(lambda n: src_root / n),
        )
        monkeypatch.setattr(
            cli_packages, "_install_destination_for",
            lambda n: tmp_path / "installed" / n,
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(""))

        rc = cli_packages._cmd_install(["halfread"])

        assert rc == 0
        err = capsys.readouterr().err
        assert "could not be parsed" in err
        assert "treated as WRITE-capable" in err
        assert write_chat_tools_allowed_for_package(
            db_conn, "halfread",
        ) is False

    def test_unreadable_capabilities_can_be_opted_in(
        self, tmp_path, monkeypatch, db_conn,
    ):
        from carpenter import cli_packages

        src_root = tmp_path / "dyn_root"
        _make_source(src_root, "dyncaps", DYNCAPS_MANIFEST, DYNCAPS_TOOLS_PY)
        monkeypatch.setattr(
            cli_packages, "_spec_for",
            _local_spec_resolver(lambda n: src_root / n),
        )
        monkeypatch.setattr(
            cli_packages, "_install_destination_for",
            lambda n: tmp_path / "installed" / n,
        )
        rc = cli_packages._cmd_install(["dyncaps", "--allow-write-chat-tools"])
        assert rc == 0
        assert write_chat_tools_allowed_for_package(db_conn, "dyncaps") is True

    def test_interactive_yes_opts_in_for_an_unreadable_package(
        self, tmp_path, monkeypatch, db_conn,
    ):
        from carpenter import cli_packages

        src_root = tmp_path / "dyn_root"
        _make_source(src_root, "dyncaps", DYNCAPS_MANIFEST, DYNCAPS_TOOLS_PY)
        monkeypatch.setattr(
            cli_packages, "_spec_for",
            _local_spec_resolver(lambda n: src_root / n),
        )
        monkeypatch.setattr(
            cli_packages, "_install_destination_for",
            lambda n: tmp_path / "installed" / n,
        )

        class _Tty(io.StringIO):
            def isatty(self):
                return True

        monkeypatch.setattr("sys.stdin", _Tty(""))
        monkeypatch.setattr("builtins.input", lambda *_a: "yes")
        rc = cli_packages._cmd_install(["dyncaps"])
        assert rc == 0
        assert write_chat_tools_allowed_for_package(db_conn, "dyncaps") is True
