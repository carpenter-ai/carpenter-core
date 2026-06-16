"""Tests for D24 stage 3b loaders: arc templates, JUDGE handlers, data models.

These exercise the artifact-loading pipeline that connects an installed
package's manifest to the platform's runtime registries.  Phase A and
Stage 3a tests focus on chat-tool registration and install hashing;
this file covers the trust-graduating Resource handoff path that
D24 §3.6 / §5.5 specify.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter.packages.handler_registry import (
    PackageHandlerRegistry,
    get_handler_registry,
)
from carpenter.packages.installer import (
    ensure_installer_tables,
    install_package,
)
from carpenter.packages.manifest import load_manifest
from carpenter.packages.loaders import load_package_artifacts
from carpenter.packages.registry import PackageRegistry


SIMPLE_DATA_MODELS = """\
from dataclasses import dataclass

@dataclass(frozen=True)
class WidgetExtract:
    name: str
    qty: int

@dataclass(frozen=True)
class WidgetBriefing:
    keyword: str
"""

SIMPLE_JUDGE = """\
def judge_widget(extract):
    class _Result:
        approved = (extract.qty <= 10)
        reason = "" if extract.qty <= 10 else f"qty {extract.qty} > 10"
        checks = []
    return _Result()
"""

SIMPLE_TEMPLATE_YAML = """\
name: widget-triage
description: Test template
steps:
  - name: extract
    role: extract
    order: 1
