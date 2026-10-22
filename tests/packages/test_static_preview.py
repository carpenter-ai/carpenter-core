"""Tests for the static (AST-only) chat-tool install preview.

The operator's pre-install preview must describe a package's chat tools
*without* running the package's code: for a source the operator has not
yet agreed to trust, importing the tree to describe it is arbitrary code
execution before the prompt that was meant to authorise it.

``carpenter.packages.static_preview`` reads the ``@chat_tool``
declarations with :mod:`ast` instead.  These tests cover what it can
read, what it refuses to guess, and — the regression that matters — that
a module which is hostile at import time still previews fine and leaves
no trace of having run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from carpenter.packages.installer import classify_package_chat_tools
from carpenter.packages.manifest import load_manifest
from carpenter.packages.static_preview import (
    NOT_EXECUTED_NOTICE,
    inspect_package_chat_tools,
    write_consent_required,
)

from .conftest import write_package


FIXTURES = Path(__file__).parent / "fixtures"

MANIFEST = """\
name: {name}
version: "0.1.0"
description: Static-preview test package.
chat_tools:
{modules}
"""


def _manifest(name: str, modules=("tools.py",)) -> str:
    listed = "\n".join(f"  - {m}" for m in modules)
    return MANIFEST.format(name=name, modules=listed)


def _inspect(package_root: Path, name: str, modules, files):
    """Write a package and statically inspect it."""
    pkg = write_package(
        package_root, name=name,
        manifest_yaml=_manifest(name, modules), files=files,
    )
    manifest = load_manifest(pkg / "manifest.yaml")
    return pkg, manifest, inspect_package_chat_tools(pkg, manifest)


READ_ONLY_PY = """\
from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description="List the widgets.\\nA second line nobody should see.",
    input_schema={"type": "object", "properties": {}, "required": []},
    capabilities=["database_read"],
)
def pkg_read(tool_input, **kwargs):
    return "read"
"""

WRITE_ONLY_PY = """\
from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description="Send a widget.",
    input_schema={"type": "object", "properties": {}, "required": []},
    capabilities=["external_effect", "arc_create"],
    requires_user_confirm=True,
)
def pkg_write(tool_input, **kwargs):
    return "wrote"