"""


def _write_widget_pkg(root: Path, name: str = "widget") -> Path:
    """Write a widget package with templates+judges+data_models."""
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.yaml").write_text(dedent(f"""\
        name: {name}
        version: "0.1.0"
        description: Test widget package.
        arc_templates:
          - name: widget-triage
            path: templates/widget-triage/template.yaml
            briefing_kind: WidgetBriefing
            extract_kind: WidgetExtract
            judge_handler: judges.widget:judge_widget
        judge_handlers:
          - name: judge_widget
            module: judges.widget
        data_models:
          - WidgetExtract
          - WidgetBriefing
    """))
    (pkg / "data_models.py").write_text(dedent(SIMPLE_DATA_MODELS))
    (pkg / "judges").mkdir()
    (pkg / "judges" / "__init__.py").write_text("")
    (pkg / "judges" / "widget.py").write_text(dedent(SIMPLE_JUDGE))
    (pkg / "templates").mkdir()
    (pkg / "templates" / "widget-triage").mkdir()
    (pkg / "templates" / "widget-triage" / "template.yaml").write_text(
        dedent(SIMPLE_TEMPLATE_YAML),
    )
    return pkg


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_installer_tables(conn)
    yield conn
    conn.close()


@pytest.fixture
def fresh_registry():
    """Reset the global handler registry before each test."""
    reg = get_handler_registry()
    reg.reset()
    yield reg
    reg.reset()


# ── Data models ─────────────────────────────────────────────────────


class TestDataModelLoader:
    def test_data_models_register(self, tmp_path, fresh_registry):
        pkg = _write_widget_pkg(tmp_path)
        manifest = load_manifest(pkg / "manifest.yaml")
        from carpenter.packages.loaders import load_data_models
        n, errors = load_data_models(manifest)
        assert errors == []
        assert n == 2
        assert fresh_registry.lookup_kind("WidgetExtract") is not None
        assert fresh_registry.lookup_kind("WidgetBriefing") is not None

    def test_data_model_lookup_returns_dataclass(self, tmp_path, fresh_registry):
        pkg = _write_widget_pkg(tmp_path)
        manifest = load_manifest(pkg / "manifest.yaml")
        from carpenter.packages.loaders import load_data_models
        load_data_models(manifest)
        cls = fresh_registry.lookup_kind("WidgetExtract")
        # Round-trip via dict.
        instance = cls(**{"name": "foo", "qty": 3})
        assert instance.name == "foo"
        assert instance.qty == 3

    def test_collision_with_platform_kind_rejected(self, tmp_path, fresh_registry):
        # Try to register a kind named ``PolicyCheckList`` (platform).
        pkg = tmp_path / "evilpkg"
        pkg.mkdir()
        (pkg / "manifest.yaml").write_text(dedent("""\
            name: evilpkg
            version: "0.1.0"
            description: Evil pkg
            data_models:
              - PolicyCheckList
        """))
        (pkg / "data_models.py").write_text(dedent("""\
            from dataclasses import dataclass
            @dataclass
            class PolicyCheckList:
                checks: list = None
        """))
        manifest = load_manifest(pkg / "manifest.yaml")
        from carpenter.packages.loaders import load_data_models
        n, errors = load_data_models(manifest)
        assert n == 0
        assert any("platform-reserved" in e for e in errors), errors

    def test_cross_package_kind_collision_rejected(self, tmp_path, fresh_registry):
        # Two packages both ship WidgetExtract.
        pkg_a = _write_widget_pkg(tmp_path / "a", "alpha")
        pkg_b = _write_widget_pkg(tmp_path / "b", "beta")
        manifest_a = load_manifest(pkg_a / "manifest.yaml")
        manifest_b = load_manifest(pkg_b / "manifest.yaml")
        from carpenter.packages.loaders import load_data_models
        na, _ = load_data_models(manifest_a)
        nb, errs_b = load_data_models(manifest_b)
        assert na == 2
        # First package's classes win; second package's collide.
        assert nb == 0
        assert any("already" in e for e in errs_b), errs_b


# ── JUDGE handlers ──────────────────────────────────────────────────


class TestJudgeHandlerLoader:
    def test_judge_handler_registers(self, tmp_path, fresh_registry):
        pkg = _write_widget_pkg(tmp_path)
        manifest = load_manifest(pkg / "manifest.yaml")
        # Data models must load first so the judge can import them.
        load_package_artifacts(manifest)
        handler = fresh_registry.lookup_judge("widget-triage")
        assert handler is not None
        assert callable(handler)

    def test_judge_runs_on_dataclass(self, tmp_path, fresh_registry):
        pkg = _write_widget_pkg(tmp_path)
        manifest = load_manifest(pkg / "manifest.yaml")
        load_package_artifacts(manifest)

        cls = fresh_registry.lookup_kind("WidgetExtract")
        handler = fresh_registry.lookup_judge("widget-triage")
        result = handler(cls(name="x", qty=3))
        assert result.approved is True
        result = handler(cls(name="y", qty=20))
        assert result.approved is False

    def test_judge_signature_violation_rejected(self, tmp_path, fresh_registry):
        pkg = tmp_path / "badpkg"
        pkg.mkdir()
        (pkg / "manifest.yaml").write_text(dedent("""\
            name: badpkg
            version: "0.1.0"
            description: Bad
            arc_templates:
              - name: bad-template
                path: t.yaml
                judge_handler: bad_judge:judge_two_args
            judge_handlers:
              - name: judge_two_args
                module: bad_judge
            data_models:
              - WidgetExtract
        """))
        (pkg / "data_models.py").write_text(dedent("""\
            from dataclasses import dataclass
            @dataclass
            class WidgetExtract:
                name: str
        """))
        (pkg / "bad_judge.py").write_text(dedent("""\
            def judge_two_args(extract, db):  # WRONG: 2 args
                class R: approved=True; reason=""; checks=[]
                return R()
        """))
        (pkg / "t.yaml").write_text(dedent("""\
            name: bad-template
            description: bad
            steps: []
        """))
        manifest = load_manifest(pkg / "manifest.yaml")
        from carpenter.packages.loaders import load_judge_handlers, load_data_models
        load_data_models(manifest)
        n, errors = load_judge_handlers(manifest)
        assert n == 0, errors
        assert any("exactly one positional" in e for e in errors), errors

    def test_judge_against_platform_template_rejected(self, tmp_path, fresh_registry):
        pkg = tmp_path / "shadow"
        pkg.mkdir()
        # `reflection` is in _PLATFORM_TEMPLATES.
        (pkg / "manifest.yaml").write_text(dedent("""\
            name: shadow
            version: "0.1.0"
            description: Shadow
            arc_templates:
              - name: reflection
                path: t.yaml
                judge_handler: j:judge_anything
            judge_handlers:
              - name: judge_anything
                module: j
            data_models: []
        """))
        (pkg / "j.py").write_text(dedent("""\
            def judge_anything(extract):
                class R: approved=True; reason=""; checks=[]
                return R()
        """))
        (pkg / "t.yaml").write_text(dedent("""\
            name: reflection
            description: shadow
            steps: []
        """))
        manifest = load_manifest(pkg / "manifest.yaml")
        from carpenter.packages.loaders import (
            load_arc_templates, load_judge_handlers, load_data_models,
        )
        load_data_models(manifest)
        n_t, errs_t, _ = load_arc_templates(manifest)
        assert n_t == 0
        assert any("platform-reserved" in e for e in errs_t), errs_t
        n_j, errs_j = load_judge_handlers(manifest)
        # Even if we tried to register, the handler-registry rejects.
        assert n_j == 0
        assert any("platform-reserved" in e or "platform" in e for e in errs_j), errs_j


# ── Templates ───────────────────────────────────────────────────────


class TestTemplateLoader:
    def test_template_loads_into_db(self, tmp_path, fresh_registry, monkeypatch):
        from carpenter.core.engine import template_manager

        # Set up an in-memory DB with the templates schema.
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE workflow_templates (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                description TEXT,
                yaml_path TEXT,
                required_for_json TEXT,
                steps_json TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                owner_package TEXT,
                updated_at TEXT NOT NULL
            );
        """)
        conn.commit()

        class _DBCtx:
            def __enter__(self_):
                return conn
            def __exit__(self_, *a):
                return False
        # template_manager imports db_transaction/db_connection by name;
        # patch the attributes on the template_manager module.
        monkeypatch.setattr(template_manager, "db_transaction", lambda: _DBCtx())
        monkeypatch.setattr(template_manager, "db_connection", lambda: _DBCtx())

        pkg = _write_widget_pkg(tmp_path)
        manifest = load_manifest(pkg / "manifest.yaml")
        from carpenter.packages.loaders import load_arc_templates
        n, errors, names = load_arc_templates(manifest)
        assert errors == [], errors
        assert n == 1
        assert names == ["widget-triage"]
        row = conn.execute(
            "SELECT name FROM workflow_templates WHERE name = ?",
            ("widget-triage",),
        ).fetchone()
        assert row is not None


# ── Integration: full registry flow ─────────────────────────────────


class TestFullPackageLoad:
    def test_install_then_register_loads_artifacts(
        self, tmp_path, db_conn, fresh_registry, monkeypatch,
    ):
        from carpenter import db as carpenter_db

        # Provide the workflow_templates table for template_manager.
        db_conn.row_factory = sqlite3.Row
        db_conn.executescript("""
            CREATE TABLE IF NOT EXISTS workflow_templates (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                description TEXT,
                yaml_path TEXT,
                required_for_json TEXT,
                steps_json TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
        """)
        db_conn.commit()

        class _DBCtx:
            def __enter__(self_):
                return db_conn
            def __exit__(self_, *a):
                return False
        monkeypatch.setattr(carpenter_db, "db_transaction", lambda: _DBCtx())
        monkeypatch.setattr(carpenter_db, "db_connection", lambda: _DBCtx())

        # Install widget package.
        src = _write_widget_pkg(tmp_path / "src")
        dest_root = tmp_path / "installed"
        dest = dest_root / "widget"
        install_package(src, dest, conn=db_conn)

        registry = PackageRegistry()
        loaded = registry.discover_and_register(
            search_paths=[dest_root], db_conn=db_conn,
        )
        assert len(loaded) == 1
        pkg = loaded[0]
        assert pkg.manifest.name == "widget"
        # Loaded artifacts: 2 data models, 1 template, 1 judge.
        counts = pkg.artifact_counts
        assert counts.get("data_models", 0) == 2, pkg.load_errors
        assert counts.get("arc_templates", 0) == 1, pkg.load_errors
        assert counts.get("judge_handlers", 0) == 1, pkg.load_errors
        assert pkg.template_names == ("widget-triage",)
        # Judge actually present in registry.
        assert fresh_registry.lookup_judge("widget-triage") is not None
        # Kind lookup goes via combined registry.
        assert fresh_registry.lookup_kind("WidgetExtract") is not None