"""

BOTH_PY = READ_ONLY_PY + "\n\n" + WRITE_ONLY_PY.split("\n\n", 1)[1]


# ── what a static read can read ─────────────────────────────────────


class TestReadsDeclarations:
    def test_read_only_tool(self, package_root):
        _, _, preview = _inspect(
            package_root, "ro", ("tools.py",), {"tools.py": READ_ONLY_PY},
        )
        assert [t.name for t in preview.tools] == ["pkg_read"]
        tool = preview.tools[0]
        # First line of the description only.
        assert tool.description == "List the widgets."
        assert tool.capabilities == ("database_read",)
        assert tool.trust_boundary == "chat"
        assert tool.always_available is False
        assert tool.requires_user_confirm is False
        assert tool.write_capabilities == ()
        assert tool.is_write_capable is False
        assert preview.write_tools == ()
        assert preview.unresolved == ()
        assert write_consent_required(preview, _StubManifest()) is False

    def test_write_capable_tool(self, package_root):
        _, _, preview = _inspect(
            package_root, "wo", ("tools.py",), {"tools.py": WRITE_ONLY_PY},
        )
        tool = preview.tools[0]
        assert tool.name == "pkg_write"
        assert set(tool.write_capabilities) == {"external_effect", "arc_create"}
        assert tool.is_write_capable is True
        assert tool.requires_user_confirm is True
        assert preview.write_tools == ("pkg_write",)
        assert preview.unresolved == ()

    def test_both_in_one_module(self, package_root):
        _, manifest, preview = _inspect(
            package_root, "both", ("tools.py",), {"tools.py": BOTH_PY},
        )
        assert [t.name for t in preview.tools] == ["pkg_read", "pkg_write"]
        assert preview.write_tools == ("pkg_write",)
        assert preview.read_only_tools == ("pkg_read",)
        assert preview.unresolved == ()
        read_only, write_capable = preview.as_classified()
        assert [t["name"] for t in read_only] == ["pkg_read"]
        assert [t["name"] for t in write_capable] == ["pkg_write"]
        assert write_consent_required(preview, manifest) is True

    def test_capabilities_default_to_pure(self, package_root):
        module = """\
            from carpenter.chat_tool_loader import chat_tool


            @chat_tool(
                description="No capabilities declared.",
                input_schema={"type": "object", "properties": {}},
            )
            def pkg_pure(tool_input, **kwargs):
                return "pure"
            """
        _, _, preview = _inspect(
            package_root, "pure", ("tools.py",), {"tools.py": module},
        )
        assert preview.tools[0].capabilities == ("pure",)
        assert preview.write_tools == ()
        assert preview.unresolved == ()

    def test_positional_arguments_and_flags(self, package_root):
        module = """\
            from carpenter.chat_tool_loader import chat_tool


            @chat_tool(
                "Positional description.",
                {"type": "object", "properties": {}},
                ["filesystem_read"],
                "chat",
                True,
                True,
            )
            def pkg_positional(tool_input, **kwargs):
                return "ok"
            """
        _, _, preview = _inspect(
            package_root, "posn", ("tools.py",), {"tools.py": module},
        )
        tool = preview.tools[0]
        assert tool.description == "Positional description."
        assert tool.capabilities == ("filesystem_read",)
        assert tool.always_available is True
        assert tool.requires_user_confirm is True
        assert preview.unresolved == ()

    def test_aliased_decorator_import(self, package_root):
        module = """\
            from carpenter.chat_tool_loader import chat_tool as ct
            import carpenter.chat_tool_loader as loader


            @ct(
                description="Aliased.",
                input_schema={"type": "object", "properties": {}},
                capabilities=["kb_read"],
            )
            def pkg_aliased(tool_input, **kwargs):
                return "ok"


            @loader.chat_tool(
                description="Module attribute.",
                input_schema={"type": "object", "properties": {}},
                capabilities=["database_write"],
            )
            def pkg_attr(tool_input, **kwargs):
                return "ok"
            """
        _, _, preview = _inspect(
            package_root, "alias", ("tools.py",), {"tools.py": module},
        )
        assert [t.name for t in preview.tools] == ["pkg_aliased", "pkg_attr"]
        assert preview.write_tools == ("pkg_attr",)
        assert preview.unresolved == ()

    def test_async_and_multiple_modules(self, package_root):
        extra = """\
            from carpenter.chat_tool_loader import chat_tool


            @chat_tool(
                description="Async tool.",
                input_schema={"type": "object", "properties": {}},
                capabilities=["config_read"],
            )
            async def pkg_async(tool_input, **kwargs):
                return "ok"
            """
        _, _, preview = _inspect(
            package_root, "multi", ("tools.py", "more/extra.py"),
            {"tools.py": READ_ONLY_PY, "more/extra.py": extra},
        )
        assert [t.name for t in preview.tools] == ["pkg_async", "pkg_read"]
        assert preview.tools[0].module == "more/extra.py"
        assert preview.unresolved == ()

    def test_platform_boundary_tool_is_listed_apart(self, package_root):
        module = """\
            from carpenter.chat_tool_loader import chat_tool


            @chat_tool(
                description="Wants to be platform.",
                input_schema={"type": "object", "properties": {}},
                capabilities=["external_effect"],
                trust_boundary="platform",
            )
            def pkg_platform(tool_input, **kwargs):
                return "refused"
            """
        _, _, preview = _inspect(
            package_root, "plat", ("tools.py",), {"tools.py": module},
        )
        assert preview.platform_boundary_tools == ("pkg_platform",)
        # Refused at registration whatever the operator opts into, so it
        # is not offered as something a write opt-in would enable.
        assert preview.write_tools == ()
        assert preview.as_classified() == ([], [])

    def test_module_with_no_tools(self, package_root):
        module = """\
            \"\"\"A chat-tool module that declares nothing.\"\"\"

            CONSTANT = 1


            def helper(x):
                return x + 1


            class Thing:
                def method(self):
                    return None
            """
        _, manifest, preview = _inspect(
            package_root, "empty", ("tools.py",), {"tools.py": module},
        )
        assert preview.tools == ()
        assert preview.write_tools == ()
        assert preview.unresolved == ()
        assert write_consent_required(preview, manifest) is False

    def test_manifest_declaring_no_chat_tools(self, package_root):
        pkg = write_package(
            package_root, name="bare",
            manifest_yaml=(
                "name: bare\n"
                'version: "0.1.0"\n'
                "description: No chat tools at all.\n"
            ),
        )
        manifest = load_manifest(pkg / "manifest.yaml")
        preview = inspect_package_chat_tools(pkg, manifest)
        assert preview.tools == ()
        assert preview.unresolved == ()
        assert write_consent_required(preview, manifest) is False

    def test_nested_and_class_scoped_definitions_are_ignored(
        self, package_root,
    ):
        # The runtime finds tools with ``dir(module)``, so neither of
        # these is ever a module attribute and neither ever registers.
        module = """\
            from carpenter.chat_tool_loader import chat_tool


            def factory():
                @chat_tool(
                    description="Nested.",
                    input_schema={"type": "object", "properties": {}},
                    capabilities=["external_effect"],
                )
                def pkg_nested(tool_input, **kwargs):
                    return "nested"
                return pkg_nested


            class Holder:
                @chat_tool(
                    description="In a class body.",
                    input_schema={"type": "object", "properties": {}},
                    capabilities=["arc_create"],
                )
                def pkg_method(self, tool_input, **kwargs):
                    return "method"


            @chat_tool(
                description="Top level.",
                input_schema={"type": "object", "properties": {}},
                capabilities=["pure"],
            )
            def pkg_top(tool_input, **kwargs):
                return "top"
            """
        _, _, preview = _inspect(
            package_root, "nested", ("tools.py",), {"tools.py": module},
        )
        assert [t.name for t in preview.tools] == ["pkg_top"]
        assert preview.write_tools == ()
        assert preview.unresolved == ()

    def test_inner_decorators_below_chat_tool_are_fine(self, package_root):
        # A decorator listed BELOW @chat_tool runs first; @chat_tool then
        # stamps its metadata on the result and returns it, so the tool
        # still registers and nothing is undetermined.
        module = """\
            import functools

            from carpenter.chat_tool_loader import chat_tool


            def noop(func):
                @functools.wraps(func)
                def inner(*a, **k):
                    return func(*a, **k)
                return inner


            @chat_tool(
                description="Wrapped underneath.",
                input_schema={"type": "object", "properties": {}},
                capabilities=["pure"],
            )
            @noop
            def pkg_inner(tool_input, **kwargs):
                return "ok"
            """
        _, _, preview = _inspect(
            package_root, "inner", ("tools.py",), {"tools.py": module},
        )
        assert [t.name for t in preview.tools] == ["pkg_inner"]
        assert preview.unresolved == ()


# ── what it refuses to guess ────────────────────────────────────────


class TestUnresolved:
    def test_non_literal_capabilities(self, package_root):
        module = """\
            from carpenter.chat_tool_loader import chat_tool

            CAPS = ["database_read"]


            @chat_tool(
                description="Capabilities from a variable.",
                input_schema={"type": "object", "properties": {}},
                capabilities=CAPS,
            )
            def pkg_dynamic(tool_input, **kwargs):
                return "ok"
            """
        _, manifest, preview = _inspect(
            package_root, "dyn", ("tools.py",), {"tools.py": module},
        )
        assert [t.name for t in preview.tools] == ["pkg_dynamic"]
        tool = preview.tools[0]
        assert tool.capabilities == ()
        assert tool.capabilities_unresolved is True
        # Fail closed: an unreadable capability list is write-capable.
        assert tool.is_write_capable is True
        assert preview.write_tools == ("pkg_dynamic",)
        assert len(preview.unresolved) == 1
        note = preview.unresolved[0]
        assert "tools.py" in note
        assert "capabilities=" in note
        assert "'CAPS'" in note
        assert write_consent_required(preview, manifest) is True

    @pytest.mark.parametrize(
        "expr,expected",
        [
            ("CAPS", "the variable 'CAPS'"),
            ("mod.CAPS", "the attribute 'CAPS'"),
            ("build_caps()", "a function call"),
            ('[f"{PREFIX}_read"]', "an f-string"),
            ("[c for c in SOURCE]", "a comprehension"),
            ('["database" + "_read"]', "an expression"),
        ],
    )
    def test_every_non_literal_shape_is_reported(
        self, package_root, expr, expected,
    ):
        module = (
            "from carpenter.chat_tool_loader import chat_tool\n\n\n"
            "@chat_tool(\n"
            '    description="Dynamic.",\n'
            '    input_schema={"type": "object"},\n'
            f"    capabilities={expr},\n"
            ")\n"
            "def pkg_dyn(tool_input, **kwargs):\n"
            "    return 'ok'\n"
        )
        _, _, preview = _inspect(
            package_root, "shape", ("tools.py",), {"tools.py": module},
        )
        assert preview.write_tools == ("pkg_dyn",)
        assert any(expected in n for n in preview.unresolved), (
            preview.unresolved
        )

    def test_starred_arguments(self, package_root):
        module = """\
            from carpenter.chat_tool_loader import chat_tool

            KW = {"description": "Hidden.", "input_schema": {}}


            @chat_tool(**KW)
            def pkg_splat(tool_input, **kwargs):
                return "ok"
            """
        _, _, preview = _inspect(
            package_root, "splat", ("tools.py",), {"tools.py": module},
        )
        assert [t.name for t in preview.tools] == ["pkg_splat"]
        assert preview.write_tools == ("pkg_splat",)
        assert any("unpacks" in n for n in preview.unresolved)

    def test_decorator_without_parentheses(self, package_root):
        module = """\
            from carpenter.chat_tool_loader import chat_tool


            @chat_tool
            def pkg_bare(tool_input, **kwargs):
                return "ok"
            """
        _, _, preview = _inspect(
            package_root, "bare", ("tools.py",), {"tools.py": module},
        )
        assert [t.name for t in preview.tools] == ["pkg_bare"]
        assert preview.write_tools == ("pkg_bare",)
        assert any(
            "without arguments" in n for n in preview.unresolved
        ), preview.unresolved

    def test_outer_decorator_is_reported(self, package_root):
        # A decorator ABOVE @chat_tool runs after it and may return an
        # object without the metadata, so registration is undetermined.
        module = """\
            from carpenter.chat_tool_loader import chat_tool


            def wrap(func):
                return lambda *a, **k: func(*a, **k)


            @wrap
            @chat_tool(
                description="Wrapped on top.",
                input_schema={"type": "object"},
                capabilities=["pure"],
            )
            def pkg_wrapped(tool_input, **kwargs):
                return "ok"
            """
        _, manifest, preview = _inspect(
            package_root, "outer", ("tools.py",), {"tools.py": module},
        )
        assert [t.name for t in preview.tools] == ["pkg_wrapped"]
        assert any("@wrap" in n for n in preview.unresolved)
        # Fail closed at the package level even though the tool itself
        # reads as read-only.
        assert preview.write_tools == ()
        assert write_consent_required(preview, manifest) is True

    def test_syntax_error_module(self, package_root):
        module = "def broken(:\n    pass\n"
        _, manifest, preview = _inspect(
            package_root, "syn", ("tools.py",), {"tools.py": module},
        )
        assert preview.tools == ()
        assert len(preview.unresolved) == 1
        assert "could not be parsed" in preview.unresolved[0]
        assert write_consent_required(preview, manifest) is True

    def test_missing_module(self, package_root):
        pkg = write_package(
            package_root, name="gone",
            manifest_yaml=_manifest("gone", ("tools.py",)),
        )
        manifest = load_manifest(pkg / "manifest.yaml")
        preview = inspect_package_chat_tools(pkg, manifest)
        assert preview.tools == ()
        assert any("is not a file" in n for n in preview.unresolved)
        assert write_consent_required(preview, manifest) is True

    def test_directory_instead_of_module(self, package_root):
        pkg = write_package(
            package_root, name="dir",
            manifest_yaml=_manifest("dir", ("tools.py",)),
        )
        (pkg / "tools.py").mkdir()
        manifest = load_manifest(pkg / "manifest.yaml")
        preview = inspect_package_chat_tools(pkg, manifest)
        assert preview.tools == ()
        assert any("is not a file" in n for n in preview.unresolved)

    def test_path_escaping_the_root(self, package_root):
        pkg = write_package(
            package_root, name="esc",
            manifest_yaml=_manifest("esc", ("../outside.py",)),
        )
        (package_root / "outside.py").write_text(READ_ONLY_PY)
        manifest = load_manifest(pkg / "manifest.yaml")
        preview = inspect_package_chat_tools(pkg, manifest)
        assert preview.tools == ()
        assert any(
            "outside the package root" in n for n in preview.unresolved
        )

    def test_absolute_path(self, package_root, tmp_path):
        outside = tmp_path / "outside.py"
        outside.write_text(READ_ONLY_PY)
        pkg = write_package(
            package_root, name="abs",
            manifest_yaml=_manifest("abs", (str(outside),)),
        )
        manifest = load_manifest(pkg / "manifest.yaml")
        preview = inspect_package_chat_tools(pkg, manifest)
        assert preview.tools == ()
        assert any("absolute path" in n for n in preview.unresolved)

    def test_non_python_module(self, package_root):
        _, _, preview = _inspect(
            package_root, "notpy", ("tools.txt",), {"tools.txt": "nope"},
        )
        assert preview.tools == ()
        assert any("not a .py file" in n for n in preview.unresolved)

    def test_declaration_the_runtime_would_refuse(self, package_root):
        # Each of these raises inside the decorator at import time, so
        # the whole module registers nothing — a static read cannot
        # claim the declared tool would appear.
        module = """\
            from carpenter.chat_tool_loader import chat_tool


            @chat_tool(
                description="Unknown capability.",
                input_schema={"type": "object"},
                capabilities=["made_up"],
            )
            def pkg_unknown_cap(tool_input, **kwargs):
                return "ok"


            @chat_tool(
                description="Pure plus more.",
                input_schema={"type": "object"},
                capabilities=["pure", "database_read"],
            )
            def pkg_pure_mixed(tool_input, **kwargs):
                return "ok"


            @chat_tool(
                description="Bogus boundary.",
                input_schema={"type": "object"},
                capabilities=["pure"],
                trust_boundary="superuser",
            )
            def pkg_bad_boundary(tool_input, **kwargs):
                return "ok"


            @chat_tool(
                description="Unknown keyword.",
                input_schema={"type": "object"},
                name="renamed",
            )
            def pkg_unknown_kwarg(tool_input, **kwargs):
                return "ok"


            @chat_tool(description="No schema.")
            def pkg_missing_schema(tool_input, **kwargs):
                return "ok"
            """
        _, manifest, preview = _inspect(
            package_root, "refused", ("tools.py",), {"tools.py": module},
        )
        notes = "\n".join(preview.unresolved)
        assert "unknown capabilit" in notes
        assert "mixes 'pure'" in notes
        assert "trust_boundary='superuser'" in notes
        assert "unknown argument 'name'" in notes
        assert "does not pass 'input_schema'" in notes
        assert write_consent_required(preview, manifest) is True

    def test_tool_name_is_always_the_function_name(self, package_root):
        # ``chat_tool`` has no ``name`` parameter: the decorator sets
        # ``name = func.__name__``, so nothing in the decorator call can
        # rename a tool.
        module = """\
            from carpenter.chat_tool_loader import chat_tool


            @chat_tool(
                description="Tries to rename itself.",
                input_schema={"type": "object"},
                capabilities=["pure"],
                name="not_my_name",
            )
            def pkg_real_name(tool_input, **kwargs):
                return "ok"
            """
        _, _, preview = _inspect(
            package_root, "named", ("tools.py",), {"tools.py": module},
        )
        assert [t.name for t in preview.tools] == ["pkg_real_name"]


# ── the regression: no package code runs ────────────────────────────


class TestNeverExecutesPackageCode:
    def test_hostile_module_previews_without_running(
        self, tmp_path, monkeypatch,
    ):
        """The vulnerability this work package closes.

        ``fixtures/hostile_preview_pkg/tools.py`` writes a marker file
        and then raises when it is imported.  The static preview must
        describe its tool and leave no marker.
        """
        marker = tmp_path / "executed.marker"
        monkeypatch.setenv("CARPENTER_TEST_PREVIEW_MARKER", str(marker))

        pkg = FIXTURES / "hostile_preview_pkg"
        manifest = load_manifest(pkg / "manifest.yaml")
        preview = inspect_package_chat_tools(pkg, manifest)

        assert not marker.exists(), (
            "the package's module-level code ran during the preview"
        )
        assert [t.name for t in preview.tools] == ["hostile_tool"]
        tool = preview.tools[0]
        assert tool.description == "Looks harmless in a listing."
        assert tool.capabilities == ("filesystem_write",)
        assert tool.requires_user_confirm is True
        assert preview.write_tools == ("hostile_tool",)
        assert preview.unresolved == ()

    def test_the_fixture_really_is_hostile(self, tmp_path, monkeypatch):
        """Prove the fixture is dangerous, so the test above means something.

        The import-based classifier leaves the marker behind and — because
        the module raises — reports no tools at all.  That is exactly the
        trade the static preview removes.
        """
        marker = tmp_path / "executed.marker"
        monkeypatch.setenv("CARPENTER_TEST_PREVIEW_MARKER", str(marker))

        manifest = load_manifest(
            FIXTURES / "hostile_preview_pkg" / "manifest.yaml",
        )
        read_only, write_capable = classify_package_chat_tools(manifest)

        assert marker.exists()
        assert marker.read_text().strip() == "the package's code ran"
        assert (read_only, write_capable) == ([], [])

    def test_inspection_adds_nothing_to_sys_modules(self, package_root):
        before = {
            name for name in sys.modules
            if name == "_carpenter_pkg_" or name.startswith("_carpenter_pkg_.")
        }
        pkg = write_package(
            package_root, name="quiet",
            manifest_yaml=_manifest("quiet", ("tools.py",)),
            files={"tools.py": BOTH_PY},
        )
        manifest = load_manifest(pkg / "manifest.yaml")
        preview = inspect_package_chat_tools(pkg, manifest)
        assert len(preview.tools) == 2
        after = {
            name for name in sys.modules
            if name == "_carpenter_pkg_" or name.startswith("_carpenter_pkg_.")
        }
        assert after == before
        assert not any("quiet" in name for name in after)


# ── parity with the runtime classifier ──────────────────────────────


_PARITY_CASES = {
    "p_read": READ_ONLY_PY,
    "p_write": WRITE_ONLY_PY,
    "p_both": BOTH_PY,
    "p_pure": (
        "from carpenter.chat_tool_loader import chat_tool\n\n\n"
        "@chat_tool(\n"
        '    description="Defaults.",\n'
        '    input_schema={"type": "object"},\n'
        ")\n"
        "def pkg_default(tool_input, **kwargs):\n"
        "    return 'ok'\n"
    ),
    "p_alias": (
        "from carpenter.chat_tool_loader import chat_tool as ct\n\n\n"
        "@ct(\n"
        '    description="Aliased import.",\n'
        '    input_schema={"type": "object"},\n'
        '    capabilities=["filesystem_write"],\n'
        ")\n"
        "def pkg_alias(tool_input, **kwargs):\n"
        "    return 'ok'\n"
    ),
    "p_platform": (
        "from carpenter.chat_tool_loader import chat_tool\n\n\n"
        "@chat_tool(\n"
        '    description="Platform boundary.",\n'
        '    input_schema={"type": "object"},\n'
        '    capabilities=["pure"],\n'
        '    trust_boundary="platform",\n'
        ")\n"
        "def pkg_plat(tool_input, **kwargs):\n"
        "    return 'ok'\n"
    ),
    "p_empty": '"""Nothing here."""\n\nX = 1\n',
}


class TestParityWithImportClassifier:
    @pytest.mark.parametrize("name", sorted(_PARITY_CASES))
    def test_static_matches_classify_for_well_behaved_packages(
        self, package_root, name,
    ):
        _, manifest, preview = _inspect(
            package_root, name, ("tools.py",),
            {"tools.py": _PARITY_CASES[name]},
        )
        imported_ro, imported_wr = classify_package_chat_tools(manifest)
        static_ro, static_wr = preview.as_classified()

        assert [t["name"] for t in static_ro] == [
            t["name"] for t in imported_ro
        ]
        assert [t["name"] for t in static_wr] == [
            t["name"] for t in imported_wr
        ]
        assert preview.unresolved == ()
        for static_entry, imported_entry in zip(
            static_ro + static_wr, imported_ro + imported_wr,
        ):
            assert static_entry == imported_entry


class _StubManifest:
    """Minimal stand-in with no declared chat tools."""

    chat_tools = ()


class TestModuleScope:
    def test_tool_guarded_by_a_module_level_if_is_found(self, package_root):
        # Still a module attribute at runtime, so it still registers.
        module = """\
            import sys

            from carpenter.chat_tool_loader import chat_tool

            if sys.version_info >= (3, 11):

                @chat_tool(
                    description="Guarded.",
                    input_schema={"type": "object"},
                    capabilities=["external_effect"],
                )
                def pkg_guarded(tool_input, **kwargs):
                    return "ok"

            try:

                @chat_tool(
                    description="In a try block.",
                    input_schema={"type": "object"},
                    capabilities=["pure"],
                )
                def pkg_tried(tool_input, **kwargs):
                    return "ok"

            except Exception:
                pass
            """
        _, _, preview = _inspect(
            package_root, "guarded", ("tools.py",), {"tools.py": module},
        )
        assert [t.name for t in preview.tools] == ["pkg_guarded", "pkg_tried"]
        assert preview.write_tools == ("pkg_guarded",)
        assert preview.unresolved == ()

    def test_decorator_re_exported_through_a_shim(self, package_root):
        module = """\
            from .shim import chat_tool as ct


            @ct(
                description="Via a package shim.",
                input_schema={"type": "object"},
                capabilities=["arc_create"],
            )
            def pkg_shimmed(tool_input, **kwargs):
                return "ok"
            """
        shim = """\
            from carpenter.chat_tool_loader import chat_tool  # noqa: F401
            """
        _, _, preview = _inspect(
            package_root, "shim", ("tools.py",),
            {"tools.py": module, "shim.py": shim},
        )
        assert [t.name for t in preview.tools] == ["pkg_shimmed"]
        assert preview.write_tools == ("pkg_shimmed",)
        assert preview.unresolved == ()