# ── Loader bug regression: relative imports must share class identity ───


class TestImportPackageModuleRelativeImports:
    """Direct regression coverage for the ``_import_package_module`` fix.

    The bug: ``spec_from_file_location`` was being called with
    ``submodule_search_locations=[candidate.parent]`` for every module
    (not just package ``__init__.py`` files).  That made plain modules
    like ``judges.py`` *look* like sub-packages, so a relative import
    inside them (``from .data_models import M``) re-imported
    ``data_models`` a second time as
    ``_carpenter_pkg_.<pkg>.judges.data_models``.  The class object
    pulled in via the second import was a *different* object from the
    one previously loaded as ``_carpenter_pkg_.<pkg>.data_models.M``,
    so JUDGE handlers' ``isinstance`` checks (and platform-level kind
    registry lookups by class identity) silently failed.  The fix:
    only set ``submodule_search_locations`` when the candidate file
    is actually an ``__init__.py``.

    These tests exercise the property directly — no email package, no
    external repo dependency — so the regression is caught here even
    if downstream package tests are skipped or unavailable.
    """

    def _write_flat_pkg(self, root: Path, pkg_name: str = "testpkg") -> Path:
        pkg = root / pkg_name
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "data_models.py").write_text(dedent("""\
            from dataclasses import dataclass

            @dataclass
            class M:
                x: int = 0
        """))
        (pkg / "judges.py").write_text(dedent("""\
            from .data_models import M

            # Re-export the class so the test can assert that the
            # judges module sees the *same* class object that the
            # data_models module exposes.
            M_alias = M

            def get_M():
                return M
        """))
        return pkg

    def test_relative_import_shares_class_identity(self, tmp_path):
        """``judges.py``'s ``from .data_models import M`` must resolve to
        the SAME class object that ``data_models.py`` exports."""
        from carpenter.packages.loaders import _import_package_module

        # Use a unique pkg_name per test to avoid sys.modules cache pollution
        # from previous test runs in the same process.
        pkg_name = "loader_canary_identity"
        pkg = self._write_flat_pkg(tmp_path, pkg_name)

        data_mod = _import_package_module(pkg_name, "data_models", pkg)
        judges_mod = _import_package_module(pkg_name, "judges", pkg)

        # Property #1: callable returning the imported class returns the
        # same class object that data_models exposes.  This is the
        # property that broke when judges.py was incorrectly treated as
        # a sub-package.
        assert judges_mod.get_M() is data_mod.M, (
            "judges.py's relative import of M must share class identity "
            "with data_models.M; if these differ, the loader is treating "
            "non-__init__ modules as sub-packages again."
        )

    def test_isinstance_check_works_across_modules(self, tmp_path):
        """An instance constructed from ``data_models.M`` must satisfy
        ``isinstance`` against the alias re-exported by ``judges.py``.

        This mimics what JUDGE handlers actually do: dispatch deserialises
        a dataclass via the kind registry (using the data_models class)
        and the handler then runs ``isinstance(extract, MyType)`` where
        ``MyType`` was imported via a relative import inside the judge
        module.  If the loader regresses, isinstance returns False even
        though the two classes are spelled the same way.
        """
        from carpenter.packages.loaders import _import_package_module

        pkg_name = "loader_canary_isinstance"
        pkg = self._write_flat_pkg(tmp_path, pkg_name)

        data_mod = _import_package_module(pkg_name, "data_models", pkg)
        judges_mod = _import_package_module(pkg_name, "judges", pkg)

        instance = data_mod.M(x=7)
        assert isinstance(instance, judges_mod.M_alias), (
            "isinstance(data_models.M(), judges.M_alias) must hold; "
            "if it doesn't, the loader has re-imported data_models as a "
            "submodule of judges, breaking JUDGE handler dispatch."
        )
